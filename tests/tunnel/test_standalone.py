"""Standalone tunnel command lifecycle and state capture."""

from types import SimpleNamespace
from unittest.mock import Mock

from click.testing import CliRunner
from pytest import mark

from stelvio.cli.tunnel import tunnel


def _install_command_run(monkeypatch, *, deployed=True, session=None, events=None):
    events = events if events is not None else []

    class Run:
        has_deployed = deployed

        def __enter__(self):
            events.append("enter")
            return self

        def __exit__(self, *_args):
            events.append("release")
            return False

        def capture_network_session(self, report):
            events.append("capture")
            self.report = report
            return session

    factory = Mock(
        side_effect=lambda env, **kwargs: (events.append(("command", env, kwargs)) or Run())
    )
    monkeypatch.setattr("stelvio.command_run.CommandRun", factory)
    return factory, events


def test_start_releases_state_context_before_wait_and_closes_on_interrupt(monkeypatch):
    events = []
    network = SimpleNamespace(
        description=object(),
        start=lambda: events.append("start"),
        close=lambda: events.append("close"),
    )

    def wait():
        events.append("wait")
        raise KeyboardInterrupt

    network.wait = wait
    factory, events = _install_command_run(monkeypatch, session=network, events=events)
    monkeypatch.setattr("stelvio.cli.determine_env", lambda env, **kwargs: env)

    result = CliRunner().invoke(tunnel, ["start", "staging"])

    assert result.exit_code == 0, result.output
    assert events == [
        ("command", "staging", {"state_only": True}),
        "enter",
        "capture",
        "release",
        "start",
        "wait",
        "close",
    ]
    assert "Tunnel stopped." in result.output
    assert factory.call_count == 1


def test_start_requires_deployed_state_before_capturing_or_starting(monkeypatch):
    events = []
    _, events = _install_command_run(monkeypatch, deployed=False, events=events)
    monkeypatch.setattr("stelvio.cli.determine_env", lambda env, **kwargs: env)

    result = CliRunner().invoke(tunnel, ["start", "staging"])

    assert result.exit_code == 1
    assert "No deployed environment 'staging'" in result.output
    assert "capture" not in events
    assert "start" not in events


def test_start_without_eligible_dependencies_is_successful_no_op(monkeypatch):
    events = []
    network = SimpleNamespace(
        description=None,
        start=lambda: events.append("start"),
        wait=lambda: events.append("wait"),
        close=lambda: events.append("close"),
    )
    _install_command_run(monkeypatch, session=network, events=events)
    monkeypatch.setattr("stelvio.cli.determine_env", lambda env, **kwargs: env)

    result = CliRunner().invoke(tunnel, ["start", "staging"])

    assert result.exit_code == 0, result.output
    assert events == [
        ("command", "staging", {"state_only": True}),
        "enter",
        "capture",
        "release",
        "start",
        "close",
    ]
    assert "wait" not in events


@mark.parametrize(
    ("failure", "message"),
    [("start", "private plugin is invalid"), ("wait", "VPC access failed: vpc-1")],
)
def test_start_reports_runtime_failure_and_closes_session(monkeypatch, failure, message):
    events = []

    def fail():
        events.append(failure)
        raise RuntimeError(message)

    network = SimpleNamespace(
        description=object(),
        start=fail if failure == "start" else lambda: events.append("start"),
        wait=fail if failure == "wait" else lambda: events.append("wait"),
        close=lambda: events.append("close"),
    )
    _install_command_run(monkeypatch, session=network, events=events)
    monkeypatch.setattr("stelvio.cli.determine_env", lambda env, **kwargs: env)

    result = CliRunner().invoke(tunnel, ["start", "staging"])

    assert result.exit_code == 1
    assert message in result.output
    assert "close" in events
    assert "Traceback" not in result.output


def test_start_surfaces_uncertified_cleanup_failure(monkeypatch):
    events = []

    def close():
        events.append("close")
        raise RuntimeError("VPC networking cleanup incomplete; preserve recovery ownership")

    network = SimpleNamespace(
        description=object(), start=lambda: events.append("start"), wait=lambda: None, close=close
    )
    _install_command_run(monkeypatch, session=network, events=events)
    monkeypatch.setattr("stelvio.cli.determine_env", lambda env, **kwargs: env)

    result = CliRunner().invoke(tunnel, ["start", "staging"])

    assert result.exit_code == 1
    assert "preserve recovery ownership" in result.output
    assert "Tunnel stopped." not in result.output


def test_start_uses_cli_default_environment_resolution(monkeypatch):
    events = []
    network = SimpleNamespace(
        description=None,
        start=lambda: events.append("start"),
        wait=Mock(),
        close=lambda: events.append("close"),
    )
    factory, _ = _install_command_run(monkeypatch, session=network, events=events)
    resolve = Mock(return_value="personal")
    monkeypatch.setattr("stelvio.cli.determine_env", resolve)

    result = CliRunner().invoke(tunnel, ["start"])

    assert result.exit_code == 0, result.output
    resolve.assert_called_once_with(None, require_explicit_in_ci=True, command_name="tunnel start")
    factory.assert_called_once_with("personal", state_only=True)


def test_command_run_captures_state_and_identity_inputs(monkeypatch):
    from stelvio.command_run import CommandRun

    created = []
    monkeypatch.setattr(
        "stelvio.command_run.NetworkSession",
        lambda **kwargs: created.append(kwargs) or object(),
    )
    run = object.__new__(CommandRun)
    run.env = "staging"
    run._app_name = "payments"
    run.load_state = lambda: {"checkpoint": {"latest": {"resources": []}}}
    report = Mock()

    result = run.capture_network_session(report)

    assert result is not None
    assert created == [
        {
            "state": {"checkpoint": {"latest": {"resources": []}}},
            "app": "payments",
            "env": "staging",
            "report": report,
        }
    ]
