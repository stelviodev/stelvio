"""Standalone tunnel command lifecycle and state capture."""

from types import SimpleNamespace
from unittest.mock import Mock

from click.testing import CliRunner
from pytest import fixture, mark

from stelvio.cli.tunnel import tunnel


def _install_command_run(monkeypatch, *, deployed=True, session=None, events=None, capture=None):
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
            return capture(report) if capture is not None else session

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


@fixture
def standalone_host_world(ssh_world, monkeypatch):
    """Drive real CLI/session/transport code with deterministic external boundaries."""
    import os
    import time
    from dataclasses import asdict, replace
    from ipaddress import IPv4Network
    from threading import Event

    from stelvio.tunnel.host_trust import HostIdentity, locked_host_trust
    from stelvio.tunnel.manifest import (
        EndpointNetwork,
        NetworkManifest,
        SessionDescription,
        SubnetNetwork,
    )
    from stelvio.tunnel.session import NetworkSession
    from stelvio.tunnel.supervisor import VpcStatus
    from stelvio.tunnel.transport import ServerIdentityError, SshTransport
    from stelvio.tunnel.wire import session_to_wire

    original, server, key = ssh_world
    network = replace(
        original.network,
        public_subnets=(
            SubnetNetwork("subnet-12345678", "us-east-1a", IPv4Network("10.254.1.0/24")),
        ),
    )
    manifest = NetworkManifest((network,), (), (EndpointNetwork("fn", "fn", (network.identity,)),))
    description = SessionDescription(
        "11111111-1111-4111-8111-111111111111", "app", "staging", os.geteuid(), manifest
    )
    records = session_to_wire(description)["records"]
    state = {
        "checkpoint": {
            "latest": {
                "resources": [
                    {
                        "urn": record["identity"],
                        "type": "stelvio:aws:Vpc"
                        if record["kind"] == "vpc"
                        else "stelvio:aws:Function",
                        "outputs": {"_stelvio_network": record},
                    }
                    for record in records
                ]
                + [{"urn": "provider", "type": "pulumi:providers:aws", "inputs": {}}]
            }
        }
    }
    runtimes = []

    class Runtime:
        def __init__(self, description, contexts):
            assert len(contexts) == 1
            self.transport = SshTransport(
                network, original.session, Event(), ownership=original.ownership
            )
            self.closed = False
            runtimes.append(self)
            try:
                self.transport.start()
                self.snapshot = VpcStatus(network.identity, "ready", 1)
            except ServerIdentityError as error:
                self.snapshot = VpcStatus(
                    network.identity, "failed", 1, "ssh-host-key", detail=str(error)
                )

        def status(self):
            return [asdict(self.snapshot)]

        def close(self):
            self.transport.close()
            self.closed = True

    monkeypatch.setattr("stelvio.tunnel.session.require_network_version", lambda *args: None)
    monkeypatch.setattr("stelvio.tunnel.session.install_helper", lambda **kwargs: None)
    monkeypatch.setattr("stelvio.tunnel.session.NetworkRuntime", Runtime)
    monkeypatch.setattr("stelvio.cli.determine_env", lambda env, **kwargs: env)
    actual_wait = NetworkSession.wait

    def interrupt_when_ready(self):
        deadline = time.monotonic() + 3
        while not self._observed:
            assert time.monotonic() < deadline, "command never received a status snapshot"
            time.sleep(0.01)
        if self.runtime.snapshot.state == "failed":
            actual_wait(self)
        raise KeyboardInterrupt

    monkeypatch.setattr(NetworkSession, "wait", interrupt_when_ready)
    _, events = _install_command_run(
        monkeypatch,
        capture=lambda report: NetworkSession(
            state=state, app="app", env="staging", report=report
        ),
    )
    target = HostIdentity("aws", network.account, network.region, network.access.instance_id)

    def pinned():
        with locked_host_trust(target, Event()) as entry:
            return entry.key

    return SimpleNamespace(
        run=lambda: CliRunner().invoke(tunnel, ["start", "staging"]),
        server=server,
        key=key,
        pinned=pinned,
        runtimes=runtimes,
        events=events,
        target=target,
        sdk=original.session,
    )


def test_separate_standalone_commands_reuse_trust_and_refuse_changed_keys(standalone_host_world):
    from stelvio.tunnel.host_trust import fingerprint

    world = standalone_host_world
    for _ in range(2):
        result = world.run()
        assert result.exit_code == 0, result.output
        assert "Network READY" in result.output
        assert "Tunnel stopped." in result.output
        assert world.pinned() == world.key
    assert len({id(runtime.transport) for runtime in world.runtimes}) == 2
    world.server.key = world.server.changed_key
    for _ in range(2):
        result = world.run()
        assert result.exit_code == 1, result.output
        assert "cause=ssh-host-key" in result.output
        assert world.target.instance_id in result.output
        assert fingerprint(world.key) in result.output
        assert fingerprint(world.server.key) in result.output
        assert "stlv tunnel trust reset" in result.output
        assert "Network READY" not in result.output
        assert "Traceback" not in result.output
        assert world.pinned() == world.key
    world.server.key = world.key
    assert world.run().exit_code == 0
    assert world.pinned() == world.key
    assert all(runtime.closed for runtime in world.runtimes)


def test_standalone_key_rotation_requires_explicit_trust_reset(standalone_host_world):
    world = standalone_host_world
    assert world.run().exit_code == 0
    world.server.key = world.server.changed_key
    assert world.run().exit_code == 1
    arguments = [
        "trust",
        "reset",
        "--partition",
        world.target.partition,
        "--account",
        world.target.account,
        "--region",
        world.target.region,
        "--instance-id",
        world.target.instance_id,
    ]
    declined = CliRunner().invoke(tunnel, arguments, input="n\n")
    assert declined.exit_code == 1
    assert world.pinned() == world.key
    assert world.run().exit_code == 1
    reset = CliRunner().invoke(tunnel, arguments, input="y\n")
    assert reset.exit_code == 0, reset.output
    assert world.pinned() is None
    assert world.run().exit_code == 0
    assert world.pinned() == world.server.changed_key
    assert all(runtime.closed for runtime in world.runtimes)


@fixture
def proof_pin_world(transport_world):
    import base64

    from stelvio.tunnel.host_trust import HOST_KEY_PREFIX, HostIdentity
    from tests.integration.cli_session import StandaloneCliSession

    transport, *_ = transport_world
    session = object.__new__(StandaloneCliSession)
    session._manifest = SimpleNamespace(enabled_vpcs=(transport.network,))
    session.aws = transport.session
    session.region = transport.network.region
    target = HostIdentity(
        "aws", transport.network.account, session.region, transport.network.access.instance_id
    )
    key = "ssh-ed25519 " + base64.b64encode(HOST_KEY_PREFIX + bytes(range(32))).decode()
    return session, target, key


def test_proof_pin_cleanup_preserves_other_instances(proof_pin_world):
    from dataclasses import replace
    from threading import Event

    from stelvio.tunnel.host_trust import locked_host_trust

    session, target, key = proof_pin_world
    other = replace(target, instance_id="i-abcdef12")
    session._initialize_proof_host_trust()
    for identity in (target, other):
        with locked_host_trust(identity, Event()) as entry:
            entry.accept(key)
    assert session.host_pins() == {target.label: key}
    session._clear_proof_host_trust()
    with locked_host_trust(target, Event()) as entry:
        assert entry.key is None
    with locked_host_trust(other, Event()) as entry:
        assert entry.key == key


def test_proof_fixture_never_claims_a_preexisting_pin(proof_pin_world):
    from threading import Event

    from pytest import raises

    from stelvio.tunnel.host_trust import locked_host_trust

    session, target, key = proof_pin_world
    with locked_host_trust(target, Event()) as entry:
        entry.accept(key)
    with raises(RuntimeError, match="already has SSH trust"):
        session._initialize_proof_host_trust()
    session._clear_proof_host_trust()
    with locked_host_trust(target, Event()) as entry:
        assert entry.key == key
