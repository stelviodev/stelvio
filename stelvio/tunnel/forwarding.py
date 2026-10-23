"""Own the installed nonroot carrier process and acknowledge stack changes."""

from __future__ import annotations

import json
import os
import select
import socket
import subprocess
import time
from contextlib import contextmanager
from threading import Lock
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from stelvio.tunnel.manifest import VpcNetwork

MAX_REPLY = 32768
CONTROL_VERSION = 2
MIN_PORT = 1024
MAX_PORT = 65535
UNIT_HEX_LENGTH = 8


@contextmanager
def packaged_forwarder() -> Iterator[Path]:
    """Both roles use the exact verified root-owned installed Traforo image."""
    from stelvio.tunnel.assets import (  # noqa: PLC0415 - lazy platform assets
        select_traforo,
        validate_version,
    )
    from stelvio.tunnel.installation import (  # noqa: PLC0415 - lazy platform installation
        INSTALLED_HELPER,
        _installed_digest,
    )

    selected = select_traforo()
    if _installed_digest() != selected.digest:
        raise RuntimeError("The installed Traforo differs; run stlv tunnel install")
    validate_version(INSTALLED_HELPER, allow_development=selected.local_path is not None)
    yield INSTALLED_HELPER


class Forwarding:
    def __init__(self, binary: Path, carrier: socket.socket) -> None:
        if not os.geteuid():
            raise RuntimeError("Forwarding must run without root privileges")
        self._lock = Lock()
        self._next_id = 0
        self._diagnostics_lock = Lock()
        self._diagnostics, diagnostics_child = socket.socketpair()
        environment = dict(os.environ, STLV_TRAFORO_DIAGNOSTICS_FD=str(diagnostics_child.fileno()))
        try:
            self.process = subprocess.Popen(  # noqa: S603 - verified packaged nonroot artifact
                [str(binary), "forwarder", str(carrier.fileno())],
                pass_fds=(carrier.fileno(), diagnostics_child.fileno()),
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except BaseException:
            self._diagnostics.close()
            raise
        finally:
            diagnostics_child.close()
        self._diagnostics.settimeout(0.5)
        try:
            hello = self._reply()
            if hello.get("version") != CONTROL_VERSION or hello.get("status") != "ready":
                raise RuntimeError("Nonroot forwarder failed to start")  # noqa: TRY301
            if hello.get("diagnostics") != 1:
                self._diagnostics.close()
        except BaseException:
            self.close()
            raise

    def _reply(self, *, deadline: float | None = None) -> dict:
        result = bytearray()
        deadline = deadline or time.monotonic() + 5
        while len(result) < MAX_REPLY:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([self.process.stdout], [], [], remaining)[0]:
                raise TimeoutError("Nonroot forwarder acknowledgement timed out")
            byte = os.read(self.process.stdout.fileno(), 1)
            if not byte:
                raise ConnectionError("Nonroot forwarder exited")
            result.extend(byte)
            if byte == b"\n":
                try:
                    message = json.loads(result)
                except (ValueError, UnicodeError):
                    raise ConnectionError("Invalid forwarder reply") from None
                if not isinstance(message, dict):
                    raise ConnectionError("Invalid forwarder reply")
                return message
        raise ConnectionError("Nonroot forwarder returned invalid acknowledgement")

    def _command(self, data: dict) -> dict:
        with self._lock:
            self._next_id += 1
            request = dict(data, version=CONTROL_VERSION, id=self._next_id)
            wire = json.dumps(request, separators=(",", ":")).encode() + b"\n"
            if len(wire) > MAX_REPLY:
                raise ValueError("Forwarder command exceeds size bound")
            deadline = time.monotonic() + 10
            try:
                remaining = memoryview(wire)
                while remaining:
                    wait = deadline - time.monotonic()
                    if wait <= 0 or not select.select([], [self.process.stdin], [], wait)[1]:
                        raise TimeoutError("Forwarder command timed out")
                    written = os.write(self.process.stdin.fileno(), remaining[:4096])
                    if not written:
                        raise ConnectionError("Forwarder input closed")  # noqa: TRY301
                    remaining = remaining[written:]
                reply = self._reply(deadline=deadline)
                if (
                    reply.get("version") != CONTROL_VERSION
                    or type(reply.get("id")) is not int
                    or reply.get("id") != self._next_id
                ):
                    raise ConnectionError("Forwarder reply identity differs")  # noqa: TRY301
                if reply.get("status") not in ("ok", "error"):
                    raise ConnectionError("Invalid forwarder reply status")  # noqa: TRY301
            except (OSError, ConnectionError, ValueError):
                # An uncertain exchange poisons this channel. Never consume an old
                # reply as another worker's acknowledgement.
                self.process.kill()
                raise
            if reply["status"] == "error":
                if reply.get("code") == "authorization":
                    raise ValueError(
                        "Private DNS returned a resource outside the selected destination subnets"
                    )
                raise ConnectionError("Forwarder rejected operation: " + str(reply.get("code")))
            return reply

    @staticmethod
    def _identity(unit: str, generation: int, attempt: int, revision: int) -> dict:
        if (
            not isinstance(unit, str)
            or len(unit) != UNIT_HEX_LENGTH
            or any(character not in "0123456789abcdef" for character in unit)
            or int(unit, 16) == 0
            or type(generation) is not int
            or not 1 <= generation <= (1 << 64) - 1
            or type(attempt) is not int
            or not 1 <= attempt <= (1 << 64) - 1
            or type(revision) is not int
            or not 0 <= revision <= (1 << 64) - 1
        ):
            raise ValueError("Invalid forwarder unit, generation, attempt, or revision")
        return {
            "unit": int(unit, 16),
            "generation": generation,
            "attempt": attempt,
            "revision": revision,
        }

    def configure_dns(  # noqa: PLR0913 - the control carries four independent fencing values
        self,
        unit: str,
        generation: int,
        attempt: int,
        revision: int,
        network: VpcNetwork,
        hostnames: tuple[str, ...],
        *,
        probe_domain: str | None = None,
    ) -> int:
        from ipaddress import IPv4Address  # noqa: PLC0415 - DNS control boundary

        if type(revision) is not int or revision < 1:
            raise ValueError("DNS configuration requires a positive revision")
        domains = tuple(
            dict.fromkeys((*network.dns_domains, *((probe_domain,) if probe_domain else ())))
        )
        reply = self._command(
            dict(
                self._identity(unit, generation, attempt, revision),
                operation="dns-configure",
                resolver=str(IPv4Address(int(network.cidrs[0].network_address) + 2)),
                domains=list(domains),
                hostnames=[name for name in hostnames if name not in domains],
            )
        )
        port = reply.get("port")
        if type(port) is not int or not MIN_PORT <= port <= MAX_PORT:
            raise ConnectionError("Invalid DNS listener port")
        return port

    def dns(
        self,
        operation: str,
        unit: str,
        generation: int,
        attempt: int,
        revision: int,
        **options: object,
    ) -> dict:
        if not isinstance(operation, str) or operation not in {
            "reject",
            "retire",
            "probe",
            "arm",
            "check",
            "release",
        }:
            raise ValueError("Unknown DNS control operation")
        if type(revision) is not int or revision < 1:
            raise ValueError("DNS control requires a positive revision")
        return self._command(
            dict(
                self._identity(unit, generation, attempt, revision),
                operation="dns-" + operation,
                **options,
            )
        )

    def add(
        self, unit: str, host_generation: int, attempt: int, network: VpcNetwork, port: int
    ) -> None:
        if type(port) is not int or not MIN_PORT <= port <= MAX_PORT:
            raise ValueError("Forwarder SOCKS proxy requires an unprivileged port")
        if not network.route_cidrs:
            raise ValueError("VPC has no private or isolated subnet destinations")
        self._command(
            dict(
                self._identity(unit, host_generation, attempt, 0),
                operation="add",
                proxy=f"127.0.0.1:{port}",
                cidrs=[str(cidr) for cidr in network.route_cidrs],
            )
        )

    def remove(self, unit: str, host_generation: int, attempt: int) -> None:
        self._command(
            dict(
                self._identity(unit, host_generation, attempt, 0),
                operation="remove",
            )
        )

    def diagnostics(self) -> dict:
        from stelvio.tunnel.diagnostics.protocol import (  # noqa: PLC0415
            SOURCE_LIMIT,
            receive,
            request,
            send,
            validate_snapshot,
        )

        with self._diagnostics_lock:
            try:
                self._diagnostics.settimeout(0.5)
                send(self._diagnostics, request(), SOURCE_LIMIT)
                return validate_snapshot(receive(self._diagnostics, SOURCE_LIMIT))
            except (OSError, EOFError, ValueError, TypeError):
                self._diagnostics.close()
                raise

    def healthy(self) -> bool:
        return self.process.poll() is None

    def close(self) -> None:
        self._diagnostics.close()
        if self.process.stdin:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        if self.process.stdout:
            self.process.stdout.close()
