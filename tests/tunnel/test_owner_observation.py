"""Acceptance observers use the owner's report, never a foreign active lease."""

import hashlib
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from stelvio.tunnel.helper_client import HelperBusyError, HelperError
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
