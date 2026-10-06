"""Controlled local creators prove complete teardown across separate groups."""

import json
import os
import signal
import subprocess
import sys
from contextlib import suppress
from dataclasses import replace

from pytest import fixture, mark, raises

from stelvio.tunnel.access_state import AccessJournal
from stelvio.tunnel.processes import StoppedTree, identity, stop_creator
from tests.tunnel.storage import Storage

pytestmark = mark.skipif(sys.platform != "darwin", reason="Darwin process birth fencing")


@fixture
def creator_tree():
    script = """
import json, subprocess, sys, time
children = [subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                            start_new_session=True) for _ in range(2)]
print(json.dumps([child.pid for child in children]), flush=True)
time.sleep(60)
"""
    process = subprocess.Popen(  # noqa: S603 - controlled fixture executable and literal script
        [sys.executable, "-u", "-c", script],
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    children = json.loads(process.stdout.readline())
    root = identity(process.pid)
    identities = [root, *(identity(pid) for pid in children)]
    try:
        yield root, identities
    finally:
        for owned in reversed(identities):
            actual = identity(owned.pid)
            if actual and owned.same_process(actual):
                with suppress(ProcessLookupError):
                    os.kill(owned.pid, signal.SIGKILL)
        process.wait(timeout=5)
        process.stdout.close()


def test_creator_stop_records_every_provider_before_signals_and_checks_birth(creator_tree):
    root, expected = creator_tree
    assert len({owned.group for owned in expected}) == 3
    records = []
    completed = []

    def lifetime():
        # The fixture's literal script launches two non-forking sleepers and
        # retains their parent for 60 seconds. This known script establishes the
        # lifetime guarantee; post-hoc ancestry cannot establish it for Pulumi.
        assert all(identity(owned.pid).parent == owned.parent for owned in expected)

    def persist(processes):
        # A failed persistence operation would leave these creators alive/frozen,
        # so cleanup cannot advance without a durable complete discovery record.
        assert all(identity(owned.pid) is not None for owned in processes)
        records.append(processes)

    # A PID with a different birth time cannot authorize signals.
    with raises(RuntimeError, match="ancestor disappeared or changed"):
        stop_creator(
            replace(root, birth_microseconds=root.birth_microseconds + 1),
            persist,
            require_ancestry_lifetime=lifetime,
            persist_completion=completed.append,
        )
    assert identity(root.pid).status != 4
    records.clear()
    assert completed == []
    proof = stop_creator(
        root,
        persist,
        require_ancestry_lifetime=lifetime,
        persist_completion=completed.append,
    )
    assert {owned.pid for owned in records[-1]} == {owned.pid for owned in expected}
    assert {owned.pid for owned in proof.processes} == {owned.pid for owned in expected}
    proof.require_stopped()
    proof.require_stopped()
    StoppedTree.restore(completed[0]).require_stopped()
    with raises(RuntimeError, match="freeze receipt is missing"):
        StoppedTree.restore({"processes": completed[0]["processes"]})


def test_remote_recovery_requires_complete_stop_and_cannot_reuse_old_creator_proof(
    creator_tree, access_intent
):
    root, actors = creator_tree
    storage = Storage()
    creating = AccessJournal(storage, "state-bucket", access_intent.account, access_intent)
    creating.claim(root)
    recorded = []

    def lifetime():
        assert all(identity(owned.pid).parent == owned.parent for owned in actors)

    proof = stop_creator(
        root,
        recorded.append,
        require_ancestry_lifetime=lifetime,
        persist_completion=lambda receipt: creating.record("freeze-complete.json", receipt),
    )
    recovering = AccessJournal(storage, "state-bucket", access_intent.account, access_intent)
    recovering.recover_claim(proof, identity(os.getpid()))
    recovering.require_claim()
    with raises(RuntimeError, match="claim is missing or changed"):
        creating.require_claim()
    another = AccessJournal(storage, "state-bucket", access_intent.account, access_intent)
    with raises(RuntimeError, match="differs from previous creator"):
        another.recover_claim(proof, identity(os.getpid()))
    recovering.release()
    assert recovering.read("claim.json") is None
