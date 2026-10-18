"""Exclusive, reversible actual-host test administration. Never used by unit tests."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import platform
import subprocess
import time
from pathlib import Path

from stelvio.tunnel import installation
from stelvio.tunnel.assets import select_traforo
from stelvio.tunnel.helper_client import HelperBusyError, NativeHelper


def run(*args, **kwargs):
    kwargs.setdefault("timeout", 30)
    return subprocess.run(  # noqa: S603 - explicit test-owned executable arguments
        args, capture_output=True, text=True, check=True, **kwargs
    )


def wait_empty(timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            state = NativeHelper().inspect()
        except HelperBusyError:
            time.sleep(0.1)
            continue
        except OSError as error:
            if error.errno not in {
                errno.EPIPE,
                errno.ENOTCONN,
                errno.ECONNRESET,
                errno.ECONNREFUSED,
                errno.ENOENT,
            }:
                raise
            time.sleep(0.1)
            continue
        if not (state.owned or state.uncertain or state.mutating or state.units):
            return
        time.sleep(0.1)
    raise AssertionError(f"Host cleanup not certified: {NativeHelper().inspect()}")


def cleanup_when_idle(timeout=30):
    deadline = time.monotonic() + timeout
    while True:
        try:
            NativeHelper().reconcile()
        except HelperBusyError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.1)
        else:
            return


def inventory():
    directory = Path("/etc/resolver")
    return {
        "host": {"os": platform.mac_ver()[0], "architecture": platform.machine()},
        "interfaces": run("/sbin/ifconfig", "-l").stdout.split(),
        "routes": run("/usr/sbin/netstat", "-rn", "-f", "inet").stdout,
        "dns": run("/usr/sbin/scutil", "--dns").stdout,
        "resolvers": {
            str(p): {
                "inode": p.lstat().st_ino,
                "device": p.lstat().st_dev,
                "mode": p.lstat().st_mode,
                "uid": p.lstat().st_uid,
                "digest": hashlib.sha256(p.read_bytes()).hexdigest(),
            }
            for p in directory.iterdir()
        }
        if directory.exists()
        else {},
        "processes": run("/bin/ps", "-axo", "pid,ppid,uid,comm").stdout,
        "service": service_inventory(),
    }


def service_inventory():
    path = Path("/Library/LaunchDaemons/dev.stelvio.tunnel.plist")
    if not path.exists():
        return None
    status = run("/bin/launchctl", "print", "system/dev.stelvio.tunnel")
    assert "state = running" in status.stdout
    info = path.lstat()
    return {
        "digest": hashlib.sha256(path.read_bytes()).hexdigest(),
        "mode": info.st_mode,
        "uid": info.st_uid,
    }


def stable_routes(text):
    # ARP/neighbor and cloned socket routes fluctuate with unrelated traffic.
    # Compare all other route destinations/gateways/interfaces, including AWS CIDRs.
    rows = [line.split() for line in text.splitlines()]
    return {
        tuple(row[:4])
        for row in rows
        if len(row) >= 4 and row[0] != "Destination" and "W" not in row[2] and "L" not in row[2]
    }


def stable_dns(text):
    """Keep resolver ordering/scopes, ignoring only labels and live reachability."""
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("reach "):
            continue
        if line.startswith("resolver #"):
            line = "resolver"
        lines.append(line)
    return lines


def network_processes(text):
    rows = [line.split(maxsplit=3) for line in text.splitlines()[1:]]
    return {
        (row[0], row[3])
        for row in rows
        if len(row) == 4
        and row[2] != "0"
        and Path(row[3]).name in {"ssh", "session-manager-plugin", "stelvio-traforo", "traforo"}
    }


class MacOSHost:
    def __init__(self, directory):
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.original = None
        self.baseline = None
        self.lock = None
        self.selection = None

    def prepare(self):
        import fcntl

        # Lock a stable root-owned directory, shared by every UID. Administration
        # locks PrivilegedHelperTools separately; use LaunchDaemons to avoid nesting.
        fd = os.open("/Library/LaunchDaemons", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        self.lock = fd
        info = os.fstat(fd)
        assert info.st_uid == 0
        assert not info.st_mode & 0o022
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert not Path("/Library/PrivilegedHelperTools/dev.stelvio.tunnel").exists(), (
            "Migrate legacy installations before this lane"
        )
        assert not list(Path("/Library/PrivilegedHelperTools").glob(".stelvio-traforo.*")), (
            "Retain and recover interrupted administration before testing"
        )
        if installation.INSTALLED_HELPER.exists():
            installation._installed_digest()  # Validate ownership before preserving anything.
            wait_empty(timeout=1)
            self.original = self.directory / "original-traforo"
            self.original.write_bytes(installation.INSTALLED_HELPER.read_bytes())
            self.original.chmod(0o700)
            self.original_digest = hashlib.sha256(self.original.read_bytes()).hexdigest()
            run(
                "/usr/bin/xattr",
                "-p",
                "dev.stelvio.traforo.installation",
                str(installation.INSTALLED_HELPER),
            )
        else:
            assert not Path("/Library/Application Support/Stelvio/tunnel").exists(), (
                "Uncertain orphaned state must be recovered first"
            )
            assert not Path("/Library/LaunchDaemons/dev.stelvio.tunnel.plist").exists()
        self.baseline = inventory()
        self.save("baseline", self.baseline)
        self.selection = select_traforo(os.environ)
        installation.install_helper()
        wait_empty()

    def save(self, name, value):
        (self.directory / f"{name}.json").write_text(json.dumps(value, indent=2))

    def admin(self, script):
        installation._authorize(script)

    def install(self, image):
        digest = hashlib.sha256(image.read_bytes()).hexdigest()
        self.admin(installation._bootstrap(image, digest))
        installation._wait_ready()
        wait_empty()
        assert installation._installed_digest() == digest

    def assert_restored(self, name):
        if installation.INSTALLED_HELPER.exists():
            wait_empty()
        else:
            assert not Path("/Library/Application Support/Stelvio/tunnel").exists()
            assert not Path("/Library/LaunchDaemons/dev.stelvio.tunnel.plist").exists()
        current = inventory()
        deadline = time.monotonic() + 10
        while stable_dns(current["dns"]) != stable_dns(self.baseline["dns"]):
            self.save(name + "-dns-pending", {"dns": current["dns"]})
            if time.monotonic() >= deadline:
                break
            time.sleep(0.1)
            current["dns"] = run("/usr/sbin/scutil", "--dns").stdout
        self.save(name, current)
        assert current["resolvers"] == self.baseline["resolvers"]
        # Compare actual interface identities; an owned utun must not survive.
        assert set(current["interfaces"]) == set(self.baseline["interfaces"])
        assert stable_routes(current["routes"]) == stable_routes(self.baseline["routes"])
        assert stable_dns(current["dns"]) == stable_dns(self.baseline["dns"])
        before = network_processes(self.baseline["processes"])
        assert network_processes(current["processes"]) <= before, (
            "New networking process survived test cleanup"
        )

    def restore(self):
        if self.baseline is None:
            return
        self.assert_restored("before-restoring-installation")
        if self.original is None:
            installation.uninstall_helper()
            assert not installation.INSTALLED_HELPER.exists()
            assert not Path("/Library/Application Support/Stelvio/tunnel").exists()
            assert not Path("/Library/LaunchDaemons/dev.stelvio.tunnel.plist").exists()
        else:
            if installation._installed_digest() != self.original_digest:
                self.install(self.original)
            assert installation._installed_digest() == self.original_digest
        final = inventory()
        self.save("final", final)
        assert final["service"] == self.baseline["service"]
        assert stable_routes(final["routes"]) == stable_routes(self.baseline["routes"])
        assert stable_dns(final["dns"]) == stable_dns(self.baseline["dns"])

    def close(self):
        if self.lock is not None:
            os.close(self.lock)
            self.lock = None


# Executed as an ordinary-user isolated process. Its parent is part of the
# authenticated helper ownership, allowing frozen-orphan tests without shell death.
HOLDER = """import json,os,sys,time
from pathlib import Path
from uuid import uuid4
from stelvio.tunnel.helper_client import HelperBusyError, NativeHelper
from stelvio.tunnel.helper_protocol import ResolverEndpoint
owner=str(uuid4())
lease=NativeHelper().acquire(owner)
try:
    for i,octet in enumerate((254,253),start=1):
        lease.configure(unit=f'{i:08x}',generation=1,vpc_id=f'vpc-{i:08x}',
            cidrs=(f'10.{octet}.0.0/16',),resolvers=(ResolverEndpoint(f'{owner.replace("-","")}-{i}.invalid',65130),))
    assert len(NativeHelper().inspect().units)==2
    Path(sys.argv[1]).write_text(json.dumps({'pid':os.getpid(),'owner':owner}))
    sys.stdin.read()
finally:
    lease.close()
"""
