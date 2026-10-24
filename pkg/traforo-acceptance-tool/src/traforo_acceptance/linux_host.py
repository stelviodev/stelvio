"""Independent Debian acceptance inventory; never imports candidate host adapters."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

from .store import digest

HELPER = Path("/usr/local/libexec/stelvio-traforo")
SERVICE = Path("/etc/systemd/system/stelvio-traforo.service")
STATE = Path("/var/lib/stelvio/tunnel")


def output(*args):
    return subprocess.check_output(args, text=True, timeout=15)


def file_info(path):
    if not path.exists() and not path.is_symlink():
        return None
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid or info.st_mode & 0o022:
        raise RuntimeError(f"Unsafe host artifact: {path}")
    return {
        "digest": digest(path),
        "uid": info.st_uid,
        "mode": info.st_mode,
        "inode": info.st_ino,
        "device": info.st_dev,
        "links": info.st_nlink,
        "acl": output("/usr/bin/getfacl", "-cp", str(path)),
    }


def snapshot():
    if os.geteuid() == 0:
        raise RuntimeError("Run Linux acceptance as the ordinary user")
    status = subprocess.run(
        ["/usr/bin/systemctl", "is-active", "stelvio-traforo.service"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    links = json.loads(output("/usr/sbin/ip", "-j", "link", "show"))
    return {
        "platform": "linux",
        "os": Path("/etc/os-release").read_text(),
        "architecture": os.uname().machine,
        "interfaces": sorted((row["ifindex"], row["ifname"]) for row in links),
        "routes": json.loads(output("/usr/sbin/ip", "-j", "-4", "route", "show", "table", "all")),
        "dns": {
            name: json.loads(
                output(
                    "/usr/bin/busctl",
                    "--json=short",
                    "get-property",
                    "org.freedesktop.resolve1",
                    "/org/freedesktop/resolve1",
                    "org.freedesktop.resolve1.Manager",
                    name,
                )
            )["data"]
            for name in ("DNSEx", "Domains", "FallbackDNSEx")
        },
        "resolvers": {},
        "resolv_conf": str(Path("/etc/resolv.conf").resolve()),
        "processes": output("/bin/ps", "-axo", "pid,ppid,uid,comm"),
        "helper": file_info(HELPER),
        "service": file_info(SERVICE),
        "systemd": {"exit_code": status.returncode, "output": status.stdout},
        "state_exists": (STATE / "installation.json").exists() or (STATE / "journal.json").exists(),
        "administration_remnants": [str(path) for path in HELPER.parent.glob(".stelvio-traforo.*")],
    }


def compare(before, after):
    from .host import network_processes

    def image(value):
        return (
            {key: item for key, item in value.items() if key not in {"inode", "device"}}
            if value
            else None
        )

    return {
        "interfaces": before["interfaces"] == after["interfaces"],
        "routes": sorted(json.dumps(row, sort_keys=True) for row in before["routes"])
        == sorted(json.dumps(row, sort_keys=True) for row in after["routes"]),
        "dns": before["dns"] == after["dns"],
        "resolv_conf": before["resolv_conf"] == after["resolv_conf"],
        "helper": image(before["helper"]) == image(after["helper"]),
        "service": image(before["service"]) == image(after["service"]),
        "processes": network_processes(after["processes"])
        <= network_processes(before["processes"]),
        "administration": not after["administration_remnants"],
        "systemd": after["systemd"]["output"].strip() == "active"
        if before["service"]
        else after["systemd"]["exit_code"] != 0,
        "state_namespace": before["state_exists"] == after["state_exists"],
    }
