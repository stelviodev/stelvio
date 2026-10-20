"""Opt-in standalone CLI → deployed DocumentDB acceptance."""

import hashlib
import signal
import time

import pytest

from stelvio.tunnel.helper_client import NativeHelper
from stelvio.tunnel.host_trust import fingerprint

from .cli_session import STARTUP_TIMEOUT, StandaloneCliSession

pytestmark = [pytest.mark.integration_tunnel, pytest.mark.integration_macos_privileged]


@pytest.fixture
def standalone_cli_session(tmp_path):
    session = StandaloneCliSession(tmp_path / "standalone-proof")
    try:
        session.deploy()
    except BaseException as deployment_error:
        try:
            session.destroy()
        except BaseException as cleanup_error:
            raise BaseExceptionGroup(
                "Proof deployment and cleanup failed", [deployment_error, cleanup_error]
            ) from None
        raise
    try:
        yield session
    finally:
        session.destroy()


def test_standalone_tls_reconnect_sigint_and_owner_death(standalone_cli_session):  # noqa: PLR0915 - full session acceptance lifecycle
    session = standalone_cli_session
    baseline = NativeHelper().status()
    assert not baseline.owned
    assert not baseline.uncertain
    assert not baseline.units
    session.assert_state_unchanged()

    session.start()
    session.wait_ready()
    session.assert_selected_routes()
    initial_pins = session.host_pins()
    assert len(initial_pins) == 2
    assert len(session.manifest.resources) == 2
    cluster_by_vpc = {
        resource.vpc: resource.resource_id for resource in session.manifest.resources
    }
    first_vpc, second_vpc = session.manifest.enabled_vpcs
    first_cluster = cluster_by_vpc[first_vpc.identity]
    second_cluster = cluster_by_vpc[second_vpc.identity]

    first_proof = session.check_database(first_cluster)
    second_proof = session.check_database(second_cluster)
    assert first_proof["tls"]
    assert first_proof["read_write"]
    assert len(first_proof["members"]) >= 2
    assert second_proof["tls"]
    assert second_proof["read_write"]

    helper = NativeHelper()
    first_unit_id = hashlib.sha256(first_vpc.identity.encode()).hexdigest()[:8]
    first_unit = next(item for item in helper.status().units if item.unit == first_unit_id)
    assert first_unit.status.name.lower() == "active"
    initial_generation = first_unit.generation
    ports_before = session.resolver_ports()
    assert ports_before

    ec2 = session.aws.client("ec2")
    bastion_id = first_vpc.access.instance_id
    ec2.stop_instances(InstanceIds=[bastion_id])
    ec2.get_waiter("instance_stopped").wait(
        InstanceIds=[bastion_id], WaiterConfig={"Delay": 5, "MaxAttempts": 60}
    )
    retained = session.wait_unit(first_vpc.identity, "retained", timeout=120)
    assert retained.generation == initial_generation + 1
    ports_during_outage = session.resolver_ports()
    assert all(ports_during_outage.get(domain) == port for domain, port in ports_before.items())

    # A failed VPC does not stop its healthy peer from serving normal database traffic.
    healthy = session.check_database(second_cluster)
    assert healthy["read_write"]

    ec2.start_instances(InstanceIds=[bastion_id])
    ec2.get_waiter("instance_running").wait(
        InstanceIds=[bastion_id], WaiterConfig={"Delay": 5, "MaxAttempts": 60}
    )
    recovered = session.wait_database(first_cluster)
    assert recovered["tls"]
    assert recovered["read_write"]
    active = session.wait_unit(
        first_vpc.identity, "active", after=retained.generation, timeout=STARTUP_TIMEOUT
    )
    assert active.generation == retained.generation + 1
    session.assert_selected_routes()
    ports_after_reconnect = session.resolver_ports()
    assert all(ports_after_reconnect.get(domain) == port for domain, port in ports_before.items())

    # Generation-stale carrier/control rejection is independently covered by the Go
    # interoperability suite; this live exercise proves the command recovers through it.
    graceful_receipts = session.active_ssm_sessions()
    assert len(graceful_receipts) == 2
    assert session.terminate(signal.SIGINT) == 0
    assert "Tunnel stopped." in session.log().splitlines()
    assert session.host_pins() == initial_pins
    session.wait_for_helper_cleanup()
    session.assert_sessions_terminated(graceful_receipts)
    assert not session.resolver_ports()
    session.assert_bastions_running()
    session.assert_state_unchanged()

    session.start()
    session.wait_ready()
    session.assert_selected_routes()
    assert session.host_pins() == initial_pins
    assert session.check_database(first_cluster)["read_write"]
    abrupt_receipts = session.active_ssm_sessions()
    assert len(abrupt_receipts) == 2
    assert session.terminate(signal.SIGKILL) == -signal.SIGKILL
    assert session.host_pins() == initial_pins
    session.wait_for_helper_cleanup(timeout=120)
    session.assert_sessions_terminated(abrupt_receipts)
    assert not session.resolver_ports()
    session.assert_bastions_running()
    session.assert_state_unchanged()

    # A new command must not trust a changed key for the same instance.
    with session.rotated_host_key(bastion_id) as changed_key:
        for _ in range(2):
            session.start()
            output = session.wait_host_key_refusal(bastion_id)
            assert fingerprint(changed_key) in output
            assert any(fingerprint(key) in output for key in initial_pins.values())
            session.wait_for_helper_cleanup()
            assert not session.active_ssm_sessions()
            assert session.host_pins() == initial_pins
            session.assert_state_unchanged()
    session.start()
    session.wait_ready()
    session.assert_selected_routes()
    assert session.host_pins() == initial_pins
    assert session.check_database(first_cluster)["read_write"]
    assert session.terminate(signal.SIGINT) == 0
    session.wait_for_helper_cleanup()
    session.assert_bastions_running()
    session.assert_state_unchanged()


def test_dev_function_url_gates_and_recovers_documentdb(standalone_cli_session):  # noqa: PLR0915 - complete dev networking acceptance
    session = standalone_cli_session
    session.start_dev()
    session.wait_ready()
    # Dev mode deploys its stub bridge. Compare state from that point through session exit.
    session._state_hash = hashlib.sha256(session.state()).hexdigest()
    first_vpc, _second_vpc = session.manifest.enabled_vpcs

    def assert_database(number: int) -> dict:
        status, response = session.invoke_function(number)
        assert status == 200, response
        assert isinstance(response, dict), response
        assert response["tls"] is True
        assert response["read_write"] is True
        assert response["members"]
        return response

    first = assert_database(1)
    second = assert_database(2)
    assert len(first["members"]) >= 2
    assert first["expected_host"] != second["expected_host"]
    first_unit = session.wait_unit(first_vpc.identity, "active", timeout=STARTUP_TIMEOUT)
    ports_before = session.resolver_ports()
    assert ports_before

    ec2 = session.aws.client("ec2")
    bastion_id = first_vpc.access.instance_id
    ec2.stop_instances(InstanceIds=[bastion_id])
    ec2.get_waiter("instance_stopped").wait(
        InstanceIds=[bastion_id], WaiterConfig={"Delay": 5, "MaxAttempts": 60}
    )
    retained = session.wait_unit(first_vpc.identity, "retained", timeout=120)
    assert retained.generation == first_unit.generation + 1
    ports_during_outage = session.resolver_ports()
    assert all(ports_during_outage.get(domain) == port for domain, port in ports_before.items())

    # Wait until the dependent endpoint is rejected before handler import. The other
    # VPC endpoint remains able to perform a real DocumentDB TLS read/write.
    deadline = time.monotonic() + 120
    while True:
        imported_before = len(session.invocations())
        status, _response = session.invoke_function(1, timeout=20)
        imported_after = len(session.invocations())
        if status == 500 and imported_after == imported_before:
            break
        if time.monotonic() >= deadline:
            raise AssertionError("Unavailable VPC endpoint did not close handler admission")
        time.sleep(1)
    healthy = assert_database(2)
    assert healthy["expected_host"] == second["expected_host"]

    ec2.start_instances(InstanceIds=[bastion_id])
    ec2.get_waiter("instance_running").wait(
        InstanceIds=[bastion_id], WaiterConfig={"Delay": 5, "MaxAttempts": 60}
    )
    active = session.wait_unit(
        first_vpc.identity,
        "active",
        after=retained.generation,
        timeout=STARTUP_TIMEOUT,
    )
    assert active.generation == retained.generation + 1
    recovered = assert_database(1)
    assert recovered["expected_host"] == first["expected_host"]
    ports_after_reconnect = session.resolver_ports()
    assert all(ports_after_reconnect.get(domain) == port for domain, port in ports_before.items())

    receipts = session.active_ssm_sessions()
    assert len(receipts) == 2
    session.terminate(signal.SIGINT)
    session.wait_for_helper_cleanup()
    session.assert_sessions_terminated(receipts)
    assert not session.resolver_ports()
    session.assert_bastions_running()
    session.assert_state_unchanged()
