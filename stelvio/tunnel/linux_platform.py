"""Linux host paths and executable checks; importing this module has no side effects."""

from __future__ import annotations

import errno
import os
import platform
import stat
import struct
from pathlib import Path

INSTALLED_HELPER = Path("/usr/local/libexec/stelvio-traforo")
STATE_DIRECTORY = Path("/var/lib/stelvio/tunnel")
RUNTIME_DIRECTORY = Path("/run/stelvio/tunnel")
CONTROL_SOCKET = RUNTIME_DIRECTORY / "helper.sock"
SERVICE = Path("/etc/systemd/system/stelvio-traforo.service")
ELF_HEADER_SIZE = 64
ELF_PROGRAM_HEADER_SIZE = 56
MAX_PROGRAM_HEADERS = 128
ELF_MACHINES = {"arm64": 183, "x86_64": 62}


def supported(*, activation: bool = True) -> bool:
    """The first supported host is Debian 13 with resolved and systemd."""
    try:
        release = platform.freedesktop_os_release()
    except OSError:
        return False
    return (
        release.get("ID") == "debian"
        and release.get("VERSION_ID") == "13"
        and Path("/run/systemd/system").is_dir()
        and (
            not activation
            or (
                Path("/run/dbus/system_bus_socket").exists()
                and Path("/etc/resolv.conf").resolve()
                == Path("/run/systemd/resolve/stub-resolv.conf")
            )
        )
    )


def validate_elf(data: bytes, architecture: str, *, limit: int) -> None:
    """Require a bounded little-endian ELF64 executable with loadable code."""
    if not ELF_HEADER_SIZE <= len(data) <= limit or data[:7] != b"\x7fELF\x02\x01\x01":
        raise ValueError("Not a little-endian ELF64 executable")
    kind, machine, version, entry, offset, _, _, size, entry_size, count = struct.unpack_from(
        "<HHIQQQIHHH", data, 16
    )
    if (
        kind not in {2, 3}
        or machine != ELF_MACHINES[architecture]
        or version != 1
        or not entry
        or size != ELF_HEADER_SIZE
        or entry_size != ELF_PROGRAM_HEADER_SIZE
        or not 1 <= count <= MAX_PROGRAM_HEADERS
        or offset < ELF_HEADER_SIZE
        or offset + count * entry_size > len(data)
    ):
        raise ValueError("ELF executable has incompatible architecture or headers")
    executable = False
    for index in range(count):
        segment, flags, start, address, _, file_size, memory_size, _ = struct.unpack_from(
            "<IIQQQQQQ", data, offset + index * entry_size
        )
        if start + file_size > len(data) or file_size > memory_size:
            raise ValueError("ELF segment exceeds executable bounds")
        if segment == 1 and flags & 1 and address <= entry < address + memory_size:
            executable = True
    if not executable:
        raise ValueError("ELF entry point is outside executable load segments")


def no_acl(path: Path) -> None:
    for name in ("system.posix_acl_access", "system.posix_acl_default"):
        try:
            os.getxattr(path, name, follow_symlinks=False)
        except OSError as error:
            if error.errno not in {errno.ENODATA, errno.ENOTSUP}:
                raise
        else:
            raise ValueError("Extended POSIX ACL is not supported for trusted paths")


def root_paths(paths: tuple[Path, ...]) -> None:
    for path in paths:
        info = path.lstat()
        if info.st_uid or not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o022:
            raise ValueError("Unsafe root-owned directory")
        no_acl(path)
