"""Faster AWS CLI acceptance: private API Gateway → Lambda, without DocumentDB."""

import shutil
import signal
import time
from uuid import uuid4

import pytest

from .lambda_tunnel_session import LambdaTunnelSession

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


def recover_transport(session):
    network = session.manifest.enabled_vpcs[0]
    initial = session.wait_unit(network.identity, "active")
    ports = session.resolver_ports()
    assert set(ports) == {session.api.hostname}
    receipts = session.active_ssm_sessions()
    assert len(receipts) == 1
    offset = len(session.log())
    session.terminate_transport(network.access.instance_id)
    session.wait_network_ready(after=offset)
    recovered = session.wait_unit(
        network.identity, "active", after=initial.generation, timeout=120
    )
    assert recovered.generation > initial.generation
    assert session.resolver_ports() == ports
    session.wait_private(uuid4().hex)
    session.assert_sessions_terminated(receipts)
    assert session.host_pins()


def test_lambda_standalone_https_and_sigint(lambda_session):
    session = lambda_session
    session.assert_private_unavailable()
    session.assert_state_unchanged()
    session.start()
    session.wait_ready()
    session.assert_selected_routes()
    session.wait_private(uuid4().hex)
    receipts = session.active_ssm_sessions()
    assert len(receipts) == 1
    assert session.terminate(signal.SIGINT) == 0
    assert "Tunnel stopped." in session.log().splitlines()
    assert_stopped(session, receipts)


def test_lambda_transport_recovery_and_sigkill(lambda_session):
    session = lambda_session
    session.start()
    session.wait_ready()
    session.wait_private(uuid4().hex)
    pins = session.host_pins()
    recover_transport(session)
    assert session.host_pins() == pins
    receipts = session.active_ssm_sessions()
    assert len(receipts) == 1
    assert session.terminate(signal.SIGKILL) == -signal.SIGKILL
    assert_stopped(session, receipts)


def test_lambda_dev_private_https_and_recovery(lambda_session):
    session = lambda_session
    session.start_dev()
    session.wait_ready()
    session.assert_selected_routes()
    session.wait_private(uuid4().hex)
    session.check_dev(uuid4().hex)
    recover_transport(session)
    session.check_dev(uuid4().hex)
    receipts = session.active_ssm_sessions()
    assert len(receipts) == 1
    # Dev uses Click Abort (exit 1) on Ctrl+C; tunnel start handles it as exit 0.
    assert session.terminate(signal.SIGINT) in (0, 1)
    assert_stopped(session, receipts)
