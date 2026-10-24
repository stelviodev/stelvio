"""Nonprivileged regressions for CLI SSM cleanup acceptance."""

import json
import os
import stat
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from . import ssm_cleanup


@pytest.fixture
def world(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    cache.mkdir(mode=0o700)
    directory = cache / "tunnel-evidence"
    directory.mkdir(mode=0o700)
    output = tmp_path / "output"
    output.mkdir()
    monkeypatch.setattr(ssm_cleanup, "_validate_cache_directory", lambda path: None)
    monkeypatch.setattr(ssm_cleanup, "_no_acl", lambda *args: None)
    clock = SimpleNamespace(now=0)
    monkeypatch.setattr(ssm_cleanup.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(
        ssm_cleanup.time, "sleep", lambda value: setattr(clock, "now", clock.now + value)
    )
    captured = {
        "SessionId": "owned-session",
        "Target": "i-owned",
        "Reason": "stelvio/" + "a" * 16 + "/" + "b" * 32,
    }
    receipt = "b" * 32
    start = directory / f"transport-start-{receipt}.json"
    observed = directory / f"transport-observed-{receipt}.json"
    stop = directory / f"transport-stop-{receipt}.json"
    for path, value in (
        (start, {"instance": "i-owned", "reason": captured["Reason"]}),
        (observed, {"session_id": "owned-session"}),
        (stop, {"complete": True}),
    ):
        path.write_text(json.dumps(value))
        path.chmod(0o600)
    history = captured | {"Status": "Terminated"}
    calls = []

    def active():
        return []

    def pages(**query):
        calls.append(query)
        if query["State"] == "Active":
            return active()
        return [{"Sessions": [history]}]

    client = Mock()
    client.get_paginator.return_value.paginate.side_effect = pages
    state = SimpleNamespace(
        directory=directory,
        output=output,
        clock=clock,
        captured=captured,
        start=start,
        observed=observed,
        stop=stop,
        history=history,
        calls=calls,
        client=client,
    )

    def set_active(function):
        nonlocal active
        active = function

    state.set_active = set_active
    return state


def audit(world, **kwargs):
    return ssm_cleanup.assert_ssm_cleanup(
        world.client, [world.captured], world.directory, world.output, **kwargs
    )


def evidence(world):
    return json.loads(next(world.output.glob("ssm-cleanup-*.json")).read_text())


def test_acknowledged_inactive_session_requires_three_observations(world):
    path = audit(world)
    data = json.loads(path.read_text())
    assert data["functional_cleanup"]
    assert data["history_finalized"]
    assert len(data["observations"]) == 3
    assert world.clock.now == 2
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.stat().st_nlink == 1
    assert data["completion_receipts"]["owned-session"]["stop"] == {"complete": True}
    assert audit(world) != path


def test_delayed_history_warns_only_after_all_other_evidence_passes(world):
    world.history.update(Status="Terminating", EndDate=datetime.now(UTC))
    with pytest.warns(UserWarning, match="history finalization delayed"):
        path = audit(world)
    data = json.loads(path.read_text())
    assert data["functional_cleanup"]
    assert not data["history_finalized"]
    assert data["observations"][-1]["history"]["owned-session"]["EndDate"]


@pytest.mark.parametrize("phase", ["start", "observed", "stop"])
def test_missing_receipt_never_establishes_cleanup(world, phase):
    getattr(world, phase).unlink()
    with pytest.raises(AssertionError, match="not certified"):
        audit(world, timeout=4)
    assert not evidence(world)["functional_cleanup"]
    assert world.clock.now == 4
    world.client.get_paginator.assert_not_called()


def test_completion_receipt_can_arrive_during_shutdown(world, monkeypatch):
    world.stop.unlink()

    def sleep(value):
        world.clock.now += value
        world.stop.write_text('{"complete":true}')
        world.stop.chmod(0o600)

    monkeypatch.setattr(ssm_cleanup.time, "sleep", sleep)
    assert audit(world)
    assert world.clock.now == 3


@pytest.mark.parametrize(
    "attack",
    [
        "symlink",
        "hardlink",
        "permissions",
        "oversize",
        "directory",
        "malformed",
        "duplicate",
        "false",
        "foreign-owner",
        "acl",
        "fifo",
    ],
)
def test_unsafe_receipts_fail_and_retain_private_evidence(world, attack, monkeypatch):
    if attack == "symlink":
        world.stop.unlink()
        world.stop.symlink_to(world.start)
    elif attack == "hardlink":
        os.link(world.stop, world.directory / "foreign-link")
    elif attack == "permissions":
        world.stop.chmod(0o644)
    elif attack == "directory":
        world.directory.chmod(0o755)
    elif attack == "foreign-owner":
        monkeypatch.setattr(ssm_cleanup.os, "geteuid", lambda: -1)
    elif attack == "acl":

        def acl(*args):
            raise AssertionError("Unsafe ACL")

        monkeypatch.setattr(ssm_cleanup, "_no_acl", acl)
    elif attack == "fifo":
        world.stop.unlink()
        os.mkfifo(world.stop, 0o600)
    else:
        text = {
            "oversize": "x" * 4097,
            "malformed": "{",
            "duplicate": '{"complete":false,"complete":true}',
            "false": '{"complete":false}',
        }[attack]
        world.stop.write_text(text)
    with pytest.raises((AssertionError, OSError, ValueError, RuntimeError)):
        audit(world)
    data = evidence(world)
    assert not data["functional_cleanup"]
    assert data["error_type"]
    assert world.stop.exists()


@pytest.mark.parametrize(
    ("phase", "value"),
    [
        ("start", {"instance": "i-foreign", "reason": "foreign"}),
        ("observed", {"session_id": "foreign"}),
    ],
)
def test_foreign_receipt_fails_ownership(world, phase, value):
    getattr(world, phase).write_text(json.dumps(value))
    with pytest.raises(AssertionError, match="ownership"):
        audit(world)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("SessionId", "foreign"),
        ("Target", "i-foreign"),
        ("Reason", "foreign"),
        ("Status", "Connected"),
        ("Status", "Failed"),
    ],
)
def test_invalid_history_cannot_be_a_warning(world, field, value):
    world.history[field] = value
    with pytest.raises(AssertionError):
        audit(world)
    assert not evidence(world)["functional_cleanup"]


@pytest.mark.parametrize("end", [None, "not-a-date", datetime(2026, 1, 1)])
def test_terminating_requires_a_valid_end_time(world, end):
    world.history.update(Status="Terminating", EndDate=end)
    with pytest.raises(AssertionError):
        audit(world)


def test_active_session_on_later_page_fails_even_with_completion_receipts(world):
    world.set_active(
        lambda: [
            {"Sessions": [], "NextToken": "next"},
            {"Sessions": [world.captured | {"Status": "Connected"}]},
        ]
    )
    with pytest.raises(AssertionError, match="not certified"):
        audit(world, timeout=4)
    assert all(row["consecutive_absent"] == 0 for row in evidence(world)["observations"])


def test_reappearing_active_session_resets_absence_count(world):
    turns = iter([False, True, False, False, False])
    world.set_active(
        lambda: [
            {"Sessions": [world.captured | {"Status": "Disconnected"}] if next(turns) else []}
        ]
    )
    audit(world)
    assert [row["consecutive_absent"] for row in evidence(world)["observations"]] == [
        1,
        0,
        1,
        2,
        3,
    ]


def test_unrelated_sessions_and_receipts_are_not_changed(world):
    foreign = world.directory / "transport-stop-foreign.json"
    foreign.write_text("unrelated")
    world.set_active(
        lambda: [{"Sessions": [{"SessionId": "foreign", "TokenValue": "never-persist"}]}]
    )
    path = audit(world)
    assert "never-persist" not in path.read_text()
    assert foreign.read_text() == "unrelated"


def test_api_failure_retains_evidence(world):
    world.client.get_paginator.side_effect = RuntimeError("service unavailable")
    with pytest.raises(RuntimeError):
        audit(world)
    assert evidence(world)["error_type"] == "RuntimeError"
    assert not evidence(world)["functional_cleanup"]


def test_history_missing_never_establishes_cleanup(world):
    world.client.get_paginator.return_value.paginate.side_effect = lambda **kwargs: [
        {"Sessions": []}
    ]
    with pytest.raises(AssertionError):
        audit(world, timeout=4)


def test_two_sessions_are_independently_certified(world):
    second = {
        "SessionId": "second-session",
        "Target": "i-second",
        "Reason": "stelvio/" + "a" * 16 + "/" + "c" * 32,
    }
    for phase, value in (
        ("start", {"instance": second["Target"], "reason": second["Reason"]}),
        ("observed", {"session_id": second["SessionId"]}),
        ("stop", {"complete": True}),
    ):
        path = world.directory / f"transport-{phase}-{'c' * 32}.json"
        path.write_text(json.dumps(value))
        path.chmod(0o600)
    history = {
        "owned-session": world.history,
        "second-session": second | {"Status": "Terminating", "EndDate": datetime.now(UTC)},
    }

    def pages(**query):
        if query["State"] == "Active":
            return [{"Sessions": []}]
        return [{"Sessions": [history[query["Filters"][0]["value"]]]}]

    world.client.get_paginator.return_value.paginate.side_effect = pages
    with pytest.warns(UserWarning, match="history finalization delayed"):
        path = ssm_cleanup.assert_ssm_cleanup(
            world.client, [world.captured, second], world.directory, world.output
        )
    data = json.loads(path.read_text())
    assert set(data["completion_receipts"]) == set(history)
    assert data["functional_cleanup"]
    assert not data["history_finalized"]


@pytest.mark.parametrize("case", ["missing", "duplicate", "invalid-reason"])
def test_invalid_captured_sessions_cannot_pass(world, case):
    receipts = [] if case == "missing" else [world.captured, world.captured]
    if case == "invalid-reason":
        receipts = [world.captured | {"Reason": "foreign"}]
    with pytest.raises(AssertionError):
        ssm_cleanup.assert_ssm_cleanup(world.client, receipts, world.directory, world.output)
    assert not evidence(world)["functional_cleanup"]


def test_unbounded_pagination_fails(world):
    world.set_active(lambda: [{"Sessions": [], "NextToken": "more"}] * 21)
    with pytest.raises(AssertionError, match="pagination"):
        audit(world)


def test_cli_cleanup_keeps_ssm_io_bounded(world):
    from .cli_session import StandaloneCliSession

    session = object.__new__(StandaloneCliSession)
    session.aws = Mock()
    session.project = world.output
    session.aws.client.return_value = world.client
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("stelvio.pulumi.get_bin_path", lambda: world.directory.parent)
        path = session.assert_sessions_terminated([world.captured])
    assert path.is_file()
    config = session.aws.client.call_args.kwargs["config"]
    assert config.connect_timeout == 2
    assert config.read_timeout == 2
    assert config.retries == {"total_max_attempts": 1}


def test_history_request_finishing_after_deadline_cannot_pass(world):
    original = world.client.get_paginator.return_value.paginate.side_effect

    def slow_pages(**query):
        if query["State"] == "History" and world.clock.now == 2:
            world.clock.now = 61
        yield from original(**query)

    world.client.get_paginator.return_value.paginate.side_effect = slow_pages
    with pytest.raises(AssertionError, match="deadline"):
        audit(world)
    assert not evidence(world)["functional_cleanup"]


def test_success_is_checked_against_deadline_after_observation(world, monkeypatch):
    original = ssm_cleanup._history

    def history(*args):
        result = original(*args)
        if world.clock.now == 2:
            world.clock.now = 61
        return result

    monkeypatch.setattr(ssm_cleanup, "_history", history)
    with pytest.raises(AssertionError, match="deadline"):
        audit(world)
    assert not evidence(world)["functional_cleanup"]
