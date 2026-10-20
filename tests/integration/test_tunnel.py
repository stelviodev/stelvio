"""Opt-in standalone CLI → deployed DocumentDB acceptance."""

import hashlib
import signal
import time

import pytest

from stelvio.tunnel.helper_client import NativeHelper
from stelvio.tunnel.host_trust import fingerprint
from stelvio.tunnel.installation import INSTALLED_HELPER

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

    first_unit = session.wait_unit(first_vpc.identity, "active", timeout=STARTUP_TIMEOUT)
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
    first_vpc = next(vpc for vpc in session.manifest.enabled_vpcs if vpc.identity.endswith("net1"))
    second_vpc = next(
        vpc for vpc in session.manifest.enabled_vpcs if vpc.identity.endswith("net2")
    )
    cluster_by_vpc = {
        resource.vpc: resource.resource_id for resource in session.manifest.resources
    }
    first_cluster = cluster_by_vpc[first_vpc.identity]
    second_cluster = cluster_by_vpc[second_vpc.identity]

    def assert_database(number: int, marker: str) -> dict:
        status, response = session.invoke_function(number, marker=marker)
        assert status == 200, response
        assert isinstance(response, dict), response
        assert response["tls"] is True
        assert response["read_write"] is True
        assert response["members"]
        assert response["marker"] == marker
        return response

    marker1 = f"{session.app}-only-vpc1"
    marker2 = f"{session.app}-only-vpc2"
    first = assert_database(1, marker1)
    second = assert_database(2, marker2)
    assert len(first["members"]) >= 2
    assert first["expected_host"] != second["expected_host"]
    assert session.check_database(first_cluster, marker=marker1)["marker_present"]
    assert not session.check_database(first_cluster, marker=marker2, marker_expected=False)[
        "marker_present"
    ]
    assert session.check_database(second_cluster, marker=marker2)["marker_present"]
    assert not session.check_database(second_cluster, marker=marker1, marker_expected=False)[
        "marker_present"
    ]

    # A disabled-bastion endpoint and an ordinary non-VPC endpoint keep normal
    # bridge dispatch without creating network dependencies.
    for number in (3, 4):
        status, response = session.invoke_function(number)
        assert status == 200, response
        assert isinstance(response, dict)
        assert response["plain"] is True
        assert response["marker"] == session.app
    disabled_vpc = next(vpc for vpc in session.manifest.used_vpcs if vpc.identity.endswith("net3"))
    assert "VPC access disabled:" in session.log()
    assert disabled_vpc.identity in session.log()

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
        status, response = session.invoke_function(1, timeout=20, marker=marker1)
        imported_after = len(session.invocations())
        if (
            status == 500
            and isinstance(response, dict)
            and response.get("errorType") == "VpcUnavailableError"
            and "VPC access unavailable for this invocation" in response.get("error", "")
            and first_vpc.identity in response.get("error", "")
            and imported_after == imported_before
        ):
            break
        if time.monotonic() >= deadline:
            raise AssertionError(
                "Unavailable VPC endpoint did not return VpcUnavailableError before handler import"
            )
        time.sleep(1)
    healthy = assert_database(2, marker2)
    assert healthy["expected_host"] == second["expected_host"]
    for number in (3, 4):
        status, response = session.invoke_function(number)
        assert status == 200, response
        assert isinstance(response, dict)
        assert response["plain"] is True

    readiness_offset = len(session.log())
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
    session.wait_network_ready(after=readiness_offset, timeout=STARTUP_TIMEOUT)
    recovered = session.wait_function_marker(1, marker1)
    assert recovered["tls"]
    assert recovered["read_write"]
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

    # A hard CLI death exercises the parent/lease cleanup path. Restarting a
    # second dev process must acquire fresh ownership and serve the same app.
    session.start_dev()
    session.wait_ready()
    assert session.wait_function_marker(2, marker2)["read_write"]
    abrupt_receipts = session.active_ssm_sessions()
    assert len(abrupt_receipts) == 2
    assert session.terminate(signal.SIGKILL) == -signal.SIGKILL
    session.wait_for_helper_cleanup(timeout=120)
    session.assert_sessions_terminated(abrupt_receipts)
    assert not session.resolver_ports()
    session.assert_bastions_running()
    session.assert_state_unchanged()

    session.start_dev()
    session.wait_ready()
    assert session.wait_function_marker(1, marker1)["read_write"]
    session.terminate(signal.SIGINT)
    session.wait_for_helper_cleanup()
    session.assert_bastions_running()
    session.assert_state_unchanged()


def test_dev_host_key_rotation_is_terminal_until_a_new_session(standalone_cli_session):  # noqa: PLR0915 - complete key-change and restart acceptance
    session = standalone_cli_session
    session.start_dev()
    session.wait_ready()
    session._state_hash = hashlib.sha256(session.state()).hexdigest()
    vpc = next(vpc for vpc in session.manifest.enabled_vpcs if vpc.identity.endswith("net1"))
    marker = f"{session.app}-host-key-proof"
    assert session.wait_function_marker(1, marker)["read_write"]
    resource = next(item for item in session.manifest.resources if item.vpc == vpc.identity)
    hostname = resource.hostnames[0]
    port = session.resolver_ports()[hostname]
    instance_id = vpc.access.instance_id
    initial_pins = session.host_pins()

    with session.rotated_host_key(instance_id) as changed_key:
        session.terminate_transport(instance_id)
        session.wait_worker_cause(vpc.identity, "ssh-host-key", timeout=240)
        output = session.log()
        assert instance_id in output
        assert fingerprint(changed_key) in output
        assert any(fingerprint(key) in output for key in initial_pins.values())
        assert "stlv tunnel trust reset" in output

        def assert_refused():
            invocation_count = len(session.invocations())
            status, response = session.invoke_function(1, timeout=20, marker=marker)
            assert status == 500, response
            assert isinstance(response, dict), response
            assert response.get("errorType") == "VpcUnavailableError"
            assert vpc.identity in response.get("error", "")
            assert len(session.invocations()) == invocation_count
            assert session.host_pins() == initial_pins
            # Only this VPC is blocked; the peer and unrelated endpoints still run.
            assert session.wait_function_marker(2, f"{session.app}-peer-healthy")["read_write"]
            for number in (3, 4):
                plain_status, plain = session.invoke_function(number)
                assert plain_status == 200
                assert isinstance(plain, dict)
                assert plain["plain"]

        assert_refused()
        session.wait_dns_rcode(hostname, port, expected=2)

        # Neither a fresh dev command nor a fresh runtime can replace the pin.
        session.terminate(signal.SIGINT)
        session.wait_for_helper_cleanup(timeout=120)
        session.start_dev()
        session.wait_worker_cause(vpc.identity, "ssh-host-key", timeout=240)
        assert_refused()

    # Pin mismatch is terminal for the old session. Restoring the proof bastion
    # key does not change that decision; only a fresh session may reconnect.
    session.wait_worker_cause(vpc.identity, "ssh-host-key", timeout=30)
    session.terminate(signal.SIGINT)
    session.wait_for_helper_cleanup(timeout=120)
    session.start_dev()
    session.wait_ready()
    assert session.host_pins() == initial_pins
    recovered = session.wait_function_marker(1, marker)
    assert recovered["tls"]
    assert recovered["read_write"]
    assert recovered["expected_host"] == hostname
    session.terminate(signal.SIGINT)
    session.wait_for_helper_cleanup(timeout=120)
    session.assert_state_unchanged()


def test_non_vpc_actual_dev_runs_without_installed_helper(macos_host, tmp_path):
    from stelvio.tunnel import installation

    session = StandaloneCliSession(
        tmp_path / "plain-without-helper", require_helper=False, no_vpc=True
    )
    try:
        session.deploy()
        assert INSTALLED_HELPER.is_file()
        installation.uninstall_helper()
        assert not INSTALLED_HELPER.exists()
        session.start_dev()
        session.wait_ready()
        status, response = session.invoke_function(1)
        assert status == 200, response
        assert isinstance(response, dict)
        assert response["plain"] is True
        assert response["marker"] == session.app
        assert not INSTALLED_HELPER.exists()
    finally:
        if session.process is not None and session.process.poll() is None:
            session.terminate(signal.SIGINT)
        # The helper remains absent here; macos_host restores its captured image
        # after the proof deployment has been destroyed.
        session.destroy()
