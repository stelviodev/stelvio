"""Session admission and captured provider inputs at the CLI/bridge boundary."""

import asyncio
import json
import time
from copy import deepcopy
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock

from pytest import fixture, mark, raises

from stelvio.bridge.local.dtos import BridgeInvocationResult
from stelvio.bridge.local.listener import _handle_data_message
from stelvio.command_run import CommandRun
from stelvio.component import BridgeableMixin
from stelvio.exceptions import StelvioValidationError
from stelvio.tunnel.session import DevNetworkSession, VpcUnavailableError


@fixture
def network_state():
    resources = []
    for index in (0, 1):
        identity = f"vpc-{index}"
        resources.append(
            {
                "urn": identity,
                "type": "stelvio:aws:Vpc",
                "outputs": {
                    "_stelvio_network": {
                        "version": 1,
                        "kind": "vpc",
                        "identity": identity,
                        "account": "123456789012",
                        "region": "us-east-1",
                        "vpc_id": f"vpc-0123456789abcdef{index}",
                        "provider": "provider",
                        "cidrs": [f"10.{index}.0.0/16"],
                        "policy": "temporary",
                        "dns_domains": [],
                        "public_subnets": [
                            {
                                "id": f"subnet-0123456789abcdef{index}",
                                "az": "us-east-1a",
                                "cidr": f"10.{index}.0.0/24",
                            }
                        ],
                    }
                },
            }
        )
    for name, vpcs in [("both", ["vpc-0", "vpc-1"]), ("healthy", ["vpc-1"]), ("plain", [])]:
        resources.append(
            {
                "urn": name,
                "type": "stelvio:aws:Function",
                "outputs": {
                    "_stelvio_network": {
                        "version": 1,
                        "kind": "endpoint",
                        "identity": name,
                        "endpoint_id": name,
                        "vpcs": vpcs,
                    }
                },
            }
        )
    resources.append(
        {"urn": "provider", "type": "pulumi:providers:aws", "inputs": {"profile": "startup"}}
    )
    return {"checkpoint": {"latest": {"resources": resources}}}


def wait_for(predicate):
    deadline = time.monotonic() + 3
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("Network status did not settle")
        time.sleep(0.01)


@fixture
def world(monkeypatch, tmp_path):
    messages = []
    statuses = [
        {"identity": f"vpc-{i}", "state": "ready", "attempt": 1, "cause": None} for i in (0, 1)
    ]
    launches = []
    closed = []
    monkeypatch.setattr("stelvio.tunnel.credentials.os.geteuid", lambda: 1000)
    monkeypatch.setattr("stelvio.tunnel.credentials.os.getuid", lambda: 1000)
    monkeypatch.setattr("stelvio.tunnel.session.install_helper", lambda: None)

    def launch(description, contexts, **kw):
        launches.append((description, contexts, kw))
        return SimpleNamespace(
            status=lambda: deepcopy(statuses), close=lambda: closed.append(True)
        )

    monkeypatch.setattr("stelvio.tunnel.session.NetworkRuntime", launch)
    return SimpleNamespace(
        messages=messages,
        statuses=statuses,
        launches=launches,
        closed=closed,
        make=lambda state: DevNetworkSession(
            state=state,
            app="app",
            env="dev",
            home={
                "account": "123456789012",
                "region": "us-east-1",
                "profile": None,
                "bucket": "home",
            },
            config_dir=tmp_path,
            report=messages.append,
        ),
    )


def test_partial_failure_all_dependencies_and_startup_context(world, network_state, monkeypatch):
    monkeypatch.setenv("AWS_PROFILE", "ORIGINAL")
    session = world.make(network_state)
    assert isinstance(session.admit("both"), VpcUnavailableError)
    assert session.admit("plain") is None
    monkeypatch.setenv("AWS_PROFILE", "HANDLER_MUTATION")
    session.start()
    try:
        wait_for(lambda: session.admit("both") is None)
        contexts = world.launches[0][1]
        assert all(c.process_environment()["AWS_PROFILE"] == "ORIGINAL" for c in contexts)
        assert contexts[0].profile == "startup"
        world.statuses[0].update(state="failed", cause="identity-or-configuration")
        wait_for(lambda: isinstance(session.admit("both"), VpcUnavailableError))
        assert session.admit("healthy") is None
        assert session.admit("plain") is None
        assert "Network DEGRADED" in world.messages
        world.statuses[0].update(state="ready", attempt=2, cause=None)
        wait_for(lambda: session.admit("both") is None)
    finally:
        session.close()
    session.close()
    assert world.closed == [True]
    assert isinstance(session.admit("healthy"), VpcUnavailableError)


def test_command_capture_survives_removed_state_and_handler_credentials(
    world, network_state, monkeypatch, tmp_path
):
    run = SimpleNamespace(
        load_state=lambda: network_state,
        app_name="app",
        env="dev",
        _home=SimpleNamespace(
            network_home=lambda: {
                "bucket": "home",
                "account": "123456789012",
                "region": "us-east-1",
                "profile": None,
            }
        ),
    )
    monkeypatch.setattr("stelvio.command_run.get_stelvio_config_dir", lambda: tmp_path)
    monkeypatch.setenv("AWS_PROFILE", "ORIGINAL")
    session = CommandRun.capture_network_session(run, world.messages.append)
    network_state.clear()
    monkeypatch.setenv("AWS_PROFILE", "HANDLER")
    session.start()
    try:
        wait_for(lambda: session.admit("both") is None)
        assert [v.identity for v in world.launches[0][0].manifest.enabled_vpcs] == [
            "vpc-0",
            "vpc-1",
        ]
        assert all(
            c.process_environment()["AWS_PROFILE"] == "ORIGINAL" for c in world.launches[0][1]
        )
    finally:
        session.close()


def test_cleanup_failure_reports_exact_recovery_and_is_idempotent(world, network_state):
    session = world.make(network_state)
    session.start()

    def failed():
        raise RuntimeError("retained temporary unit")

    session.runtime.close = failed
    with raises(RuntimeError, match="VPC dev cleanup incomplete"):
        session.close()
    session.close()
    assert any(m == "Host recovery: stlv tunnel reconcile" for m in world.messages)
    command = next(m for m in world.messages if m.startswith("AWS recovery:"))
    assert "--bucket home --home-account 123456789012 --home-region us-east-1" in command
    assert (
        f"--app app --env dev --session {session.description.session_id} --profile startup"
        in command
    )


@mark.parametrize("bad", ["sharedConfigFiles", "assumeRoleWithWebIdentity", "httpsProxy"])
def test_unsupported_provider_inputs_refused_before_runtime(world, network_state, bad):
    network_state["checkpoint"]["latest"]["resources"][-1]["inputs"][bad] = "override"
    with raises(StelvioValidationError, match="AWS SDK startup credential chain"):
        world.make(network_state)
    assert not world.launches


def test_mixed_disabled_vpc_is_bypassed_without_degrading(world, network_state):
    network_state["checkpoint"]["latest"]["resources"][0]["outputs"]["_stelvio_network"][
        "policy"
    ] = "disabled"
    world.statuses.pop(0)
    session = world.make(network_state)
    session.start()
    try:
        wait_for(lambda: session.admit("both") is None)
        assert "Network READY" in world.messages
        assert "Network DEGRADED" not in world.messages
    finally:
        session.close()


@mark.parametrize("bad_snapshot", ["duplicate", "missing", "invalid-attempt"])
def test_unverifiable_status_revokes_admission(world, network_state, bad_snapshot):
    session = world.make(network_state)
    session.start()
    try:
        wait_for(lambda: session.admit("both") is None)
        if bad_snapshot == "duplicate":
            world.statuses.append(deepcopy(world.statuses[0]))
        elif bad_snapshot == "missing":
            world.statuses.pop(0)
        else:
            world.statuses[0]["attempt"] = True
        wait_for(lambda: isinstance(session.admit("both"), VpcUnavailableError))
        assert session.admit("plain") is None
    finally:
        session.close()


def test_unresponsive_status_expires_admission_with_bounded_rejection(
    world, network_state, monkeypatch
):
    session = world.make(network_state)
    blocked = Event()
    release = Event()
    monkeypatch.setattr("stelvio.tunnel.session.STATUS_MAX_AGE", 0.05)
    session.start()
    try:
        wait_for(lambda: session.admit("both") is None)

        def unavailable():
            blocked.set()
            assert release.wait(timeout=5)
            return deepcopy(world.statuses)

        session.runtime.status = unavailable
        assert blocked.wait(timeout=2)
        before = time.monotonic()
        assert isinstance(session.admit("both"), VpcUnavailableError)
        assert time.monotonic() - before < 0.1
        assert session.admit("plain") is None
    finally:
        release.set()
        session.close()


@mark.parametrize("boundary", ["install_helper", "NetworkRuntime"])
def test_global_start_failure_reports_recovery_before_rethrow(
    world, network_state, monkeypatch, boundary
):
    session = world.make(network_state)

    def unavailable(*args, **kwargs):
        raise RuntimeError("global startup unavailable")

    monkeypatch.setattr(f"stelvio.tunnel.session.{boundary}", unavailable)
    with raises(RuntimeError, match="global startup unavailable"):
        session.start()
    session.close()
    assert not world.launches
    assert (
        world.messages[0]
        == f"Network recovery ownership: session {session.description.session_id}"
    )
    assert world.messages[1] == "Host recovery: stlv tunnel reconcile"
    assert any(
        m.startswith("AWS recovery: stlv tunnel recover --bucket home") for m in world.messages
    )
    assert isinstance(session.admit("both"), VpcUnavailableError)


@mark.parametrize("disabled", [False, True])
def test_bypass_never_starts_helper_or_runtime(world, network_state, monkeypatch, disabled):
    if disabled:
        for r in network_state["checkpoint"]["latest"]["resources"][:2]:
            r["outputs"]["_stelvio_network"]["policy"] = "disabled"
    else:
        network_state = None

    def forbidden():
        raise AssertionError("Bypass activated the helper")

    monkeypatch.setattr("stelvio.tunnel.session.install_helper", forbidden)
    session = world.make(network_state)
    session.start()
    try:
        assert session.admit("both") is None
        assert not world.launches
        assert any("disabled" in m for m in world.messages) == disabled
    finally:
        session.close()


def test_bridge_rejection_precedes_handler_import_and_preserves_plain_dispatch(
    world, network_state, monkeypatch
):
    session = world.make(network_state)
    calls = []

    class Handler(BridgeableMixin):
        _dev_endpoint_id = "both"

        async def _handle_bridge_event(self, data):
            calls.append("handler import/execute")
            return BridgeInvocationResult({}, None, "/", "GET", 0, 200)

    published = AsyncMock()
    monkeypatch.setattr("stelvio.bridge.local.listener.publish", published)
    monkeypatch.setattr("stelvio.bridge.local.listener.log_invocation", lambda result: None)
    monkeypatch.setattr("stelvio.bridge.local.listener.WebsocketHandlers.all", lambda: [Handler()])
    data = {"event": json.dumps({"endpointId": "both", "invoke_id": "id", "event": {}})}
    asyncio.run(_handle_data_message(data, None, "key", "app", "dev", session.admit))
    assert not calls
    assert isinstance(published.call_args.args[0].error_result, VpcUnavailableError)
    session.start()
    try:
        wait_for(lambda: session.admit("both") is None)
        asyncio.run(_handle_data_message(data, None, "key", "app", "dev", session.admit))
        assert calls == ["handler import/execute"]
        assert published.call_args.args[0].success_result == {}
    finally:
        session.close()
