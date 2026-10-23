"""Bounded, read-only local support report. No application or AWS operations."""

from __future__ import annotations

import hashlib
import importlib.metadata
import ipaddress
import json
import os
import platform
import re
import selectors
import stat
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from rich.console import Console

from stelvio.tunnel.diagnostics.protocol import observation, timestamp
from stelvio.tunnel.diagnostics.runtime import discover, snapshot
from stelvio.tunnel.helper_client import NativeHelper

REPORT_LIMIT = 8 * 1024 * 1024
OUTPUT_LIMIT = 128 * 1024
RESOLVER_LIMIT = 4096
IPV4_OCTETS = 4
ROUTE_MIN_COLUMNS = 4
MAX_ROUTE_FINDINGS = 256
MAX_FINDINGS = 512
MAX_ARTIFACT_BYTES = 32 * 1024 * 1024


def command(arguments: list[str], deadline: float) -> dict:
    """Read bounded output without communicate() allocating unbounded memory."""
    remaining = min(2, deadline - time.monotonic())
    if remaining <= 0:
        return observation(status="skipped", code="report-deadline")
    end = time.monotonic() + remaining
    try:
        with subprocess.Popen(  # noqa: S603 - fixed system tools; arguments never use a shell
            arguments,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LC_ALL": "C"},
        ) as process:
            output = bytearray()
            truncated = False
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while selector.get_map():
                    wait_seconds = end - time.monotonic()
                    if wait_seconds <= 0:
                        process.kill()
                        process.wait()
                        return observation(status="failed", code="command-timeout")
                    for key, _ in selector.select(wait_seconds):
                        part = os.read(
                            key.fileobj.fileno(), min(8192, OUTPUT_LIMIT + 1 - len(output))
                        )
                        if not part:
                            selector.unregister(key.fileobj)
                        else:
                            output.extend(part)
                        if len(output) > OUTPUT_LIMIT:
                            truncated = True
                            process.kill()
                            selector.unregister(key.fileobj)
                try:
                    process.wait(timeout=max(0.001, end - time.monotonic()))
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                    return observation(status="failed", code="command-timeout")
            return observation(
                {
                    "output": output[:OUTPUT_LIMIT].decode(errors="replace"),
                    "truncated": truncated,
                    "exit_code": process.returncode,
                },
                status="collected" if process.returncode == 0 or truncated else "unavailable",
                code=None if process.returncode == 0 or truncated else "command-denied-or-failed",
            )
    except OSError as error:
        return observation(status="unavailable", code=type(error).__name__)


def versions() -> dict:
    installed = {}
    for package in ("stelvio", "pulumi"):
        try:
            installed[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            installed[package] = None
    return observation(
        dict(
            installed,
            python=platform.python_version(),
            system=platform.system(),
            architecture=platform.machine(),
            kernel=platform.release(),
            macos=platform.mac_ver()[0] or None,
        )
    )


def _artifact(path: Path, digest: str | None, owner: int, architecture: str) -> dict:
    info = path.lstat()
    value = {
        "path": str(path),
        "owner_uid": info.st_uid,
        "mode": oct(stat.S_IMODE(info.st_mode)),
        "size": info.st_size,
        "expected_digest": digest,
        "architecture": architecture,
    }
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != owner
            or before.st_mode & 0o022
            or not 0 < before.st_size <= MAX_ARTIFACT_BYTES
        ):
            value["validation"] = "unsafe"
            return observation(value, status="failed", code="artifact-permissions")
        data = stream.read(MAX_ARTIFACT_BYTES + 1)
        from stelvio.tunnel.assets import validate_macho  # noqa: PLC0415

        validate_macho(data, architecture)
        after = os.fstat(stream.fileno())
        actual = hashlib.sha256(data).hexdigest()
        value["digest"] = actual
        stable = (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) == (
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        value["validation"] = (
            ("developer-image" if digest is None else "verified-digest")
            if stable and (digest is None or actual == digest)
            else "digest-mismatch"
        )
        # No executable is launched by artifact inspection, including unknown developer images.
        return observation(
            value,
            status="collected" if stable and (digest is None or actual == digest) else "failed",
            code=None if stable and (digest is None or actual == digest) else "artifact-digest",
        )


def artifacts() -> dict:
    from stelvio.pulumi import get_stelvio_config_dir  # noqa: PLC0415
    from stelvio.tunnel.assets import (  # noqa: PLC0415
        DEVELOPMENT_BINARY_ENV,
        TARGETS,
        TRAFORO_VERSION,
        _validate_developer_parent,
    )
    from stelvio.tunnel.installation import INSTALLED_HELPER  # noqa: PLC0415
    from stelvio.tunnel.plugin import BINARY_DIGESTS, PLUGIN_VERSION  # noqa: PLC0415

    architecture = platform.machine()
    if architecture not in TARGETS:
        return observation(status="unavailable", code="unsupported-architecture")
    # get_bin_path() creates directories. Inspection uses the read-only parent path instead.
    cache = get_stelvio_config_dir() / "bin"
    result = {"pinned_traforo_version": TRAFORO_VERSION, "pinned_plugin_version": PLUGIN_VERSION}
    selected = cache / f"stelvio-traforo-{TRAFORO_VERSION}-{architecture}"
    selected_digest = TARGETS[architecture]["sha256"]
    developer = os.environ.get(DEVELOPMENT_BINARY_ENV)
    if developer:
        try:
            candidate = Path(developer)
            parent = _validate_developer_parent(candidate)
            selected = parent / candidate.name
            result["selected_traforo"] = _artifact(selected, None, os.geteuid(), architecture)
            selected_digest = result["selected_traforo"]["data"]["digest"]
        except (OSError, RuntimeError, ValueError, KeyError) as error:
            result["selected_traforo"] = observation(status="failed", code=type(error).__name__)
    for name, path, digest, owner in (
        ("installed_traforo", INSTALLED_HELPER, selected_digest, 0),
        (
            "selected_traforo",
            cache / f"stelvio-traforo-{TRAFORO_VERSION}-{architecture}",
            TARGETS[architecture]["sha256"],
            os.geteuid(),
        ),
        (
            "plugin",
            cache / f"session-manager-plugin-{PLUGIN_VERSION}-{architecture}",
            BINARY_DIGESTS[architecture],
            os.geteuid(),
        ),
    ):
        if name == "selected_traforo" and developer:
            continue
        try:
            result[name] = _artifact(path, digest, owner, architecture)
        except (OSError, ValueError, RuntimeError) as error:
            result[name] = observation(status="unavailable", code=type(error).__name__)
    return observation(result)


def helper() -> dict:
    client = NativeHelper(timeout=0.5)
    try:
        return observation(client.diagnostics())
    except (OSError, RuntimeError, ValueError, EOFError):
        try:
            value = asdict(client.status())
            for unit in value["units"]:
                unit["status"] = unit["status"].name.lower()
            return observation(
                {"legacy_status": value, "statistics_available": False},
                code="diagnostics-unsupported",
            )
        except (OSError, RuntimeError, ValueError, EOFError) as error:
            return observation(status="unavailable", code=type(error).__name__)


def sessions() -> dict:
    try:
        paths = discover()
    except (OSError, ValueError) as error:
        return observation(status="failed", code=type(error).__name__)
    values = []
    errors = []
    # Up to four clients in parallel; dead sockets do not suppress healthy sessions.
    with ThreadPoolExecutor(max_workers=4) as pool:
        pending = {pool.submit(snapshot, path): path for path in paths[:8]}
        for future, endpoint in pending.items():
            try:
                values.append(future.result())
            except (OSError, ValueError, TypeError, EOFError, RecursionError) as error:
                errors.append({"endpoint": endpoint.name, "code": type(error).__name__})
    return observation(
        {
            "sessions": values,
            "endpoint_errors": errors,
            "omitted_endpoints": max(0, len(paths) - 8),
        }
    )


SYSTEM_COMMANDS = {
    "macos_build": ["/usr/bin/sw_vers"],
    "interfaces": ["/sbin/ifconfig", "-a"],
    "routes": ["/usr/sbin/netstat", "-rn"],
    "default_route": ["/sbin/route", "-n", "get", "default"],
    "resolvers": ["/usr/sbin/scutil", "--dns"],
    "network_services": ["/usr/sbin/networksetup", "-listallnetworkservices"],
    "proxies": ["/usr/sbin/scutil", "--proxy"],
    "firewall": ["/usr/libexec/ApplicationFirewall/socketfilterfw", "--getglobalstate"],
    "packet_filter": ["/sbin/pfctl", "-s", "info"],
    "helper_service": ["/bin/launchctl", "print", "system/dev.stelvio.tunnel"],
}


def _sanitize_system(name: str, value: dict) -> dict:  # noqa: C901 - explicit allowlists
    data = value.get("data")
    if not data:
        return value
    text = data["output"]
    if name == "proxies":
        # Keep only routing-relevant scalars. PAC URLs can carry tokens or userinfo.
        clean = []
        for line in text.splitlines():
            key, separator, item = line.partition(" : ")
            key = key.strip()
            if not separator:
                continue
            if key.endswith(("Enable", "Port")):
                if item.strip().isdigit():
                    clean.append(f"{key}: {item.strip()}")
            elif key in {"HTTPProxy", "HTTPSProxy", "SOCKSProxy", "FTPProxy"}:
                host = item.strip()
                if re.fullmatch(r"[a-zA-Z0-9.:[\]-]{1,253}", host):
                    clean.append(f"{key}: {host}")
            elif key == "ProxyAutoConfigURLString":
                parsed = urlsplit(item.strip())
                clean.append(f"PAC configured: {parsed.scheme or 'unknown'}")
        data["output"] = "\n".join(clean)
    elif name == "helper_service":
        # launchctl print can include inherited environment variables. Never retain them.
        allowed = {"state", "pid", "runs", "last exit code", "type", "program"}
        data["output"] = "\n".join(
            line.strip()
            for line in text.splitlines()
            if line.strip().partition(" = ")[0] in allowed
        )
    return value


def _resolver_files(sections: dict) -> dict:
    candidates = set()
    helper_data = sections.get("helper", {}).get("data") or {}
    samples = [helper_data]
    samples.extend(
        item.get("sources", {}).get("helper", {}).get("data") or {}
        for item in (sections.get("sessions", {}).get("data") or {}).get("sessions", [])
    )
    for sample in samples:
        for unit in sample.get("journal_units", []):
            for receipt in unit.get("resolver_receipts", []):
                name = receipt.get("name", "")
                if re.fullmatch(
                    r"stelvio\.[0-9a-f]{32}\.[0-9a-f]{8}\.[0-9a-f]{16}\.[0-9a-f]{2}", name
                ):
                    candidates.add(name)
    result = []
    for name in sorted(candidates)[:512]:
        path = Path("/etc/resolver") / name
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError("Resolver is not a regular file")  # noqa: TRY301
                text = stream.read(RESOLVER_LIMIT + 1)
            lines = [
                line
                for line in text.decode(errors="replace").splitlines()
                if re.fullmatch(
                    (
                        r"(?:nameserver [0-9a-fA-F.:]+|port [0-9]+|domain [a-zA-Z0-9.-]+|"
                        r"search_order [0-9]+|timeout [0-9]+)"
                    ),
                    line,
                )
            ]
            result.append(
                {
                    "name": name,
                    "owner_uid": info.st_uid,
                    "inode": info.st_ino,
                    "mode": oct(stat.S_IMODE(info.st_mode)),
                    "configuration": lines,
                    "truncated": len(text) > RESOLVER_LIMIT,
                }
            )
        except (OSError, ValueError) as error:
            result.append({"name": name, "error": type(error).__name__})
    return observation({"files": result, "omitted": max(0, len(candidates) - len(result))})


def collect() -> dict:
    started = time.monotonic()
    deadline = started + 15
    report = {"schema_version": 1, "generated_at": timestamp(), "sections": {}, "findings": []}
    sections = report["sections"]
    collectors = {
        "versions": versions,
        "artifacts": artifacts,
        "sessions": sessions,
    }
    if sys.platform == "darwin":
        for name, args in SYSTEM_COMMANDS.items():
            collectors[name] = lambda name=name, args=args: _sanitize_system(
                name, command(args, deadline)
            )
    else:
        sections["macos"] = observation(status="unavailable", code="unsupported-platform")
    pool = ThreadPoolExecutor(max_workers=4)
    try:
        futures = {pool.submit(function): name for name, function in collectors.items()}
        done, pending = wait(futures, timeout=max(0, deadline - time.monotonic()))
        for future in done:
            name = futures[future]
            try:
                sections[name] = future.result()
            except Exception as error:
                sections[name] = observation(status="failed", code=type(error).__name__)
        for future in pending:
            future.cancel()
            sections[futures[future]] = observation(status="failed", code="report-deadline")
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    sections["helper"] = _helper_observation(sections, deadline)
    if sys.platform == "darwin" and time.monotonic() < deadline:
        try:
            sections["resolver_files"] = _resolver_files(sections)
        except (OSError, ValueError, TypeError, KeyError) as error:
            sections["resolver_files"] = observation(status="failed", code=type(error).__name__)
        try:
            _network_details(report, deadline)
        except (OSError, ValueError, TypeError, KeyError) as error:
            sections["network_details"] = observation(status="failed", code=type(error).__name__)
        sections["processes"] = _processes(sections)
    report["duration_seconds"] = round(time.monotonic() - started, 3)
    report["truncated"] = False
    return bound_report(report)


def _helper_observation(sections: dict, deadline: float) -> dict:
    for item in (sections.get("sessions", {}).get("data") or {}).get("sessions", []):
        source = item.get("sources", {}).get("helper", {})
        data = source.get("data") or {}
        if (
            source.get("status") == "collected"
            and not source.get("stale", True)
            and data.get("session_id") == item["session"].get("session_id")
        ):
            return source
    if time.monotonic() + 1 > deadline:
        return observation(status="skipped", code="report-deadline")
    return helper()


def _route_network(destination: str) -> ipaddress.IPv4Network | None:
    if destination == "default" or ":" in destination:
        return None
    address, _, bits = destination.partition("/")
    pieces = address.split(".")
    if not 1 <= len(pieces) <= IPV4_OCTETS or any(not part.isdigit() for part in pieces):
        return None
    try:
        prefix = int(bits) if bits else len(pieces) * 8
        return ipaddress.IPv4Network(
            ".".join(pieces + ["0"] * (IPV4_OCTETS - len(pieces))) + f"/{prefix}", strict=False
        )
    except ValueError:
        return None


def _route_findings(report: dict, ranges: list) -> None:  # noqa: C901 - bounded route comparisons
    sections = report["sections"]
    rows = (sections.get("routes", {}).get("data") or {}).get("output", "").splitlines()
    known_interfaces = set()
    expected_interfaces = {}
    for item in (sections.get("sessions", {}).get("data") or {}).get("sessions", []):
        session = item["session"]
        helper_data = item.get("sources", {}).get("helper", {}).get("data") or {}
        if helper_data.get("session_id") != session.get("session_id"):
            continue
        units = {unit["unit"]: unit for unit in helper_data.get("units", [])}
        for vpc in session.get("vpcs", []):
            interface = units.get(vpc.get("unit"), {}).get("interface_name")
            if interface:
                expected_interfaces[(session["session_id"], vpc["identity"])] = interface
                known_interfaces.add(interface)
    direct = sections.get("helper", {}).get("data") or {}
    known_interfaces.update(unit.get("interface_name") for unit in direct.get("units", []))
    conflicts = []
    for row in rows[:2048]:
        columns = row.split()
        if len(columns) < ROUTE_MIN_COLUMNS:
            continue
        network = _route_network(columns[0])
        if network is None:
            continue
        interface = next(
            (part for part in columns[3:] if re.fullmatch(r"[a-z]+[0-9]+", part)), None
        )
        if not interface:
            continue
        # The helper requires these point-to-point addresses even without an active session.
        if interface not in known_interfaces and (
            network.overlaps(ipaddress.IPv4Network("192.0.2.1/32"))
            or network.overlaps(ipaddress.IPv4Network("192.0.2.2/32"))
        ):
            conflicts.append(
                {
                    "code": "helper-address-route-present",
                    "route": str(network),
                    "interface": interface,
                }
            )
        for session_id, unit, selected in ranges[:64]:
            if (
                network.overlaps(selected)
                and expected_interfaces.get((session_id, unit)) != interface
            ):
                conflicts.append(
                    {
                        "code": "selected-range-route-overlap",
                        "session": session_id,
                        "vpc": unit,
                        "range": str(selected),
                        "route": str(network),
                        "interface": interface,
                    }
                )
        if len(conflicts) >= MAX_ROUTE_FINDINGS:
            break
    report["findings"].extend(conflicts[:MAX_ROUTE_FINDINGS])


def _network_details(report: dict, deadline: float) -> None:  # noqa: C901 - bounded OS observations
    sections = report["sections"]
    ranges = []
    for item in (sections.get("sessions", {}).get("data") or {}).get("sessions", []):
        for vpc in item["session"].get("vpcs", []):
            ranges.extend(
                (item["session"]["session_id"], vpc["identity"], ipaddress.ip_network(cidr))
                for cidr in vpc.get("ranges", [])
            )
    _route_findings(report, ranges)
    lookups = []
    for session_id, vpc, network in ranges[:32]:
        if time.monotonic() >= deadline:
            break
        lookups.append(
            {
                "session": session_id,
                "vpc": vpc,
                "range": str(network),
                "observation": command(
                    ["/sbin/route", "-n", "get", str(network.network_address + 1)], deadline
                ),
            }
        )
    sections["selected_routes"] = observation(
        {"lookups": lookups, "omitted": max(0, len(ranges) - len(lookups))}
    )
    for index, first in enumerate(ranges):
        for second in ranges[index + 1 :]:
            if (
                first[:2] != second[:2]
                and first[2].overlaps(second[2])
                and len(report["findings"]) < MAX_FINDINGS
            ):
                report["findings"].append(
                    {"code": "selected-range-overlap", "ranges": [str(first[2]), str(second[2])]}
                )
    services = (
        (sections.get("network_services", {}).get("data") or {}).get("output", "").splitlines()[1:]
    )
    settings = []
    for service in services[:16]:
        if time.monotonic() >= deadline:
            break
        if service and not service.startswith("*"):
            settings.append(
                {
                    "service": service[:256],
                    "dns": command(
                        ["/usr/sbin/networksetup", "-getdnsservers", service], deadline
                    ),
                }
            )
    sections["network_service_dns"] = observation(
        {"services": settings, "omitted": max(0, len(services) - len(settings))}
    )


def _processes(sections: dict) -> dict:
    from stelvio.tunnel.processes import identity  # noqa: PLC0415

    pids = set()
    direct = sections.get("helper", {}).get("data") or {}
    if type(direct.get("pid")) is int:
        pids.add(direct["pid"])
    for item in (sections.get("sessions", {}).get("data") or {}).get("sessions", []):
        session = item["session"]
        pids.update(
            pid
            for pid in (session.get("runtime_pid"), session.get("forwarder_pid"))
            if type(pid) is int and pid > 0
        )
        pids.update(
            vpc["transport_pid"]
            for vpc in session.get("vpcs", [])
            if type(vpc.get("transport_pid")) is int and vpc["transport_pid"] > 0
        )
    values = []
    for pid in sorted(pids)[:64]:
        try:
            value = identity(pid)
            values.append({"pid": pid, "identity": asdict(value) if value else None})
        except (OSError, RuntimeError) as error:
            values.append({"pid": pid, "error": type(error).__name__})
    return observation(
        {
            "known_processes": values,
            "plugin": {"status": "unavailable", "reason": "owned-by-ssh-proxy"},
        }
    )


def bound_report(report: dict) -> dict:
    if len(json.dumps(report, allow_nan=False, indent=2).encode()) + 1 <= REPORT_LIMIT:
        return report
    report["truncated"] = True
    for name in sorted(report["sections"], reverse=True):
        section = report["sections"][name]
        section["data"] = None
        section["truncated"] = True
        if len(json.dumps(report, allow_nan=False, indent=2).encode()) + 1 <= REPORT_LIMIT:
            return report
    raise ValueError("Diagnostic report exceeds size bound")


def render(report: dict, console: Console) -> None:
    from rich.pretty import Pretty  # noqa: PLC0415
    from rich.table import Table  # noqa: PLC0415
    from rich.text import Text  # noqa: PLC0415

    console.print(Text("Stelvio tunnel diagnostics", style="bold"))
    console.print(
        Text(f"Generated {report['generated_at']} | {report['duration_seconds']} seconds")
    )
    console.print(Text("Local report: resource identities and network addresses are included."))
    for name, section in sorted(report["sections"].items()):
        table = Table(title=Text(name.replace("_", " ").title()), show_header=False)
        table.add_column("Field", overflow="fold")
        table.add_column("Value", overflow="fold")
        table.add_row("Status", Text(section["status"] + (" (stale)" if section["stale"] else "")))
        if section.get("error"):
            table.add_row("Error", Pretty(section["error"]))
        table.add_row("Data", Pretty(section["data"], max_string=4096))
        console.print(table)
    if report["findings"]:
        console.print(Pretty(report["findings"]))
