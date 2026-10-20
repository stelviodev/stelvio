"""The dev command owns networking across bridge startup, exit, and interruption."""

import pytest

from stelvio.exceptions import StelvioProjectError
from tests.cli_test_helpers import FakeCommandRun


def _install_run(monkeypatch, cli_commands, events):
    class Network:
        def start(self):
            events.append("network-start")

        def admit(self, endpoint):
            events.append(("admit", endpoint))

        def close(self):
            events.append("network-close")

    network = Network()

    class Run(FakeCommandRun):
        def __enter__(self):
            events.append("deployment-enter")
            return self

        def __exit__(self, *_args):
            events.append("deployment-exit")
            return False

        def capture_network_session(self, report):
            events.append("capture")
            assert callable(report)
            return network

    run = Run({"checkpoint": {"latest": {"resources": []}}})
    cli_commands.CommandRun.return_value = run
    monkeypatch.setattr(cli_commands.ProviderStore, "region", lambda: "us-east-1")
    monkeypatch.setattr(cli_commands, "_clean_stale_caches", lambda: None)
    return network


def test_dev_captures_before_cleanup_starts_before_bridge_and_closes(cli_commands, monkeypatch):
    events = []
    network = _install_run(monkeypatch, cli_commands, events)

    def bridge(**options):
        events.append("bridge")
        assert options == {
            "region": "us-east-1",
            "profile": "default",
            "app_name": "demo",
            "env": "dev",
            "admission": network.admit,
        }

    monkeypatch.setattr(cli_commands, "run_bridge_server", bridge)

    cli_commands.run_dev("dev")

    assert events == [
        "deployment-enter",
        "capture",
        "deployment-exit",
        "network-start",
        "bridge",
        "network-close",
    ]


@pytest.mark.parametrize("failure", [KeyboardInterrupt(), RuntimeError("bridge failed")])
def test_dev_closes_network_after_bridge_failure(cli_commands, monkeypatch, failure):
    events = []
    _install_run(monkeypatch, cli_commands, events)

    def bridge(**_options):
        events.append("bridge")
        raise failure

    monkeypatch.setattr(cli_commands, "run_bridge_server", bridge)

    with pytest.raises(type(failure), match=str(failure)):
        cli_commands.run_dev("dev")

    assert events[-2:] == ["bridge", "network-close"]


def test_dev_global_network_start_failure_skips_bridge_and_closes(cli_commands, monkeypatch):
    events = []
    network = _install_run(monkeypatch, cli_commands, events)

    def fail_start():
        events.append("network-start")
        raise RuntimeError("pinned transport artifact is unavailable")

    network.start = fail_start
    monkeypatch.setattr(
        cli_commands, "run_bridge_server", lambda **_options: events.append("bridge")
    )

    with pytest.raises(StelvioProjectError, match="pinned transport artifact"):
        cli_commands.run_dev("dev")

    assert events == [
        "deployment-enter",
        "capture",
        "deployment-exit",
        "network-start",
        "network-close",
    ]


def test_dev_surfaces_uncertified_cleanup(cli_commands, monkeypatch):
    events = []
    network = _install_run(monkeypatch, cli_commands, events)

    def fail_close():
        events.append("network-close")
        raise RuntimeError("VPC networking cleanup incomplete; preserve recovery ownership")

    network.close = fail_close
    monkeypatch.setattr(cli_commands, "run_bridge_server", lambda **_options: None)

    with pytest.raises(StelvioProjectError, match="preserve recovery ownership"):
        cli_commands.run_dev("dev")

    assert events[-1] == "network-close"


def test_dev_closes_captured_session_if_deployment_context_exit_fails(cli_commands, monkeypatch):
    events = []

    class Network:
        def start(self):
            events.append("network-start")

        def admit(self, _endpoint):
            return None

        def close(self):
            events.append("network-close")

    network = Network()

    class Run(FakeCommandRun):
        def __enter__(self):
            events.append("deployment-enter")
            return self

        def __exit__(self, *_args):
            events.append("deployment-exit")
            raise RuntimeError("temporary deployment cleanup failed")

        def capture_network_session(self, _report):
            events.append("capture")
            return network

    cli_commands.CommandRun.return_value = Run({"checkpoint": {"latest": {"resources": []}}})
    monkeypatch.setattr(cli_commands.ProviderStore, "region", lambda: "us-east-1")

    with pytest.raises(RuntimeError, match="temporary deployment cleanup failed"):
        cli_commands.run_dev("dev")

    assert events == ["deployment-enter", "capture", "deployment-exit", "network-close"]
