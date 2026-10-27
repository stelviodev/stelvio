"""Process inventory keeps shutdown checks within their remaining budget."""

import os

from pytest import mark

from stelvio.tunnel.processes import ProcessIdentity, process_group_empty


@mark.parametrize("present", [False, True])
def test_group_inventory_uses_remaining_budget(monkeypatch, present):
    calls = []

    def output(arguments, **options):
        calls.append((arguments, options))
        return f"123 1 123 {os.geteuid()}\n456 1 456 {os.geteuid()}\n"

    def process(pid):
        if pid == 123 and not present:
            return None
        return ProcessIdentity(pid, 1, pid, os.geteuid(), 1, 0, 2)

    monkeypatch.setattr("stelvio.tunnel.processes.subprocess.check_output", output)
    monkeypatch.setattr("stelvio.tunnel.processes.identity", process)
    assert process_group_empty(123, timeout=0.25) is not present
    assert calls == [
        (["/bin/ps", "-axo", "pid=,ppid=,pgid=,uid="], {"text": True, "timeout": 0.25})
    ]


def test_foreign_group_member_prevents_cleanup_confirmation(monkeypatch):
    monkeypatch.setattr(
        "stelvio.tunnel.processes.subprocess.check_output",
        lambda *args, **kwargs: f"123 1 123 {os.geteuid() + 1}\n",
    )

    def forbidden_identity(pid):
        raise AssertionError("foreign process identity must not be inspected")

    monkeypatch.setattr("stelvio.tunnel.processes.identity", forbidden_identity)
    assert not process_group_empty(123, timeout=0.25)
