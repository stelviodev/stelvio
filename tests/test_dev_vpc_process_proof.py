"""Interrupt the proof's whole engine tree before permitting backend recovery."""

import os
from dataclasses import replace
from importlib import import_module

from pytest import raises

tree = import_module("spikes.dev-vpc-v1.process_tree")


def test_separate_plugin_group_is_recorded_stopped_and_killed(monkeypatch):
    root = os.getpid() + 10000
    child = root + 1
    processes = {
        root: tree.Process(root, os.getpid(), root, os.geteuid(), "/proof/python", "S"),
        child: tree.Process(child, root, child, os.geteuid(), "/proof/provider", "S"),
    }
    events = []
    recorded = set()
    monkeypatch.setattr(tree, "snapshot", lambda: dict(processes))
    monkeypatch.setattr(tree.subprocess, "check_output", lambda *args, **kwargs: "")

    def persist(members):
        recorded.update(member.pid for member in members)
        events.append("record")

    def signal(pid, kind):
        assert pid in recorded
        if kind == tree.signal.SIGSTOP:
            processes[pid] = replace(processes[pid], state="T")
        else:
            assert kind == tree.signal.SIGKILL
            assert all(process.state == "T" for process in processes.values())
            events.append("kill")
            del processes[pid]

    monkeypatch.setattr(tree.os, "kill", signal)
    result = tree.interrupt_tree(root, on_discovery=persist)
    assert {process.pid for process in result} == {root, child}
    assert not processes
    assert events.index("record") < events.index("kill")
    assert events.count("kill") == 2


def test_disappearing_ancestor_refuses_success_after_stop(monkeypatch):
    root = os.getpid() + 10000
    process = tree.Process(root, os.getpid(), root, os.geteuid(), "/proof/python", "S")
    processes = {root: process}
    signals = []
    recorded = []
    monkeypatch.setattr(tree, "snapshot", lambda: dict(processes))

    def signal(pid, kind):
        signals.append(kind)
        processes.pop(pid)

    monkeypatch.setattr(tree.os, "kill", signal)
    with raises(RuntimeError, match="disappeared or was reparented"):
        tree.interrupt_tree(root, on_discovery=lambda members: recorded.extend(members))
    assert recorded == [process]
    assert signals == [tree.signal.SIGSTOP]
