"""Stop a controlled proof's nonroot process tree, including Pulumi plugin groups.

Only executable identities are inspected; process arguments may contain tokens.
The proof parent must still be alive when this runs, so descendants can be
identified before interruption causes reparenting.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

PS_FIELDS = 6


@dataclass(frozen=True)
class Process:
    pid: int
    parent: int
    group: int
    uid: int
    executable: str
    state: str


def snapshot() -> dict[int, Process]:
    output = subprocess.check_output(
        ["/bin/ps", "-axo", "pid=,ppid=,pgid=,uid=,state=,comm="], text=True, timeout=5
    )
    result = {}
    for row in output.splitlines():
        fields = row.split(maxsplit=5)
        if len(fields) == PS_FIELDS:
            process = Process(
                *(int(value) for value in fields[:4]), executable=fields[5], state=fields[4]
            )
            result[process.pid] = process
    return result


def descendants(root: int, processes: dict[int, Process]) -> dict[int, Process]:
    found = {root: processes[root]} if root in processes else {}
    while True:
        added = {
            pid: process
            for pid, process in processes.items()
            if process.parent in found and pid not in found
        }
        if not added:
            return found
        found.update(added)


def signal_owned(process: Process, kind: signal.Signals) -> None:
    actual = snapshot().get(process.pid)
    if actual is None:
        if kind == signal.SIGSTOP:
            raise RuntimeError("Proof ancestor disappeared before freezing; cancel refused")
        return
    if (actual.group, actual.uid, actual.executable) != (
        process.group,
        process.uid,
        process.executable,
    ):
        raise RuntimeError("Proof process identity changed; signal refused")
    if kind == signal.SIGSTOP and actual.parent != process.parent:
        raise RuntimeError("Proof ancestor was reparented before freezing; cancel refused")
    with suppress(ProcessLookupError):
        os.kill(process.pid, kind)
    if kind == signal.SIGSTOP:
        confirm_stopped(process)


def confirm_stopped(process: Process) -> None:
    deadline = time.monotonic() + 3
    while True:
        actual = snapshot().get(process.pid)
        if actual is None or actual.parent != process.parent:
            raise RuntimeError("Proof ancestor disappeared or was reparented; cancel refused")
        if (actual.group, actual.uid, actual.executable) != (
            process.group,
            process.uid,
            process.executable,
        ):
            raise RuntimeError("Proof ancestor identity changed; cancel refused")
        if actual.state.startswith("T"):
            return
        if time.monotonic() >= deadline:
            raise RuntimeError("Proof ancestor did not stop; cancel refused")
        time.sleep(0.05)


def freeze_tree(
    root: int, initial: Process, on_discovery: Callable[[list[Process]], None]
) -> dict[int, Process]:
    owned = {root: initial}
    on_discovery(list(owned.values()))
    # Freeze parents while discovering children: they cannot spawn new plugins
    # or exit and erase the parent relationship before those plugins are recorded.
    for _ in range(10):
        for process in owned.values():
            signal_owned(process, signal.SIGSTOP)
        discovered = descendants(root, snapshot())
        if any(process.uid != initial.uid for process in discovered.values()):
            raise RuntimeError("Proof contains a foreign-UID descendant; interruption refused")
        new = {pid: process for pid, process in discovered.items() if pid not in owned}
        owned.update(new)
        on_discovery(list(owned.values()))
        if not new:
            for process in owned.values():
                confirm_stopped(process)
            break
    else:
        raise RuntimeError("Proof process tree did not stabilize; retain frozen identities")
    return owned


def interrupt_tree(root: int, *, on_discovery: Callable[[list[Process]], None]) -> list[Process]:
    initial = snapshot().get(root)
    if initial is None or initial.uid != os.geteuid() or root == os.getpid():
        raise RuntimeError("Require a live, separate, same-UID proof creator")
    owned = freeze_tree(root, initial, on_discovery)
    for process in reversed(list(owned.values())):
        signal_owned(process, signal.SIGKILL)
    deadline = time.monotonic() + 15
    while True:
        current = snapshot()
        # Zombies cannot perform AWS operations. A separate state-only query
        # distinguishes them without ever reading token-bearing arguments.
        states = subprocess.check_output(["/bin/ps", "-axo", "pid=,state="], text=True, timeout=5)
        zombies = {
            int(row.split()[0]) for row in states.splitlines() if row.split()[1].startswith("Z")
        }
        live = [pid for pid in owned if pid in current and pid not in zombies]
        if not live:
            return list(owned.values())
        if time.monotonic() >= deadline:
            raise RuntimeError("Owned engine descendants remain alive; backend cancel refused")
        time.sleep(0.2)
