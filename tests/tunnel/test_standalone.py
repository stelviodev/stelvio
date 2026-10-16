"""Standalone tunneling reads deployed state without deploying or running a bridge."""

from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

from click.testing import CliRunner
from pytest import mark


def test_start_captures_state_releases_context_and_closes_on_interrupt(monkeypatch):
    from stelvio.cli.tunnel import tunnel

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
    run = MagicMock()
    run.__enter__.return_value = SimpleNamespace(
        has_deployed=True,
        capture_network_session=lambda report: network,
    )
    run.__exit__.side_effect = lambda *args: events.append("release") or False
    factory = Mock(return_value=run)
    monkeypatch.setattr("stelvio.command_run.CommandRun", factory)
    result = CliRunner().invoke(tunnel, ["start", "staging"])
    assert result.exit_code == 0, result.output
    assert events == ["release", "start", "wait", "close"]
    factory.assert_called_once_with("staging", state_only=True)


@mark.parametrize("deployed", [False, True])
def test_start_without_deployment_or_enabled_dependencies_never_waits(monkeypatch, deployed):
    from stelvio.cli.tunnel import tunnel

    network = SimpleNamespace(description=None, start=Mock(), wait=Mock(), close=Mock())
    run = MagicMock()
    capture = Mock(return_value=network)
    run.__enter__.return_value = SimpleNamespace(
        has_deployed=deployed, capture_network_session=capture
    )
    monkeypatch.setattr("stelvio.command_run.CommandRun", Mock(return_value=run))
    result = CliRunner().invoke(tunnel, ["start", "staging"])
    assert result.exit_code == (0 if deployed else 1), result.output
    network.wait.assert_not_called()
    if deployed:
        network.start.assert_called_once_with()
        network.close.assert_called_once_with()
    else:
        capture.assert_not_called()
        network.start.assert_not_called()
        network.close.assert_not_called()


def test_start_reports_legacy_metadata_validation_without_traceback(monkeypatch):
    from stelvio.cli.tunnel import tunnel
    from stelvio.exceptions import StelvioValidationError

    run = MagicMock()
    run.__enter__.return_value = SimpleNamespace(
        has_deployed=True,
        capture_network_session=Mock(
            side_effect=StelvioValidationError("set bastion=True and redeploy")
        ),
    )
    monkeypatch.setattr("stelvio.command_run.CommandRun", Mock(return_value=run))
    result = CliRunner().invoke(tunnel, ["start", "staging"])
    assert result.exit_code == 1
    assert result.output == "Error: set bastion=True and redeploy\n"
