"""Durable, before-exec ownership for native AWS provider and backend actors.

Every AWS-mutating subprocess must pass through this barrier. The supervisor's
own birth identity is in the claim before it mutates AWS through the SDK. Unlike
ancestry snapshots, registration survives later reparenting or creator death.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from contextlib import suppress
from dataclasses import asdict
from hashlib import file_digest
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from stelvio.tunnel.processes import ProcessIdentity, identity

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from stelvio.tunnel.access_state import AccessJournal

CLI_VERSION = "3.263.0"
AWS_VERSION = "7.47.0"
_KINDS = {"engine", "aws-provider"}
_ZOMBIE = 5
_MACHO = {b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xca\xfe\xba\xbe"}


def _digest(binary: Path) -> str:
    with binary.open("rb") as stream:
        if stream.read(4) not in _MACHO or not os.access(binary, os.X_OK):
            raise RuntimeError("Require the proven native macOS AWS actor artifact")
        stream.seek(0)
        return file_digest(stream, "sha256").hexdigest()


class ActorRegistry:
    def __init__(self, journal: AccessJournal, cli: Path, provider: Path) -> None:
        self.journal = journal
        self.cli = cli
        self.provider = provider
        self.creator = identity(os.getpid())
        if (
            not self.creator
            or self.creator.uid == 0
            or self.creator.uid != journal.intent.owner_uid
        ):
            raise RuntimeError("Require the intended nonroot AWS actor supervisor")
        self.profile = {
            "version": 1,
            "protocol": "registered-native-actors",
            "cli": {"version": CLI_VERSION, "sha256": _digest(cli)},
            "aws-provider": {"version": AWS_VERSION, "sha256": _digest(provider)},
        }
        self.children: list[tuple[ProcessIdentity, subprocess.Popen]] = []
        self.operation: str | None = None

    def _check(self) -> None:
        self.journal.require_claim()
        claim = self.journal.read("claim.json")
        saved = claim.get("creator")
        if not saved or not self.creator.same_process(ProcessIdentity(**saved)):
            raise RuntimeError("Native AWS actor supervisor differs from the claimed creator")
        actual = identity(self.creator.pid)
        if not actual or not actual.same_process(self.creator):
            raise RuntimeError("Native AWS actor supervisor birth identity changed")
        self.journal.record("actor-protocol.json", self.profile)

    def bind(self) -> None:
        """Publish the enforced actor protocol before any worker SDK mutations."""
        self._check()

    def __call__[T](self, label: str, operation: Callable[[], T]) -> T:
        self._check()
        self.require_stopped()
        self.operation = label
        try:
            return operation()
        finally:
            self.operation = None
            self.require_stopped()

    def spawn(
        self,
        kind: str,
        args: Sequence[str],
        environment: Mapping[str, str],
        cwd: str | None = None,
    ) -> subprocess.Popen:
        self._check()
        if kind not in _KINDS:
            raise ValueError("Unknown native AWS actor kind")
        binary = self.cli if kind == "engine" else self.provider
        if _digest(binary) != self.profile[kind if kind == "aws-provider" else "cli"]["sha256"]:
            raise RuntimeError("Native AWS actor artifact changed; creation refused")
        process = subprocess.Popen(  # noqa: S603 - fixed isolated launcher and verified native actor
            [
                sys.executable,
                "-I",
                "-S",
                str(Path(__file__).with_name("process_child.py")),
                str(binary),
                *args,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(environment),
            cwd=cwd,
            text=True,
            start_new_session=True,
            close_fds=True,
        )
        actor = identity(process.pid)
        if not actor or actor.uid != self.creator.uid:
            process.kill()
            process.wait(timeout=5)
            raise RuntimeError("Native AWS actor could not be identified before execution")
        self.children.append((actor, process))
        try:
            self.journal.record(
                f"actor-process-{uuid4()}.json",
                {
                    "version": 1,
                    "kind": kind,
                    "creator": asdict(self.creator),
                    "identity": asdict(actor),
                    "sha256": self.profile[kind if kind == "aws-provider" else "cli"]["sha256"],
                },
            )
            self._check()
            process.stdin.write("1")
            process.stdin.flush()
            process.stdin.close()
        except BaseException:
            # Without ACK the child cannot mutate AWS; closing the sole writer
            # yields EOF. Kill it as well so a failed startup leaves no launcher.
            process.stdin.close()
            process.kill()
            process.wait(timeout=5)
            raise
        return process

    def stop(self, process: subprocess.Popen) -> None:
        owned = next(actor for actor, child in self.children if child is process)
        actual = identity(owned.pid)
        if actual and actual.same_process(owned):
            with suppress(ProcessLookupError):
                os.kill(owned.pid, signal.SIGKILL)
        process.wait(timeout=15)

    def require_stopped(self) -> None:
        for actor, _ in self.children:
            actual = identity(actor.pid)
            if actual and actor.same_process(actual) and actual.status != _ZOMBIE:
                raise RuntimeError("Registered AWS actor is still alive; cleanup refused")


def read_registered_actors(
    journal: AccessJournal, creator: ProcessIdentity
) -> tuple[ProcessIdentity, ...]:
    """Inventory a stopped creator's durable barrier receipts, including orphans."""
    actual = identity(creator.pid)
    if actual and creator.same_process(actual) and actual.status not in {4, 5}:
        raise RuntimeError("Creator must be frozen or dead before registered actor discovery")
    profile = journal.read("actor-protocol.json")
    if (
        not profile
        or profile.get("protocol") != "registered-native-actors"
        or profile.get("version") != 1
    ):
        raise RuntimeError("Native actor registration protocol is missing; recovery refused")
    result = [creator]
    for key in journal.records():
        name = key.removeprefix(journal.intent.prefix)
        if not name.startswith("actor-process-"):
            continue
        record = journal.read(name)
        if record["version"] != 1 or record["kind"] not in _KINDS:
            raise RuntimeError("Malformed native AWS actor receipt; recovery refused")
        owner = ProcessIdentity(**record["creator"])
        actor = ProcessIdentity(**record["identity"])
        kind = "cli" if record["kind"] == "engine" else "aws-provider"
        if (
            owner.uid != creator.uid
            or actor.uid != creator.uid
            or record["sha256"] != profile[kind]["sha256"]
        ):
            raise RuntimeError("Native AWS actor receipt ownership differs; recovery refused")
        if not owner.same_process(creator):
            owner_actual = identity(owner.pid)
            if (
                owner_actual
                and owner.same_process(owner_actual)
                and owner_actual.status not in {4, 5}
            ):
                raise RuntimeError("An earlier registered creator is still live; recovery refused")
            if owner not in result:
                result.append(owner)
        result.append(actor)
    return tuple(result)
