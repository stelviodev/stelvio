"""Exclusive, reversible actual-host test administration. Never used by unit tests."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import platform
import re
import stat
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
            state = NativeHelper().status()
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
    raise AssertionError(f"Host cleanup not certified: {NativeHelper().status()}")


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


def _acl_free(path):
    output = run("/bin/ls", "-lde", str(path)).stdout
    first = output.splitlines()[0].split(maxsplit=1)[0]
    return not first.endswith("+")


def inspect_administration_artifacts(directory=None, *, root_owner=None, acl_free=None):
    """Validate the permanent admin lock and report known transaction remnants.

    The lock is intentionally persistent: unlinking it could let concurrent
    administrators lock different inodes. The callbacks keep ownership and ACL
    checks testable without root in ordinary unit tests.
    """
    directory = Path(directory or installation.ADMIN_LOCK.parent)
    if not directory.exists() and not directory.is_symlink():
        return ()
    owner_check = root_owner or (lambda info: info.st_uid == 0)
    acl_check = acl_free or _acl_free
    try:
        directory_info = directory.lstat()
    except OSError as error:
        raise AssertionError(
            f"Cannot inspect Traforo administration directory: {directory}"
        ) from error
    if (
        not stat.S_ISDIR(directory_info.st_mode)
        or not owner_check(directory_info)
        or directory_info.st_mode & 0o022
        or not acl_check(directory)
    ):
        raise AssertionError("Traforo administration directory is unsafe")

    def validate_file(path, *, mode=None, links=(1,)):
        try:
            info = path.lstat()
        except OSError as error:
            raise AssertionError(
                f"Cannot inspect Traforo administration artifact: {path}"
            ) from error
        if (
            not stat.S_ISREG(info.st_mode)
            or not owner_check(info)
            or (mode is not None and stat.S_IMODE(info.st_mode) != mode)
            or (mode is None and info.st_mode & 0o022)
            or info.st_nlink not in links
            or not acl_check(path)
        ):
            raise AssertionError(f"Traforo administration artifact is unsafe: {path.name}")

    names = sorted(
        entry.name for entry in directory.iterdir() if entry.name.startswith(".stelvio-traforo.")
    )
    remnants = []
    for name in names:
        path = directory / name
        if name == installation.ADMIN_LOCK.name:
            validate_file(path, mode=0o600)
        elif name == ".stelvio-traforo.retired" or re.fullmatch(
            r"\.stelvio-traforo\.bootstrap\.[0-9a-f]{64}", name
        ):
            validate_file(path, links=(1, 2))
            remnants.append(path)
        else:
            raise AssertionError(f"Unknown Traforo administration artifact: {name}")
    return tuple(remnants)


class MacOSHost:
    def __init__(self, directory):
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.original = None
        self.baseline = None
        self.lock = None
        self.selection = None
        self.admin_lock_identity = None

    def assert_admin_lock_stable(self, *, allow_transaction_remnants=False):
        artifacts = inspect_administration_artifacts()
        if not allow_transaction_remnants:
            assert not artifacts, (
                "Retain and recover interrupted administration before testing: "
                + ", ".join(path.name for path in artifacts)
            )
        lock = installation.ADMIN_LOCK
        if not lock.exists() and not lock.is_symlink():
            assert self.admin_lock_identity is None, "Persistent Traforo admin lock disappeared"
            return None
        info = lock.lstat()
        identity = info.st_dev, info.st_ino
        if self.admin_lock_identity is None:
            self.admin_lock_identity = identity
        else:
            assert identity == self.admin_lock_identity, (
                "Persistent Traforo admin lock inode changed"
            )
        return identity

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
        remnants = inspect_administration_artifacts()
        assert not remnants, "Retain and recover interrupted administration before testing"
        self.assert_admin_lock_stable()
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
        self.assert_admin_lock_stable()
        wait_empty()

    def save(self, name, value):
        (self.directory / f"{name}.json").write_text(json.dumps(value, indent=2))

    def admin(self, script):
        try:
            installation._authorize(script)
        finally:
            self.assert_admin_lock_stable(allow_transaction_remnants=True)

    def install(self, image):
        digest = hashlib.sha256(image.read_bytes()).hexdigest()
        self.admin(installation._bootstrap(image, digest))
        installation._wait_ready()
        wait_empty()
        assert installation._installed_digest() == digest

    def assert_restored(self, name):
        self.assert_admin_lock_stable()
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
            self.assert_admin_lock_stable()
            assert not installation.INSTALLED_HELPER.exists()
            assert not Path("/Library/Application Support/Stelvio/tunnel").exists()
            assert not Path("/Library/LaunchDaemons/dev.stelvio.tunnel.plist").exists()
        else:
            if installation._installed_digest() != self.original_digest:
                self.install(self.original)
            assert installation._installed_digest() == self.original_digest
        final = inventory()
        self.assert_admin_lock_stable()
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
    assert len(NativeHelper().status().units)==2
    Path(sys.argv[1]).write_text(json.dumps({'pid':os.getpid(),'owner':owner}))
    sys.stdin.read()
    assert len(NativeHelper().status().units)==2
finally:
    lease.close()
"""
