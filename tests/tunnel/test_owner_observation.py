"""Acceptance observers use the owner's report, never a foreign active lease."""

import hashlib
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from stelvio.tunnel.helper_client import (
    HelperBusyError,
    HelperError,
    HelperUnitState,
    HelperUnitStatus,
)
from tests.integration.cli_session import StandaloneCliSession

IDENTITY = "test-vpc"
UNIT = hashlib.sha256(IDENTITY.encode()).hexdigest()[:8]


def observer(monkeypatch, lines):
    session = object.__new__(StandaloneCliSession)
    session.process = Mock()
    session.process.poll.return_value = None
    monkeypatch.setattr(session, "log", lambda: "\n".join(lines))
    monkeypatch.setattr(
        "tests.integration.cli_session.NativeHelper",
        Mock(side_effect=AssertionError("Active observer must not connect to the helper")),
    )
    return session


def line(generation, status):
    return f"  Host unit {UNIT} generation={generation} status={status}"


@pytest.mark.parametrize(("generation", "status"), [(1, "active"), (2, "retained"), (3, "active")])
def test_owner_observer_preserves_exact_generation(monkeypatch, generation, status):
    session = observer(monkeypatch, [line(generation, status)])
    unit = session.wait_unit(IDENTITY, status, after=generation - 1)
    assert unit.unit == UNIT
    assert unit.generation == generation
    assert unit.status.name.lower() == status


@pytest.mark.parametrize(
    "suffix",
    [
        line(2, "retained"),
        "Network DEGRADED: runtime status unavailable.",
        f"  Host unit {UNIT} generation=0 status=absent",
    ],
)
def test_newer_observation_invalidates_old_active_state(monkeypatch, suffix):
    session = observer(monkeypatch, [line(1, "active"), suffix])
    with pytest.raises(TimeoutError, match="last owner observation"):
        session.wait_unit(IDENTITY, "active", timeout=0.01)


def test_generation_threshold_rejects_stale_recovery(monkeypatch):
    session = observer(monkeypatch, [line(1, "active")])
    with pytest.raises(TimeoutError, match="after generation 1"):
        session.wait_unit(IDENTITY, "active", after=1, timeout=0.01)


def test_other_vpc_cannot_satisfy_observation(monkeypatch):
    session = observer(monkeypatch, [line(3, "active").replace(UNIT, "00000001")])
    with pytest.raises(TimeoutError):
        session.wait_unit(IDENTITY, "active", timeout=0.01)


def test_command_death_rejects_even_matching_old_output(monkeypatch):
    session = observer(monkeypatch, [line(3, "active")])
    session.process.poll.return_value = 1
    with pytest.raises(RuntimeError, match="exited while observing"):
        session.wait_unit(IDENTITY, "active")


def test_shutdown_waits_for_owner_rejection_then_certifies_cleanup(monkeypatch):
    helper = Mock()
    helper.status.side_effect = [
        HelperBusyError("busy"),
        SimpleNamespace(owned=True, uncertain=False, mutating=False, units=()),
        SimpleNamespace(owned=False, uncertain=False, mutating=False, units=()),
    ]
    monkeypatch.setattr("tests.integration.cli_session.NativeHelper", lambda **kw: helper)
    session = object.__new__(StandaloneCliSession)
    session.wait_for_helper_cleanup(timeout=1)
    assert helper.status.call_count == 3


def test_shutdown_uncertainty_is_not_retried(monkeypatch):
    helper = Mock()
    helper.status.return_value = SimpleNamespace(uncertain=True)
    monkeypatch.setattr("tests.integration.cli_session.NativeHelper", lambda **kw: helper)
    session = object.__new__(StandaloneCliSession)
    with pytest.raises(AssertionError, match="uncertain"):
        session.wait_for_helper_cleanup(timeout=1)
    helper.status.assert_called_once()


def test_shutdown_invalid_reply_is_not_retried(monkeypatch):
    helper = Mock()
    helper.status.side_effect = HelperError("invalid reply")
    monkeypatch.setattr("tests.integration.cli_session.NativeHelper", lambda **kw: helper)
    session = object.__new__(StandaloneCliSession)
    with pytest.raises(HelperError, match="invalid reply"):
        session.wait_for_helper_cleanup(timeout=1)


def test_busy_shutdown_deadline_still_fails(monkeypatch):
    helper = Mock()
    helper.status.side_effect = HelperBusyError("busy")
    monkeypatch.setattr("tests.integration.cli_session.NativeHelper", lambda **kw: helper)
    session = object.__new__(StandaloneCliSession)
    with pytest.raises(AssertionError, match="still owns"):
        session.wait_for_helper_cleanup(timeout=0.01)


def test_command_restart_cannot_reuse_previous_log(monkeypatch, tmp_path):
    session = object.__new__(StandaloneCliSession)
    session.process = None
    session.log_path = tmp_path / "command.log"
    session.log_path.write_text(line(1, "active"))
    session.cli = tmp_path / "stlv"
    session.environment_name = "test"
    session.environment = {}
    session.project = tmp_path
    process = Mock()
    process.poll.return_value = None
    monkeypatch.setattr(
        "tests.integration.cli_session.subprocess.Popen", Mock(return_value=process)
    )
    session.start()
    try:
        assert session.log() == ""
        with pytest.raises(TimeoutError):
            session.wait_unit(IDENTITY, "active", timeout=0.01)
    finally:
        session._log_file.close()


@pytest.mark.parametrize(
    "history",
    [
        [(1, "active"), (2, "retained"), (3, "active"), (4, "retained"), (3, "active")],
        [(5, "active"), (1, "active")],
        [(4, "retained"), (0, "absent"), (3, "active")],
    ],
)
def test_owner_observer_rejects_generation_rollback(monkeypatch, history):
    session = observer(monkeypatch, [line(generation, status) for generation, status in history])
    with pytest.raises(AssertionError, match="generation regressed"):
        session.wait_unit(IDENTITY, "active", after=0, timeout=0.01)


def test_owner_observer_accepts_multiple_complete_retry_cycles(monkeypatch):
    session = observer(
        monkeypatch,
        [
            line(1, "active"),
            line(2, "retained"),
            line(3, "active"),
            line(4, "retained"),
            line(5, "active"),
        ],
    )
    assert session.wait_unit(IDENTITY, "active", after=2).generation == 5


@pytest.mark.parametrize("cycles", [0, 1, 2])
def test_recovery_requires_fresh_readiness_and_accepts_complete_cycles(monkeypatch, cycles):
    history = ["Network READY", line(1, "active"), "Network DEGRADED", line(2, "retained")]
    offset = len("\n".join(history))
    for index in range(cycles):
        history.extend([line(3 + 2 * index, "active"), line(4 + 2 * index, "retained")])
    generation = 3 + 2 * cycles
    history.extend(["Network READY", line(generation, "active")])
    session = observer(monkeypatch, history)
    retained = HelperUnitState(UNIT, 2, HelperUnitStatus.RETAINED)
    active = session.wait_recovered_unit(IDENTITY, retained, after_log=offset, timeout=0.1)
    assert active.generation == generation


def test_recovery_rejects_active_unit_without_fresh_ready_report(monkeypatch):
    history = ["Network READY", line(1, "active"), "Network DEGRADED", line(2, "retained")]
    offset = len("\n".join(history))
    session = observer(monkeypatch, [*history, line(5, "active")])
    retained = HelperUnitState(UNIT, 2, HelperUnitStatus.RETAINED)
    with pytest.raises(TimeoutError, match="fresh Network READY"):
        session.wait_recovered_unit(IDENTITY, retained, after_log=offset, timeout=0.01)


@pytest.mark.parametrize(
    ("history", "error"),
    [
        ([line(2, "retained"), "Network READY", line(4, "active")], AssertionError),
        ([line(2, "retained"), "Network READY", line(5, "retained")], TimeoutError),
        (
            [line(2, "retained"), "Network READY", line(5, "active").replace(UNIT, "00000001")],
            TimeoutError,
        ),
    ],
)
def test_recovery_rejects_invalid_cycle_status_or_unit(monkeypatch, history, error):
    session = observer(monkeypatch, history)
    with pytest.raises(error):
        session.wait_recovered_unit(
            IDENTITY,
            HelperUnitState(UNIT, 2, HelperUnitStatus.RETAINED),
            after_log=0,
            timeout=0.01,
        )


def test_recovery_uses_one_deadline_for_readiness_and_generation(monkeypatch):
    session = observer(monkeypatch, [line(2, "retained"), "Network READY"])
    clock = [0.0]
    monkeypatch.setattr(
        "tests.integration.cli_session.time",
        SimpleNamespace(
            monotonic=lambda: clock[0],
            sleep=lambda duration: clock.__setitem__(0, clock[0] + duration),
        ),
    )
    with pytest.raises(TimeoutError, match="fresh Network READY"):
        session.wait_recovered_unit(
            IDENTITY,
            HelperUnitState(UNIT, 2, HelperUnitStatus.RETAINED),
            after_log=0,
            timeout=1,
        )
    assert 1 <= clock[0] < 1.2


def test_recovery_rejects_ready_report_superseded_by_an_outage(monkeypatch):
    history = ["Network READY", line(1, "active"), "Network DEGRADED", line(2, "retained")]
    offset = len("\n".join(history))
    session = observer(
        monkeypatch,
        [
            *history,
            "Network READY",
            line(5, "active"),
            "Network DEGRADED",
            line(6, "retained"),
            "Network DEGRADED",
            line(7, "active"),
        ],
    )
    retained = HelperUnitState(UNIT, 2, HelperUnitStatus.RETAINED)
    with pytest.raises(TimeoutError, match="fresh Network READY"):
        session.wait_recovered_unit(IDENTITY, retained, after_log=offset, timeout=0.01)


def test_recovery_uses_one_snapshot_for_readiness_and_generation(monkeypatch):
    history = ["Network READY", line(1, "active"), "Network DEGRADED", line(2, "retained")]
    offset = len("\n".join(history))
    partial_ready = "\n".join([*history, "Network READY"])
    degraded = "\n".join(
        [
            *history,
            "Network READY",
            line(3, "active"),
            "Network DEGRADED",
            line(4, "retained"),
            "Network DEGRADED",
            line(5, "active"),
        ]
    )
    session = observer(monkeypatch, [])
    snapshots = iter([partial_ready])
    session.log = lambda: next(snapshots, degraded)
    with pytest.raises(TimeoutError, match="fresh Network READY"):
        session.wait_recovered_unit(
            IDENTITY,
            HelperUnitState(UNIT, 2, HelperUnitStatus.RETAINED),
            after_log=offset,
            timeout=0.01,
        )
