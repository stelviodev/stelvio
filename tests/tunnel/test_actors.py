"""Real native execution is gated by durable ownership, even on lost returns."""

import json
import os
import select
import signal
import subprocess
import sys
import time
from contextlib import suppress
from dataclasses import replace
from pathlib import Path

from pytest import mark, raises

from stelvio.tunnel.access_state import AccessJournal
from stelvio.tunnel.actors import ActorRegistry, stop_registered_actors
from stelvio.tunnel.processes import ProcessIdentity, identity
from tests.tunnel.storage import VersionedStorage

pytestmark = mark.skipif(sys.platform != "darwin", reason="Darwin native actors and birth fencing")


def test_lost_registration_response_never_executes_native_actor(
    access_intent, tmp_path, monkeypatch
):
    intent = replace(access_intent, owner_uid=os.geteuid())
    journal = AccessJournal(VersionedStorage(), "state-bucket", intent.account, intent)
    journal.claim(identity(os.getpid()))
    registry = ActorRegistry(journal, Path("/usr/bin/touch"), Path("/bin/sleep"))
    marker = tmp_path / "native-executed"
    record = journal.record
    receipts = []

    def lose_return(name, value):
        record(name, value)
        if name.startswith("actor-process-"):
            receipts.append(value)
            assert not marker.exists()
            raise TimeoutError("durable registration response lost")

    try:
        monkeypatch.setattr(journal, "record", lose_return)
        with raises(TimeoutError, match="durable registration response lost"):
            registry.spawn("engine", [str(marker)], {})
        registry.require_stopped()
        assert len(receipts) == 1
        assert not marker.exists()
        # A later ordinary registration authorizes the same native executable.
        monkeypatch.setattr(journal, "record", record)
        process = registry.spawn("engine", [str(marker)], {})
        assert process.wait(timeout=5) == 0
        assert marker.is_file()
    finally:
        for _, child in registry.children:
            registry.stop(child)
            child.stdout.close()
            child.stderr.close()


def test_registered_native_orphans_are_stopped_before_recovery_claim(access_intent):
    script = """
import json, os, sys, time
from pathlib import Path
from stelvio.tunnel.access_state import AccessIntent, AccessJournal
from stelvio.tunnel.actors import ActorRegistry
from stelvio.tunnel.processes import identity
from tests.tunnel.storage import VersionedStorage
intent = AccessIntent.from_dict(json.loads(sys.stdin.readline()))
journal = AccessJournal(VersionedStorage(), 'state-bucket', intent.account, intent)
journal.claim(identity(os.getpid()))
registry = ActorRegistry(journal, Path('/bin/sleep'), Path('/bin/sleep'))
registry.spawn('aws-provider', ['60'], {})
registry.spawn('engine', ['60'], {})
print(json.dumps({key: value.decode() for key, value in journal.s3.objects.items()}), flush=True)
time.sleep(60)
"""
    intent = replace(access_intent, owner_uid=os.geteuid())
    process = subprocess.Popen(  # noqa: S603 - controlled nonroot fixture and literal script
        [sys.executable, "-u", "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    actors = []
    try:
        process.stdin.write(json.dumps(intent.to_dict()) + "\n")
        process.stdin.close()
        assert select.select([process.stdout], [], [], 10)[0]
        storage = VersionedStorage()
        storage.objects = {
            key: value.encode() for key, value in json.loads(process.stdout.readline()).items()
        }
        journal = AccessJournal(storage, "state-bucket", intent.account, intent)
        creator = ProcessIdentity(**journal.read("claim.json")["creator"])
        actors = [
            ProcessIdentity(**json.loads(value)["identity"])
            for key, value in storage.objects.items()
            if key.rsplit("/", 1)[-1].startswith("actor-process-")
        ]
        assert len(actors) == 2
        process.kill()
        process.wait(timeout=5)
        assert all(identity(actor.pid).same_process(actor) for actor in actors)
        proof = stop_registered_actors(journal, creator)
        assert set(proof.processes) == {creator, *actors}
        proof.require_stopped()
        journal.recover_claim(proof, identity(os.getpid()))
        journal.require_claim()
        assert journal.read("claim.json")["creator"]["pid"] == os.getpid()
    finally:
        for actor in actors:
            actual = identity(actor.pid)
            if actual and actor.same_process(actual):
                with suppress(ProcessLookupError):
                    os.kill(actor.pid, signal.SIGKILL)
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        process.stdout.close()


def test_creator_death_closes_unacknowledged_exec_barrier(tmp_path):
    launcher = Path(__file__).parents[2] / "stelvio/tunnel/process_child.py"
    marker = tmp_path / "must-not-execute"
    script = """
import json, subprocess, sys, time
from dataclasses import asdict
from stelvio.tunnel.processes import identity
child = subprocess.Popen([sys.executable, '-I', '-S', sys.argv[1],
                          '/usr/bin/touch', sys.argv[2]], stdin=subprocess.PIPE,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         close_fds=True, start_new_session=True)
print(json.dumps(asdict(identity(child.pid))), flush=True)
time.sleep(60)
"""
    process = subprocess.Popen(  # noqa: S603 - controlled barrier fixture and literal script
        [sys.executable, "-u", "-c", script, str(launcher), str(marker)],
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    child = None
    try:
        assert select.select([process.stdout], [], [], 10)[0]
        child = ProcessIdentity(**json.loads(process.stdout.readline()))
        assert identity(child.pid).same_process(child)
        process.kill()
        process.wait(timeout=5)
        deadline = time.monotonic() + 5
        while (
            (actual := identity(child.pid)) and child.same_process(actual) and actual.status != 5
        ):
            assert time.monotonic() < deadline
            time.sleep(0.05)
        assert not marker.exists()
    finally:
        if child and (actual := identity(child.pid)) and child.same_process(actual):
            with suppress(ProcessLookupError):
                os.kill(child.pid, signal.SIGKILL)
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        process.stdout.close()
