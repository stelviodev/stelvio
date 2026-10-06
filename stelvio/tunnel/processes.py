"""Same-UID macOS creator-tree fencing, without reading token-bearing arguments.

The creator must remain alive until this boundary freezes its complete tree.
Missing/reparented ancestors cannot certify an unknown detached AWS provider.
Birth timestamps fence PID reuse; process groups alone do not identify ownership.
"""

from __future__ import annotations

import ctypes
import errno
import os
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from functools import cache
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable

_BSD_INFO = 3
_STOPPED = 4
_ZOMBIE = 5
_FREEZE_SECONDS = 3
_EXIT_SECONDS = 15


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
        raise RuntimeError("VPC creator process fencing requires the supported macOS host")
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
    info = _BsdInfo()
    ctypes.set_errno(0)
    size = _libproc().proc_pidinfo(pid, _BSD_INFO, 0, ctypes.byref(info), ctypes.sizeof(info))
    if size != ctypes.sizeof(info):
        error = ctypes.get_errno()
        if error == errno.ESRCH:
            return None
        raise RuntimeError("Cannot verify creator process identity; backend recovery refused")
    return ProcessIdentity(
        info.pid,
        info.ppid,
        info.pgid,
        info.uid,
        info.seconds,
        info.microseconds,
        info.status,
    )


def _snapshot() -> dict[int, ProcessIdentity]:
    output = subprocess.check_output(
        ["/bin/ps", "-axo", "pid=,ppid=,pgid=,uid="], text=True, timeout=5
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


def _descendants(root: int, processes: dict[int, ProcessIdentity]) -> dict[int, ProcessIdentity]:
    found = {root: processes[root]} if root in processes else {}
    while added := {
        pid: process
        for pid, process in processes.items()
        if process.parent in found and pid not in found
    }:
        found.update(added)
    return found


def _signal(process: ProcessIdentity, kind: signal.Signals) -> None:
    actual = identity(process.pid)
    if not actual or not process.same_process(actual):
        if kind == signal.SIGSTOP:
            raise RuntimeError("Creator ancestor disappeared or changed; recovery refused")
        return
    if actual.uid != os.geteuid() or process.pid == os.getpid():
        raise RuntimeError("Require a separate same-UID creator; signal refused")
    if kind == signal.SIGSTOP and actual.parent != process.parent:
        raise RuntimeError("Creator ancestor was reparented; recovery refused")
    try:
        os.kill(process.pid, kind)
    except ProcessLookupError:
        if kind == signal.SIGSTOP:
            raise RuntimeError("Creator disappeared before freezing; recovery refused") from None


def _confirm_frozen(process: ProcessIdentity) -> None:
    deadline = time.monotonic() + _FREEZE_SECONDS
    while True:
        actual = identity(process.pid)
        if not actual or not process.same_process(actual) or actual.parent != process.parent:
            raise RuntimeError("Creator ancestry changed while freezing; recovery refused")
        if actual.status == _STOPPED:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError("Creator did not freeze; retain recovery ownership")
        time.sleep(0.05)


@dataclass(frozen=True)
class StoppedTree:
    processes: tuple[ProcessIdentity, ...]
    completion: dict

    @classmethod
    def restore(cls, completion: dict) -> StoppedTree:
        """A partial discovery record is never a completed freeze certificate."""
        if not isinstance(completion, dict) or completion.keys() != {
            "version",
            "state",
            "processes",
        }:
            raise RuntimeError("Complete creator freeze receipt is missing; recovery refused")
        try:
            processes = tuple(ProcessIdentity(**value) for value in completion["processes"])
        except (KeyError, TypeError) as error:
            raise RuntimeError("Malformed creator freeze receipt; recovery refused") from error
        if completion != _completion(processes):
            raise RuntimeError("Complete creator freeze receipt differs; recovery refused")
        return cls(processes, completion)

    def require_stopped(self) -> None:
        if not self.processes or self.completion != _completion(self.processes):
            raise RuntimeError("No complete creator tree was recorded; recovery refused")
        for process in self.processes:
            actual = identity(process.pid)
            if actual and process.same_process(actual) and actual.status != _ZOMBIE:
                raise RuntimeError(
                    "Owned creator/provider remains alive; backend recovery refused"
                )


class CreatorStopped(Protocol):
    @property
    def processes(self) -> tuple[ProcessIdentity, ...]: ...

    def require_stopped(self) -> None: ...


def _completion(processes: tuple[ProcessIdentity, ...]) -> dict:
    return {
        "version": 1,
        "state": "frozen-complete",
        "processes": [asdict(process) for process in processes],
    }


def _freeze_creator(
    creator: ProcessIdentity, persist: Callable[[tuple[ProcessIdentity, ...]], None]
) -> tuple[ProcessIdentity, ...]:
    if creator.uid != os.geteuid() or creator.uid == 0 or creator.pid == os.getpid():
        raise RuntimeError("Require a separate nonroot same-UID creator")
    owned = {creator.pid: creator}
    persist(tuple(owned.values()))
    for _ in range(10):
        for process in owned.values():
            _signal(process, signal.SIGSTOP)
            _confirm_frozen(process)
        discovered = _descendants(creator.pid, _snapshot())
        if creator.pid not in discovered:
            raise RuntimeError("Creator ancestry disappeared; retain frozen recovery ownership")
        if any(process.uid != creator.uid for process in discovered.values()):
            raise RuntimeError("Foreign-UID creator descendant; recovery refused")
        if any(
            not owned[pid].same_process(process)
            for pid, process in discovered.items()
            if pid in owned
        ):
            raise RuntimeError("Creator PID generation changed; recovery refused")
        added = {pid: process for pid, process in discovered.items() if pid not in owned}
        owned.update(added)
        persist(tuple(owned.values()))
        if not added:
            break
    else:
        raise RuntimeError("Creator tree did not stabilize; retain frozen recovery ownership")
    for process in owned.values():
        _confirm_frozen(process)
    return tuple(owned.values())


def stop_creator(
    creator: ProcessIdentity,
    persist: Callable[[tuple[ProcessIdentity, ...]], None],
    *,
    require_ancestry_lifetime: Callable[[], None],
    persist_completion: Callable[[dict], None],
) -> StoppedTree:
    """Freeze parents, durably record all descendants, then kill and verify absence.

    The runner must guarantee, from before AWS creation begins, that a creating
    descendant cannot exit and orphan still-creating descendants before this
    freeze discovers them. Post-hoc snapshots cannot establish that guarantee.
    There is deliberately no default/no-op production implementation.
    The trusted caller supplies the birth identity captured before creation began.
    If discovery or persistence fails, processes remain frozen and records remain
    recoverable; no backend cancellation or AWS cleanup is authorized.
    """
    require_ancestry_lifetime()
    owned = _freeze_creator(creator, persist)
    require_ancestry_lifetime()
    completed = _completion(owned)
    # Only this separate receipt, after final freeze verification and before
    # killing, certifies completeness rather than a partial discovery tuple.
    persist_completion(completed)
    for process in reversed(owned):
        _signal(process, signal.SIGKILL)
    proof = StoppedTree(owned, completed)
    deadline = time.monotonic() + _EXIT_SECONDS
    while True:
        try:
            proof.require_stopped()
        except RuntimeError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.1)
        else:
            return proof
