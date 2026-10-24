"""Exclusive Debian host fixture with independent kernel/resolver baselines."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path

from stelvio.tunnel import installation
from stelvio.tunnel.helper_client import NativeHelper
from stelvio.tunnel.linux_installation import authorize, bootstrap


def run(*args):
    return subprocess.check_output(args, text=True, timeout=15)  # noqa: S603 - fixed host inspection commands


def inventory():
    return {
        "interfaces": json.loads(run("/usr/sbin/ip", "-j", "link", "show")),
        "routes": json.loads(run("/usr/sbin/ip", "-j", "-4", "route", "show", "table", "all")),
        "dns": {
            prop: json.loads(
                run(
                    "/usr/bin/busctl",
                    "--json=short",
                    "get-property",
                    "org.freedesktop.resolve1",
                    "/org/freedesktop/resolve1",
                    "org.freedesktop.resolve1.Manager",
                    prop,
                )
            )["data"]
            for prop in ("DNSEx", "Domains", "FallbackDNSEx")
        },
        "processes": run("/bin/ps", "-axo", "pid=,ppid=,uid=,comm="),
        "resolv_conf": str(Path("/etc/resolv.conf").resolve()),
    }


def stable(value):
    # IPv6 autoconfiguration and traffic counters are not owned configuration.
    return {
        "interfaces": sorted((row["ifindex"], row["ifname"]) for row in value["interfaces"]),
        "routes": sorted(json.dumps(row, sort_keys=True) for row in value["routes"]),
        "dns": value["dns"],
        "resolv_conf": value["resolv_conf"],
    }


def wait_empty(timeout=30):
    deadline = time.monotonic() + timeout
    while True:
        state = NativeHelper(timeout=2).status()
        if not (state.owned or state.uncertain or state.mutating or state.units):
            return
        if time.monotonic() >= deadline:
            raise AssertionError(f"Linux cleanup not certified: {state}")
        time.sleep(0.1)


class LinuxHost:
    def __init__(self, directory):
        self.directory = directory
        self.lock = None
        self.original = None
        self.original_digest = None
        self.baseline = None

    def prepare(self):
        # A stable root-owned inode gives all UIDs the same exclusive test gate.
        self.lock = os.open("/etc/systemd/system", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if installation.INSTALLED_HELPER.exists():
            self.original_digest = installation._installed_digest()
            wait_empty(timeout=2)
            self.original = self.directory / "original-traforo"
            self.original.write_bytes(installation.INSTALLED_HELPER.read_bytes())
            self.original.chmod(0o700)
        self.baseline = inventory()
        self.save("baseline", self.baseline)
        installation.install_helper()
        wait_empty()

    def save(self, name, value):
        (self.directory / f"{name}.json").write_text(json.dumps(value, indent=2))

    def admin(self, script):
        authorize(script)

    def install(self, image):
        checksum = hashlib.sha256(image.read_bytes()).hexdigest()
        authorize(bootstrap(image, checksum))
        installation._wait_ready()
        wait_empty()

    def assert_restored(self, name):
        if installation.INSTALLED_HELPER.exists():
            wait_empty()
        current = inventory()
        self.save(name, current)
        assert stable(current) == stable(self.baseline), "Linux host configuration changed"
        before = network_processes(self.baseline["processes"])
        assert network_processes(current["processes"]) <= before, (
            "Networking child survived cleanup"
        )

    def restore(self):
        if self.baseline is None:
            return
        self.assert_restored("before-installation-restoration")
        if self.original is None:
            installation.uninstall_helper()
        elif installation._installed_digest() != self.original_digest:
            self.install(self.original)
        self.save("final", inventory())

    def close(self):
        if self.lock is not None:
            os.close(self.lock)
            self.lock = None


def network_processes(text):
    rows = [line.split(maxsplit=3) for line in text.splitlines()]
    return {
        (row[0], row[3])
        for row in rows
        if len(row) == 4
        and row[2] != "0"
        and Path(row[3]).name in {"ssh", "session-manager-plugin", "stelvio-traforo", "traforo"}
    }


def route_to(address):
    from ipaddress import IPv4Network

    rows = json.loads(run("/usr/sbin/ip", "-j", "-4", "route", "get", "fibmatch", str(address)))
    assert len(rows) == 1, rows
    assert rows[0].get("dev"), rows
    network = IPv4Network("0.0.0.0/0" if rows[0].get("dst") == "default" else rows[0]["dst"])
    return {
        "interface": rows[0]["dev"],
        "destination": str(network.network_address),
        "mask": str(network.netmask),
    }


def resolver_ports(domains):
    def prop(name):
        return json.loads(
            run(
                "/usr/bin/busctl",
                "--json=short",
                "get-property",
                "org.freedesktop.resolve1",
                "/org/freedesktop/resolve1",
                "org.freedesktop.resolve1.Manager",
                name,
            )
        )["data"]

    servers = prop("DNSEx")
    result = {}
    for index, name, route_only in prop("Domains"):
        if name not in domains:
            continue
        ports = {row[3] or 53 for row in servers if row[0] == index}
        assert route_only, name
        assert len(ports) == 1, (name, ports)
        result[name] = ports.pop()
    return result
