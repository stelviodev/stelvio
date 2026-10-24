"""Same-UID macOS SSH process identity and process-group fencing.

Birth timestamps fence PID reuse without reading token-bearing arguments.
"""

from __future__ import annotations

import ctypes
import errno
import os
import subprocess
import sys
from dataclasses import dataclass
from functools import cache
from pathlib import Path

_BSD_INFO = 3
_MAX_PROC_STAT = 4096


class _BsdInfo(ctypes.Structure):
    # Darwin SDK sys/proc_info.h, struct proc_bsdinfo; uid_t/gid_t are uint32.
    _fields_ = (
        [
            (name, ctypes.c_uint32)
            for name in (
                "flags",
                "status",
                "xstatus",
                "pid",
                "ppid",
                "uid",
                "gid",
                "ruid",
                "rgid",
                "svuid",
                "svgid",
                "reserved",
            )
        ]
        + [("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32)]
        + [(name, ctypes.c_uint32) for name in ("nfiles", "pgid", "pjobc", "tdev", "tpgid")]
        + [
            ("nice", ctypes.c_int32),
            ("seconds", ctypes.c_uint64),
            ("microseconds", ctypes.c_uint64),
        ]
    )


@cache
def _libproc() -> ctypes.CDLL:
    if sys.platform != "darwin":
        raise RuntimeError("SSH process fencing requires the supported macOS host")
    library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    library.proc_pidinfo.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint64,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    library.proc_pidinfo.restype = ctypes.c_int
    return library


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    parent: int
    group: int
    uid: int
    birth_seconds: int
    birth_microseconds: int
    status: int

    def same_process(self, other: ProcessIdentity) -> bool:
        return (self.pid, self.uid, self.birth_seconds, self.birth_microseconds) == (
            other.pid,
            other.uid,
            other.birth_seconds,
            other.birth_microseconds,
        )


def identity(pid: int) -> ProcessIdentity | None:
    if sys.platform == "linux":
        return _linux_identity(pid)
    info = _BsdInfo()
    ctypes.set_errno(0)
    size = _libproc().proc_pidinfo(pid, _BSD_INFO, 0, ctypes.byref(info), ctypes.sizeof(info))
    if size != ctypes.sizeof(info):
        error = ctypes.get_errno()
        if error == errno.ESRCH:
            return None
        raise RuntimeError("Cannot verify SSH process identity; process signaling refused")
    return ProcessIdentity(
        info.pid,
        info.ppid,
        info.pgid,
        info.uid,
        info.seconds,
        info.microseconds,
        info.status,
    )


def unreaped_child(pid: int) -> bool:
    """Peek at our exited child without releasing its PID/process-group fence.

    Darwin libproc stops returning BSD identity for zombies. waitid(WNOWAIT)
    proves the exact PID is still our waitable child; it cannot be reused.
    """
    if sys.platform == "linux":
        try:
            result = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        except ChildProcessError:
            return False
        return result is not None and result.si_pid == pid
    if sys.platform != "darwin":
        raise RuntimeError("SSH process fencing requires a supported Unix host")
    # Darwin siginfo_t begins with six 32-bit fields, then 64-bit data/padding.
    # An aligned full-size buffer avoids reading platform-dependent unused fields.
    info = (ctypes.c_uint64 * 13)()
    library = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    library.waitid.argtypes = [ctypes.c_int, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int]
    library.waitid.restype = ctypes.c_int
    ctypes.set_errno(0)
    result = library.waitid(
        os.P_PID, pid, ctypes.byref(info), os.WEXITED | os.WNOHANG | os.WNOWAIT
    )
    if result:
        error = ctypes.get_errno()
        if error == errno.ECHILD:
            return False
        raise OSError(error, "Cannot verify unreaped SSH child")
    return ctypes.cast(info, ctypes.POINTER(ctypes.c_int32))[3] == pid


def process_group_empty(group: int, *, timeout: float = 5) -> bool:
    """Read-only live process inventory, used only for an already fenced child."""
    return not any(process.group == group for process in _snapshot(timeout=timeout).values())


def _snapshot(*, timeout: float = 5) -> dict[int, ProcessIdentity]:
    output = subprocess.check_output(
        ["/bin/ps", "-axo", "pid=,ppid=,pgid=,uid="], text=True, timeout=timeout
    )
    processes = {}
    for row in output.splitlines():
        pid, parent, group, uid = map(int, row.split())
        if uid == os.geteuid():
            process = identity(pid)
            if process:
                processes[pid] = process
        else:
            # Public ancestry suffices to reject foreign descendants. Never
            # signal them or infer their birth identity from a PID alone.
            processes[pid] = ProcessIdentity(pid, parent, group, uid, 0, 0, 0)
    return processes


def _linux_identity(pid: int) -> ProcessIdentity | None:
    if pid <= 0:
        return None
    path = Path(f"/proc/{pid}")
    try:
        with (path / "stat").open() as stream:
            raw = stream.read(_MAX_PROC_STAT + 1)
        if len(raw) > _MAX_PROC_STAT:
            raise RuntimeError("Linux process identity exceeds bounds")
        fields = raw[raw.rindex(") ") + 2 :].split()
        uid = path.stat().st_uid
        ticks = int(fields[19])
        # Incorporate boot identity without reading token-bearing arguments.
        boot = int(Path("/proc/sys/kernel/random/boot_id").read_text().replace("-", ""), 16)
        return ProcessIdentity(
            pid,
            int(fields[1]),
            int(fields[2]),
            uid,
            boot,
            ticks,
            5 if fields[0] == "Z" else 1,
        )
    except (FileNotFoundError, ProcessLookupError):
        return None
    except (OSError, ValueError, IndexError) as error:
        raise RuntimeError("Cannot verify Linux process identity; signaling refused") from error
