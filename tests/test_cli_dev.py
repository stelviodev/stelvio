"""The dev command owns networking across bridge startup, exit, and interruption."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from stelvio.aws.function import Function
from stelvio.aws.vpc import Vpc, VpcAttachment
from stelvio.bridge.local.handlers import WebsocketHandlers
from stelvio.exceptions import StelvioProjectError, StelvioValidationError
from tests.cli_test_helpers import FakeCommandRun


def _install_run(monkeypatch, cli_commands, events):
    class Network:
        manifest = SimpleNamespace(vpcs=())

        def start(self):
            events.append("network-start")

        def admit_vpcs(self, required):
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
            return False

    run = Run({"checkpoint": {"latest": {"resources": []}}})
    cli_commands.CommandRun.return_value = run

    def capture(**options):
        events.append("capture")
        assert options["state"] == run.load_state()
        assert (options["app"], options["env"]) == ("demo", "dev")
        assert callable(options["report"])
        return network

    monkeypatch.setattr(cli_commands, "NetworkSession", capture)
    monkeypatch.setattr(cli_commands.ProviderStore, "region", lambda: "us-east-1")
    monkeypatch.setattr(cli_commands, "_clean_stale_caches", lambda: None)
    return network


def test_dev_captures_before_cleanup_starts_before_bridge_and_closes(cli_commands, monkeypatch):
    events = []
    _install_run(monkeypatch, cli_commands, events)

    def bridge(**options):
        events.append("bridge")
        assert options | {"admission": None} == {
            "region": "us-east-1",
            "profile": "default",
            "app_name": "demo",
            "env": "dev",
            "admission": None,
        }
        assert options["admission"]("unknown-endpoint") is None

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
        manifest = SimpleNamespace(vpcs=())

        def start(self):
            events.append("network-start")

        def admit_vpcs(self, _required):
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

    cli_commands.CommandRun.return_value = Run({"checkpoint": {"latest": {"resources": []}}})

    def capture(**_options):
        events.append("capture")
        return network

    monkeypatch.setattr(cli_commands, "NetworkSession", capture)
    monkeypatch.setattr(cli_commands.ProviderStore, "region", lambda: "us-east-1")

    with pytest.raises(RuntimeError, match="temporary deployment cleanup failed"):
        cli_commands.run_dev("dev")

    assert events == ["deployment-enter", "capture", "deployment-exit", "network-close"]


@pytest.mark.parametrize("attachment", ["vpc", "attachment", "dict"])
def test_dev_admission_uses_loaded_function_vpc(cli_commands, pulumi_mocks, attachment):
    vpc = Vpc("net")
    configured = {
        "vpc": vpc,
        "attachment": VpcAttachment(vpc=vpc),
        "dict": {"vpc": vpc},
    }[attachment]
    function = Function("api", handler="functions/simple.handler", vpc=configured)
    WebsocketHandlers.register(function)
    identity = "urn:pulumi:dev::sample::stelvio:aws:Vpc::net"
    network = SimpleNamespace(
        manifest=SimpleNamespace(
            vpcs=(SimpleNamespace(identity=identity, bastion_instance_id="i-bastion"),)
        ),
        admit_vpcs=Mock(return_value=None),
    )

    admission = cli_commands._dev_network_admission(network, Mock())
    admission(function._dev_endpoint_id)

    network.admit_vpcs.assert_called_once_with((identity,))


def test_dev_admission_snapshots_current_function_endpoint(cli_commands, pulumi_mocks):
    function = Function("api", handler="functions/simple.handler", vpc=Vpc("net"))
    WebsocketHandlers.register(function)
    identity = "urn:pulumi:dev::sample::stelvio:aws:Vpc::net"
    network = SimpleNamespace(
        manifest=SimpleNamespace(
            vpcs=(SimpleNamespace(identity=identity, bastion_instance_id="i-bastion"),)
        ),
        admit_vpcs=Mock(return_value=None),
    )
    old_endpoint = function._dev_endpoint_id
    admission = cli_commands._dev_network_admission(network, Mock())
    function._dev_endpoint_id = "api-new-session"

    admission(old_endpoint)
    network.admit_vpcs.assert_called_with((identity,))
    admission(function._dev_endpoint_id)
    network.admit_vpcs.assert_called_with(())
    current = cli_commands._dev_network_admission(network, Mock())
    current(function._dev_endpoint_id)
    network.admit_vpcs.assert_called_with((identity,))


def test_dev_warns_once_for_referenced_vpc_without_bastion(cli_commands, pulumi_mocks):
    disabled = Vpc("disabled")
    functions = [
        Function("first", handler="functions/simple.handler", vpc=disabled),
        Function("second", handler="functions/simple.handler", vpc=disabled),
        Function("plain", handler="functions/simple.handler"),
    ]
    for function in functions:
        WebsocketHandlers.register(function)
    identity = "urn:pulumi:dev::sample::stelvio:aws:Vpc::disabled"
    network = SimpleNamespace(
        manifest=SimpleNamespace(
            vpcs=(
                SimpleNamespace(identity=identity, bastion_instance_id=None),
                SimpleNamespace(
                    identity="urn:pulumi:dev::sample::stelvio:aws:Vpc::unused",
                    bastion_instance_id=None,
                ),
            )
        ),
        admit_vpcs=Mock(return_value=None),
    )
    report = Mock()

    admission = cli_commands._dev_network_admission(network, report)
    for function in functions:
        admission(function._dev_endpoint_id)

    report.assert_called_once_with(
        f"VPC access disabled: {identity}; local networking is your responsibility."
    )
    assert network.admit_vpcs.call_count == 3
    assert all(call.args == ((),) for call in network.admit_vpcs.call_args_list)


@pytest.mark.parametrize("count", [0, 2], ids=["missing", "ambiguous"])
def test_dev_rejects_unmatched_function_vpc_record(cli_commands, pulumi_mocks, count):
    function = Function("api", handler="functions/simple.handler", vpc=Vpc("net"))
    WebsocketHandlers.register(function)
    record = SimpleNamespace(
        identity="urn:pulumi:dev::sample::stelvio:aws:Vpc::net", bastion_instance_id="i-bastion"
    )
    network = SimpleNamespace(manifest=SimpleNamespace(vpcs=(record,) * count))

    with pytest.raises(
        StelvioValidationError, match=r"Function 'api' requires Vpc 'net'.*redeploy"
    ):
        cli_commands._dev_network_admission(network, Mock())
