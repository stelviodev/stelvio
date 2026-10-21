"""Session admission and captured provider inputs at the CLI/bridge boundary."""

import time
from copy import deepcopy
from threading import Event
from types import SimpleNamespace
from uuid import UUID

from pytest import fixture, mark, raises

from stelvio.exceptions import StelvioValidationError
from stelvio.tunnel.manifest import VERSION
from stelvio.tunnel.session import NetworkSession, VpcUnavailableError


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
                        "version": VERSION,
                        "kind": "vpc",
                        "identity": identity,
                        "account": "123456789012",
                        "region": "us-east-1",
                        "vpc_id": f"vpc-0123456789abcdef{index}",
                        "provider": "provider",
                        "cidrs": [f"10.{index}.0.0/16"],
                        "policy": "persistent",
                        "access": {
                            "instance_id": f"i-1234567{index}",
                            "security_group_id": f"sg-1234567{index}",
                            "az": "us-east-1a",
                            "owner": identity,
                        },
                        "dns_domains": [],
                        "public_subnets": [
                            {
                                "id": f"subnet-0123456789abcdef{index}",
                                "az": "us-east-1a",
                                "cidr": f"10.{index}.0.0/24",
                            }
                        ],
                        "private_subnets": [
                            {
                                "id": f"subnet-private-{index}",
                                "az": "us-east-1a",
                                "cidr": f"10.{index}.1.0/24",
                            }
                        ],
                        "isolated_subnets": [
                            {
                                "id": f"subnet-isolated-{index}",
                                "az": "us-east-1a",
                                "cidr": f"10.{index}.2.0/24",
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
                        "version": VERSION,
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
    monkeypatch.setattr("stelvio.tunnel.session.require_network_version", lambda *a: None)
    monkeypatch.setattr("stelvio.tunnel.session.install_helper", lambda **kw: None)

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
        make=lambda state: NetworkSession(
            state=state,
            app="app",
            env="dev",
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


def test_cleanup_failure_reports_exact_recovery_and_is_idempotent(world, network_state):
    session = world.make(network_state)
    session.start()

    def failed():
        raise RuntimeError("retained temporary unit")

    session.runtime.close = failed
    with raises(
        RuntimeError,
        match="VPC networking cleanup incomplete; preserve recovery ownership",
    ):
        session.close()
    session.close()
    assert (
        world.messages[-1]
        == "Host cleanup incomplete: run stlv tunnel status, then stlv tunnel cleanup."
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
    network_state["checkpoint"]["latest"]["resources"][0]["outputs"]["_stelvio_network"][
        "access"
    ] = None
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
    assert world.messages == [
        "Host cleanup incomplete: run stlv tunnel status, then stlv tunnel cleanup."
    ]
    assert isinstance(session.admit("both"), VpcUnavailableError)


@mark.parametrize("disabled", [False, True])
def test_bypass_never_starts_helper_or_runtime(world, network_state, monkeypatch, disabled):
    if disabled:
        for r in network_state["checkpoint"]["latest"]["resources"][:2]:
            r["outputs"]["_stelvio_network"]["policy"] = "disabled"
            r["outputs"]["_stelvio_network"]["access"] = None
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


def test_standalone_wait_terminates_after_runtime_ipc_failure(world, network_state):
    session = world.make(network_state)
    session.start()
    try:
        wait_for(lambda: session.admit("both") is None)

        def dead():
            raise EOFError("runtime exited")

        session.runtime.status = dead
        wait_for(lambda: session._runtime_error is not None)
        with raises(RuntimeError, match="Network runtime unavailable: EOFError"):
            session.wait()
    finally:
        session.close()


def test_developer_override_is_captured_for_install_and_runtime(world, network_state, monkeypatch):
    monkeypatch.setenv("STLV_TRAFORO_DEV_BINARY", "/original/build")
    session = world.make(network_state)
    monkeypatch.setenv("STLV_TRAFORO_DEV_BINARY", "/handler/mutation")
    captured = []

    def install(*, environment):
        captured.append(environment["STLV_TRAFORO_DEV_BINARY"])
        return SimpleNamespace(notice="Using developer Traforo abc123")

    monkeypatch.setattr("stelvio.tunnel.session.install_helper", install)
    try:
        session.start()
        assert captured == ["/original/build"]
        assert (
            world.launches[0][1][0].process_environment()["STLV_TRAFORO_DEV_BINARY"]
            == "/original/build"
        )
        assert "Using developer Traforo abc123" in world.messages
    finally:
        session.close()


def test_incompatible_binary_version_never_installs_or_starts_runtime(
    world, network_state, monkeypatch
):
    from stelvio.tunnel.assets import NativeAssetError

    seen = []

    def missing(environment):
        seen.append(environment)
        raise NativeAssetError("Traforo must report v0.0.7; rebuild local image")

    def forbidden(**options):
        raise AssertionError("installation must not run")

    monkeypatch.setattr("stelvio.tunnel.session.require_network_version", missing)
    monkeypatch.setattr("stelvio.tunnel.session.install_helper", forbidden)
    session = world.make(network_state)
    try:
        with raises(NativeAssetError, match="must report"):
            session.start()
        assert len(seen) == 1
        assert world.launches == []
    finally:
        session.close()


def test_private_plugin_failure_is_reported_before_runtime_launch(
    world, network_state, monkeypatch
):
    from stelvio.tunnel.plugin import PluginAssetError

    def plugin_failure(**kwargs):
        raise PluginAssetError("Private AWS plugin is missing")

    monkeypatch.setattr("stelvio.tunnel.session.install_helper", plugin_failure)
    session = world.make(network_state)
    try:
        with raises(PluginAssetError, match="plugin is missing"):
            session.start()
        assert world.launches == []
        assert world.messages == [
            "Network artifact unavailable: Private AWS plugin is missing",
            "Repair the pinned Traforo image or Session Manager plugin with stlv tunnel install.",
        ]
    finally:
        session.close()


def test_host_key_failure_blocks_dependencies_and_reports_recovery(world, network_state):
    detail = (
        "SSH host key changed for aws/123456789012/us-east-1/i-12345678: "
        "expected SHA256:old, presented SHA256:new."
    )
    session = world.make(network_state)
    session.start()
    try:
        wait_for(lambda: session.admit("both") is None)
        world.statuses[0].update(
            state="failed", cause="ssh-host-key", detail=detail, dns_retained=True
        )
        wait_for(lambda: isinstance(session.admit("both"), VpcUnavailableError))
        assert session.admit("healthy") is None
        assert session.admit("plain") is None
        wait_for(lambda: "  " + detail in world.messages)
        assert any("Host-key changes are not retried" in message for message in world.messages)
        assert any("stlv tunnel trust reset" in message for message in world.messages)
        with raises(RuntimeError, match="VPC access failed: vpc-0"):
            session.wait()
    finally:
        session.close()


@mark.parametrize("invalid", ["x" * 1025, "line\nbreak", "\x1b[31m", 123, ""])
def test_invalid_host_key_diagnostics_fail_closed(world, network_state, invalid, monkeypatch):
    session_id = UUID("a0dad4ae-81e1-4513-8afc-18ec1239d86e")
    monkeypatch.setattr("stelvio.tunnel.session.uuid4", lambda: session_id)
    session = world.make(network_state)
    session.start()
    try:
        wait_for(lambda: session.admit("both") is None)
        # Wait for the final ready report before marking the diagnostic boundary.
        wait_for(lambda: any(message.startswith("  vpc-1: ready ") for message in world.messages))
        assert f"Network STARTING; session {session_id}" in world.messages
        diagnostic_start = len(world.messages)
        world.statuses[0].update(state="failed", cause="ssh-host-key", detail=invalid)
        diagnostic = "Network DEGRADED: runtime status unavailable."
        wait_for(lambda: diagnostic in world.messages[diagnostic_start:])
        assert session._runtime_error == "ValueError"
        assert isinstance(session.admit("both"), VpcUnavailableError)
        assert session.admit("plain") is None
        # Reject the entire untrusted status, including an empty detail. A valid
        # startup identifier containing the same digits is not a diagnostic leak.
        assert world.messages[diagnostic_start:] == [diagnostic]
    finally:
        session.close()


@mark.parametrize("cause", ["host-uncertain-configure-20", "cleanup-incomplete"])
def test_route_conflict_reports_both_ranges_without_blocking_other_vpcs(
    world, network_state, cause
):
    detail = (
        "Native helper request failed: uncertain (configure stage 20): "
        "requested route 10.0.1.0/24 conflicts with route 10.0.1.128/25 on interface index 7"
    )
    session = world.make(network_state)
    session.start()
    try:
        wait_for(lambda: session.admit("both") is None)
        world.statuses[0].update(state="failed", cause=cause, detail=detail, dns_retained=True)
        wait_for(lambda: isinstance(session.admit("both"), VpcUnavailableError))
        assert session.admit("healthy") is None
        assert session.admit("plain") is None
        wait_for(lambda: "  " + detail in world.messages)
        assert any(
            "Do not delete" in message or "stlv tunnel cleanup" in message
            for message in world.messages
        )
    finally:
        session.close()


@mark.parametrize(
    ("cause", "state", "detail"),
    [
        ("host-uncertain-configure-20", "ready", "route facts"),
        ("host-uncertain-configure-0", "failed", "route facts"),
        ("identity-or-configuration", "failed", "route facts"),
        ("host-uncertain-configure-20", "failed", "line\nbreak"),
        ("host-uncertain-configure-20", "failed", "x" * 1025),
        ("host-uncertain-configure-20\x1b", "failed", None),
    ],
)
def test_invalid_route_diagnostics_fail_before_output_or_admission(
    world, network_state, cause, state, detail
):
    session = world.make(network_state)
    session.start()
    try:
        wait_for(lambda: session.admit("both") is None)
        world.statuses[0].update(state=state, cause=cause, detail=detail)
        wait_for(lambda: session._runtime_error is not None)
        assert isinstance(session.admit("both"), VpcUnavailableError)
        assert session.admit("plain") is None
        assert "  " + str(detail) not in world.messages
    finally:
        session.close()
