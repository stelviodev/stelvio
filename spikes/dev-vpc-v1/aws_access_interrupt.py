"""Kill a real P2 create, then recover it in a separate production owner process."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from aws_access_lifecycle import Fixture, report

from stelvio.tunnel.access_state import AccessIntent, AccessJournal
from stelvio.tunnel.actors import read_registered_actors, stop_registered_actors
from stelvio.tunnel.processes import ProcessIdentity, identity

_ZOMBIE = 5
_STOPPED = 4


def freeze(process: ProcessIdentity) -> None:
    current = identity(process.pid)
    if (
        process.uid != os.geteuid()
        or process.pid == os.getpid()
        or not current
        or not current.same_process(process)
    ):
        raise RuntimeError("Proof actor identity changed before freeze")
    os.kill(process.pid, signal.SIGSTOP)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        current = identity(process.pid)
        if not current or not current.same_process(process) or current.status == _ZOMBIE:
            raise RuntimeError("Proof actor exited before freeze")
        if current.status == _STOPPED:
            return
        time.sleep(0.05)
    raise RuntimeError("Proof actor did not freeze")


def interrupt(fixture: Fixture, child: subprocess.Popen) -> None:  # noqa: C901 - serial live ownership fences
    deadline = time.monotonic() + 360
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise RuntimeError("Create exited before interruption; inspect create.log")
        saved = json.loads(fixture.path.read_text())
        if "access_intent" not in saved:
            time.sleep(0.5)
            continue
        intent = AccessIntent.from_dict(saved["access_intent"])
        journal = AccessJournal(fixture.s3, fixture.args.bucket, fixture.account, intent)
        claim = journal.read("claim.json")
        if not claim or not journal.read("creation-started.json"):
            time.sleep(0.5)
            continue
        creator = ProcessIdentity(**claim["creator"])
        actual = identity(child.pid)
        if creator.pid != child.pid or not actual or not creator.same_process(actual):
            raise RuntimeError("Creator claim differs from the proof child")
        groups = fixture.ec2.describe_security_groups(
            Filters=[
                {"Name": "tag:stlv:tunnel-session", "Values": [intent.session]},
                {"Name": "tag:stlv:tunnel-unit", "Values": [intent.unit]},
            ]
        )["SecurityGroups"]
        if not groups:
            time.sleep(0.5)
            continue
        if journal.read("creation-finished.json") is not None:
            raise RuntimeError("Creation already completed; interruption gate missed")
        freeze(creator)
        live_kinds = set()
        for key in journal.records():
            name = key.removeprefix(intent.prefix)
            if name.startswith("actor-process-"):
                record = journal.read(name)
                actor = ProcessIdentity(**record["identity"])
                current = identity(actor.pid)
                if (
                    record.get("operation") == "access-create"
                    and (record["kind"] != "engine" or record.get("command") == "up")
                    and current
                    and current.same_process(actor)
                    and current.status != _ZOMBIE
                ):
                    freeze(actor)
                    live_kinds.add(record["kind"])
        if live_kinds != {"engine", "aws-provider"}:
            raise RuntimeError("No live access-create engine/provider at interruption")
        # Freeze the exact creator before reading its enforced durable actor set.
        # The production stop function also validates birth identities and waits
        # for every registered engine/provider to die before certifying cleanup.
        proof = stop_registered_actors(journal, creator)
        proof.require_stopped()
        child.wait(timeout=15)
        actors = read_registered_actors(journal, creator)
        if journal.read("creation-finished.json") is not None:
            raise RuntimeError("Did not interrupt an unfinished registered native create")
        fixture.state = json.loads(fixture.path.read_text())
        fixture.state["interruption"] = {
            "creator": claim["creator"],
            "registered_processes": len(actors),
            "group_ids": [group["GroupId"] for group in groups],
            "creation_finished": False,
        }
        fixture.save()
        report(
            "create-interrupted",
            registered_processes=len(actors),
            group_ids=fixture.state["interruption"]["group_ids"],
        )
        return
    raise RuntimeError("Timed out waiting for real AWS create effects")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--region", default="us-east-1")
    args = parser.parse_args()
    os.umask(0o077)
    fixture = Fixture(args)
    if set(fixture.state) != {"context"}:
        raise RuntimeError(
            "Use a new owner; recover an existing owner with aws_access_lifecycle.py"
        )
    command = [sys.executable, str(Path(__file__).with_name("aws_access_lifecycle.py"))]
    options = ["--owner", args.owner, "--bucket", args.bucket, "--region", args.region]
    failed = None
    with (fixture.root / "create.log").open("w") as output:
        child = subprocess.Popen(  # noqa: S603 - fixed proof script, explicit owner context
            [*command, "run", *options],
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
        try:
            child_identity = identity(child.pid)
        except BaseException:
            child.kill()
            child.wait(timeout=15)
            raise
        if not child_identity or child_identity.uid != os.geteuid():
            child.kill()
            child.wait(timeout=15)
            raise RuntimeError("Cannot identify the proof creator; recover its fixture WAL")
        try:
            interrupt(fixture, child)
        except BaseException as error:
            failed = error
        finally:
            if child.poll() is None:
                stop_child(fixture, child, child_identity)
    report("recover-start", owner=args.owner)
    result = subprocess.run([*command, "recover", *options], check=False)  # noqa: S603
    if result.returncode:
        raise RuntimeError("Fresh-process recovery failed; retained exact owner requires recovery")
    if failed:
        raise failed
    report("interrupted-create-PASS", owner=args.owner)


def stop_child(fixture: Fixture, child: subprocess.Popen, birth: ProcessIdentity) -> None:
    # Fence SDK writes and further registrations before fallible metadata reads.
    freeze(birth)
    try:
        saved = json.loads(fixture.path.read_text())
        if "access_intent" in saved:
            intent = AccessIntent.from_dict(saved["access_intent"])
            journal = AccessJournal(fixture.s3, fixture.args.bucket, fixture.account, intent)
            claim = journal.read("claim.json")
            if claim:
                creator = ProcessIdentity(**claim["creator"])
                if not creator.same_process(birth):
                    raise RuntimeError("Proof child claim differs; retain recovery WAL")  # noqa: TRY301 - report uncertain stop below
                stop_registered_actors(journal, creator)
            else:
                child.kill()
        else:
            child.kill()
        child.wait(timeout=15)
    except BaseException:
        report(
            "stop-uncertain",
            owner=fixture.args.owner,
            creator_pid=birth.pid,
            birth_seconds=birth.birth_seconds,
            birth_microseconds=birth.birth_microseconds,
            recovery="aws_access_lifecycle.py recover",
            path=str(fixture.root),
        )
        raise


if __name__ == "__main__":
    main()
