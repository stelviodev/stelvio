"""Independent read-only macOS inventory and normalization."""

from __future__ import annotations

import os
import platform
import stat
import subprocess
from pathlib import Path

from .store import digest

HELPER = Path("/Library/PrivilegedHelperTools/stelvio-traforo")
PLIST = Path("/Library/LaunchDaemons/dev.stelvio.tunnel.plist")
STATE = Path("/Library/Application Support/Stelvio/tunnel")


def output(*args):
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT, timeout=15)


def file_info(path):
    if not path.exists() and not path.is_symlink():
        return None
    info = path.lstat()
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"Unexpected non-regular host file: {path}")
    return {
        "digest": digest(path),
        "uid": info.st_uid,
        "mode": info.st_mode,
        "inode": info.st_ino,
        "device": info.st_dev,
        "links": info.st_nlink,
        "acl": output("/bin/ls", "-lde", str(path)).splitlines()[1:],
    }


def snapshot():
    if platform.system() != "Darwin" or os.geteuid() == 0:
        raise RuntimeError("Native acceptance requires macOS and an ordinary user")
    resolvers = Path("/etc/resolver")
    directory = HELPER.parent
    remnants = [
        str(p)
        for p in directory.iterdir()
        if "stelvio-traforo" in p.name
        and p not in {HELPER, directory / ".stelvio-traforo.admin.lock"}
    ]
    lock_path = directory / ".stelvio-traforo.admin.lock"
    lock = None
    if lock_path.exists() or lock_path.is_symlink():
        info = lock_path.lstat()
        lock = {
            "valid": stat.S_ISREG(info.st_mode)
            and info.st_uid == 0
            and stat.S_IMODE(info.st_mode) == 0o600
            and info.st_nlink == 1
            and not output("/bin/ls", "-lde", str(lock_path)).splitlines()[1:],
            "identity": [info.st_dev, info.st_ino],
        }
    launchd = subprocess.run(
        ["/bin/launchctl", "print", "system/dev.stelvio.tunnel"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    return {
        "administration_lock": lock,
        "launchd": {"exit_code": launchd.returncode, "output": launchd.stdout + launchd.stderr},
        "os": platform.mac_ver()[0],
        "architecture": platform.machine(),
        "interfaces": output("/sbin/ifconfig", "-l").split(),
        "routes": output("/usr/sbin/netstat", "-rn", "-f", "inet"),
        "dns": output("/usr/sbin/scutil", "--dns"),
        "resolvers": {str(p): file_info(p) for p in sorted(resolvers.iterdir())}
        if resolvers.exists()
        else {},
        "processes": output("/bin/ps", "-axo", "pid,ppid,uid,comm"),
        "helper": file_info(HELPER),
        "service": file_info(PLIST),
        "state_exists": STATE.exists(),
        "administration_remnants": remnants,
    }


def stable_dns(text):
    return [
        "resolver" if line.startswith("resolver #") else line
        for raw in text.splitlines()
        if (line := raw.strip()) and not line.startswith("reach ")
    ]


def stable_routes(text):
    return sorted(
        [
            row[:4]
            for raw in text.splitlines()
            if len(row := raw.split()) >= 4
            and row[0] != "Destination"
            and not any(flag in row[2] for flag in "WL")
        ]
    )


def network_processes(text):
    return {
        (row[0], row[3])
        for raw in text.splitlines()[1:]
        if len(row := raw.split(maxsplit=3)) == 4
        and row[2] != "0"
        and Path(row[3]).name in {"ssh", "session-manager-plugin", "stelvio-traforo", "traforo"}
    }


def compare(before, after):
    def installed(value):
        return (
            {key: item for key, item in value.items() if key not in {"inode", "device"}}
            if value
            else None
        )

    return {
        "interfaces": sorted(before["interfaces"]) == sorted(after["interfaces"]),
        "routes": stable_routes(before["routes"]) == stable_routes(after["routes"]),
        "dns": stable_dns(before["dns"]) == stable_dns(after["dns"]),
        "resolvers": before["resolvers"] == after["resolvers"],
        "helper": installed(before["helper"]) == installed(after["helper"]),
        "service": installed(before["service"]) == installed(after["service"]),
        "processes": network_processes(after["processes"])
        <= network_processes(before["processes"]),
        "administration": not after["administration_remnants"],
        "administration_lock": (
            after.get("administration_lock", {}).get("valid", False)
            and (
                not before.get("administration_lock")
                or before["administration_lock"]["identity"]
                == after["administration_lock"]["identity"]
            )
        )
        if after.get("administration_lock")
        else not before.get("administration_lock"),
        "launchd": (
            ("state = running" in after.get("launchd", {}).get("output", ""))
            if before["service"]
            else after.get("launchd", {}).get("exit_code", 1) != 0
        ),
        "state_namespace": before["state_exists"] == after["state_exists"],
    }
