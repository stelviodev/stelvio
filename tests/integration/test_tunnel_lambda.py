"""Faster AWS CLI acceptance: private API Gateway → Lambda, without DocumentDB."""

import shutil
import signal
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from stelvio.tunnel.host_trust import fingerprint

from .cli_session import STARTUP_TIMEOUT
from .lambda_tunnel_session import LambdaTunnelSession, assert_echo, private_request

pytestmark = [pytest.mark.integration_tunnel, pytest.mark.integration_macos_privileged]


@pytest.fixture(scope="module")
def lambda_application(tmp_path_factory, macos_host):
    # Explicit dependency preserves the original helper before any deployment.
    session = LambdaTunnelSession(tmp_path_factory.mktemp("lambda-tunnel") / "app")
    try:
        session.deploy()
        yield session
    finally:
        session.destroy()
        macos_host.assert_restored("lambda-application-destroyed")


@pytest.fixture
def lambda_session(lambda_application, request):
    session = lambda_application
    started = time.monotonic()
    try:
        yield session
    finally:
        try:
            receipts = []
            try:
                if session.process is not None and session.process.poll() is None:
                    receipts = session.active_ssm_sessions()
            finally:
                try:
                    if session.process is not None and session.process.poll() is None:
                        session.terminate()
                finally:
                    session.wait_for_helper_cleanup(timeout=120)
            if receipts:
                session.assert_sessions_terminated(receipts)
        finally:
            if session.log_path.exists():
                shutil.copyfile(session.log_path, session.project / f"{request.node.name}-cli.log")
            session.timings[request.node.name + "_seconds"] = time.monotonic() - started
            session.write_evidence()


def assert_stopped(session, receipts):
    session.wait_for_helper_cleanup(timeout=120)
    session.assert_sessions_terminated(receipts)
    assert not session.resolver_ports()
    session.assert_bastions_running()
    session.assert_state_unchanged()
    session.assert_private_unavailable()


def assert_two_vpc_access(session):
    session.assert_selected_routes()
    ports = session.assert_dns_isolation()
    # Keep boto3 Session access on the owning thread. Only independent HTTP
    # clients run concurrently; give them different payloads as well as nonces.
    requests = []
    for number in (1, 2):
        session.wait_private(uuid4().hex, number)
        requests.append((number, uuid4().hex, f"vpc-{number}:" + str(number) * 16384))
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(private_request, session.apis[number].url, nonce, payload)
            for number, nonce, payload in requests
        ]
        responses = [future.result(timeout=30) for future in futures]
    for (number, nonce, payload), response in zip(requests, responses, strict=True):
        assert_echo(response, app=session.backend_identity(number), nonce=nonce, payload=payload)
    assert {response["backend"] for response in responses} == {
        session.backend_identity(1),
        session.backend_identity(2),
    }
    assert len(session.host_pins()) == 2
    return ports


def recover_transport(session, number=1):
    network = session.network(number)
    healthy = session.network(3 - number)
    initial = session.wait_unit(network.identity, "active")
    healthy_unit = session.wait_unit(healthy.identity, "active")
    ports = session.assert_dns_isolation()
    receipts = session.active_ssm_sessions()
    assert len(receipts) == 2
    retired = [item for item in receipts if item["Target"] == network.access.instance_id]
    peer = [item for item in receipts if item["Target"] == healthy.access.instance_id]
    assert len(retired) == len(peer) == 1
    offset = len(session.log())
    session.terminate_transport(network.access.instance_id)
    session.wait_network_ready(after=offset)
    recovered = session.wait_unit(
        network.identity,
        "active",
        after=initial.generation,
        timeout=120,
    )
    assert recovered.generation > initial.generation
    assert session.resolver_ports() == ports
    session.wait_private(uuid4().hex, number)
    session.check_private(uuid4().hex, 3 - number)
    assert session.wait_unit(healthy.identity, "active").generation == healthy_unit.generation
    assert [
        item["SessionId"]
        for item in session.active_ssm_sessions()
        if item["Target"] == healthy.access.instance_id
    ] == [item["SessionId"] for item in peer]
    session.assert_sessions_terminated(retired)


def partial_failure(session, failed, *, dev=False):
    healthy = 3 - failed
    network = session.network(failed)
    peer = session.network(healthy)
    initial = session.wait_unit(network.identity, "active")
    peer_unit = session.wait_unit(peer.identity, "active")
    ports = session.assert_dns_isolation()
    sessions_before = session.active_ssm_sessions()
    assert len(sessions_before) == 2
    retired = [item for item in sessions_before if item["Target"] == network.access.instance_id]
    peer_sessions = [item for item in sessions_before if item["Target"] == peer.access.instance_id]
    assert len(retired) == len(peer_sessions) == 1
    pins = session.host_pins()
    with session.stopped_bastion(failed):
        retained = session.wait_unit(
            network.identity, "retained", after=initial.generation, timeout=120
        )
        assert retained.generation == initial.generation + 1
        assert session.resolver_ports() == ports
        session.wait_dns_rcode(
            session.apis[failed].hostname, ports[session.apis[failed].hostname], expected=2
        )
        session.assert_private_unavailable(failed)
        for _ in range(2):
            # Both the host client and a fresh local invocation must keep working.
            session.check_private(uuid4().hex, healthy)
            if dev:
                session.wait_admission_rejected(failed)
                session.check_dev(uuid4().hex, healthy)
            assert session.wait_unit(peer.identity, "active").generation == peer_unit.generation
            assert session.resolver_ports() == ports
        assert [
            item["SessionId"]
            for item in session.active_ssm_sessions()
            if item["Target"] == peer.access.instance_id
        ] == [item["SessionId"] for item in peer_sessions]
        offset = len(session.log())
    session.wait_recovered_unit(
        network.identity, retained, after_log=offset, timeout=STARTUP_TIMEOUT
    )
    assert session.resolver_ports() == ports
    assert_two_vpc_access(session)
    if dev:
        session.check_dev(uuid4().hex, failed)
        session.check_dev(uuid4().hex, healthy)
    assert session.host_pins() == pins
    assert session.wait_unit(peer.identity, "active").generation == peer_unit.generation
    session.assert_sessions_terminated(retired)
    session.assert_bastions_running()
    session.assert_state_unchanged()


def test_lambda_standalone_https_and_sigint(lambda_session):
    session = lambda_session
    session.assert_private_unavailable()
    session.assert_state_unchanged()
    session.start()
    session.wait_ready()
    assert_two_vpc_access(session)
    receipts = session.active_ssm_sessions()
    assert len(receipts) == 2
    assert session.terminate(signal.SIGINT) == 0
    assert "Tunnel stopped." in session.log().splitlines()
    assert_stopped(session, receipts)


def test_lambda_transport_recovery_and_sigkill(lambda_session):
    session = lambda_session
    session.start()
    session.wait_ready()
    assert_two_vpc_access(session)
    pins = session.host_pins()
    recover_transport(session)
    assert session.host_pins() == pins
    receipts = session.active_ssm_sessions()
    assert len(receipts) == 2
    assert session.terminate(signal.SIGKILL) == -signal.SIGKILL
    assert_stopped(session, receipts)


def test_lambda_standalone_partial_failure_and_recovery(lambda_session):
    session = lambda_session
    session.start()
    session.wait_ready()
    assert_two_vpc_access(session)
    partial_failure(session, 1)
    receipts = session.active_ssm_sessions()
    assert len(receipts) == 2
    assert session.terminate(signal.SIGINT) == 0
    assert_stopped(session, receipts)


def test_lambda_dev_private_https_and_partial_failure(lambda_session):
    session = lambda_session
    session.start_dev()
    session.wait_ready()
    assert_two_vpc_access(session)
    for number in (1, 2):
        session.check_dev(uuid4().hex, number)
    recover_transport(session)
    # Reverse the failed/healthy roles: both VPCs must be independently managed.
    partial_failure(session, 2, dev=True)
    receipts = session.active_ssm_sessions()
    assert len(receipts) == 2
    # Dev uses Click Abort (exit 1) on Ctrl+C; tunnel start handles it as exit 0.
    assert session.terminate(signal.SIGINT) in (0, 1)
    assert_stopped(session, receipts)


def test_lambda_host_key_rejection_and_peer_isolation(lambda_session):
    session = lambda_session
    session.start_dev()
    session.wait_ready()
    ports = assert_two_vpc_access(session)
    session.check_dev(uuid4().hex, 1)
    network = session.network(1)
    pins = session.host_pins()
    with session.rotated_host_key(network.access.instance_id) as changed:
        session.terminate_transport(network.access.instance_id)
        session.wait_worker_cause(network.identity, "ssh-host-key", timeout=240)
        output = session.log()
        assert fingerprint(changed) in output
        assert any(fingerprint(key) in output for key in pins.values())
        assert "stlv tunnel trust reset" in output
        session.wait_dns_rcode(session.apis[1].hostname, ports[session.apis[1].hostname], 2)
        session.wait_admission_rejected(1)
        session.check_dev(uuid4().hex, 2)
        assert session.host_pins() == pins
        # A new runtime must still reject the changed key; never reset the pin.
        session.terminate(signal.SIGINT)
        session.wait_for_helper_cleanup(timeout=120)
        session.start_dev()
        session.wait_worker_cause(network.identity, "ssh-host-key", timeout=240)
        session.wait_admission_rejected(1)
        session.wait_unit(session.network(2).identity, "active", timeout=STARTUP_TIMEOUT)
        session.wait_private(uuid4().hex, 2)
        session.check_dev(uuid4().hex, 2)
        assert session.host_pins() == pins
    # Restoring the original key cannot revive a terminally failed worker.
    session.wait_worker_cause(network.identity, "ssh-host-key", timeout=30)
    session.terminate(signal.SIGINT)
    session.wait_for_helper_cleanup(timeout=120)
    session.start_dev()
    session.wait_ready()
    assert_two_vpc_access(session)
    for number in (1, 2):
        session.check_dev(uuid4().hex, number)
    assert session.host_pins() == pins
    receipts = session.active_ssm_sessions()
    assert len(receipts) == 2
    session.terminate(signal.SIGINT)
    assert_stopped(session, receipts)
