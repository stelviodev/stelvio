"""Exclusive actual macOS CLI → Function URL → local DocumentDB scenarios."""

import platform
import socket
import time

from pytest import fixture, mark, raises

from stelvio.tunnel.helper_client import HelperError, NativeHelper
from stelvio.tunnel.installation import INSTALLED_HELPER, cleanup_helper

from .assert_tunnel import (
    assert_example_response,
    interrupt_transport,
    invoke_url,
    owned_transport,
    wait_new_transport,
)
from .cli_session import CliSession

pytestmark = [mark.integration_tunnel, mark.integration_vpc]


@fixture
def cli_session(request):
    assert platform.system() == "Darwin"
    assert platform.mac_ver()[0] == "15.7.5"
    assert INSTALLED_HELPER.exists(), (
        "Install the approved helper before the exclusive tunnel lane"
    )
    baseline = NativeHelper().inspect()
    assert not baseline.owned
    assert not baseline.uncertain
    assert not baseline.units
    session = CliSession(request.param)
    if request.param == "multi":
        session.make_multi_vpc()
    try:
        yield session
    finally:
        session.destroy()


@mark.parametrize("cli_session", ["True", "omitted"], indirect=True)
def test_example_actual_cli_tls_discovery_reconnect_and_cleanup(cli_session):
    session = cli_session
    session.start()  # Actual stlv dev with no additional arguments.
    session.wait_ready()
    url = session.url()
    manifest = session.manifest()
    vpc = manifest.enabled_vpcs[0]
    before = assert_example_response(url, marker=session.marker, pid=session.process.pid)
    assert before["expected_host"].endswith(".docdb.amazonaws.com")
    instance = vpc.access.instance_id if vpc.access else _temporary_instance(session, vpc.vpc_id)
    original = owned_transport(instance, session.transport_owner())["SessionId"]
    handler = session.project / "functions/ping.py"
    handler.write_text(handler.read_text() + "\n# Acceptance: reload the handler.\n")
    assert_example_response(url, marker=session.marker, pid=session.process.pid)
    assert owned_transport(instance, session.transport_owner())["SessionId"] == original
    # Idle is an explicit acceptance condition, not a deployment/readiness delay.
    time.sleep(30)
    assert_example_response(url, marker=session.marker, pid=session.process.pid)
    assert owned_transport(instance, session.transport_owner())["SessionId"] == original
    session.evidence("reload-idle", {"session": original, "idle_seconds": 30})
    old = interrupt_transport(instance, session.transport_owner())
    wait_new_transport(instance, old, session.transport_owner())
    deadline = time.monotonic() + 180
    while True:
        try:
            after = assert_example_response(url, marker=session.marker, pid=session.process.pid)
            break
        except AssertionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(1)
    assert after["expected_host"] == before["expected_host"]
    invocations = session.invocations()
    assert len({i["id"] for i in invocations}) == len(invocations)
    session.evidence("example-reconnected", after)
    session.stop()
    host = NativeHelper().inspect()
    assert not host.owned
    assert not host.uncertain
    assert not host.units
    # Normal stop retains the application; temporary access is a separate owner.
    assert session.manifest().vpcs[0].vpc_id == vpc.vpc_id
    if vpc.access is None:
        _assert_no_temporary_instances(session, vpc.vpc_id)
        session.command("deploy", "--yes", "--json")
        _assert_no_temporary_instances(session, vpc.vpc_id)
        session.evidence("ordinary-deploy-no-temporary-access", {"vpc": vpc.vpc_id})


def _assert_no_temporary_instances(session, vpc_id):
    reservations = session.sdk.client("ec2").describe_instances(
        Filters=[
            {"Name": "vpc-id", "Values": [vpc_id]},
            {"Name": "tag-key", "Values": ["stlv:tunnel-session"]},
        ]
    )["Reservations"]
    assert all(i["State"]["Name"] == "terminated" for r in reservations for i in r["Instances"])


def _temporary_instance(session, vpc_id):
    ec2 = session.sdk.client("ec2")
    reservations = ec2.describe_instances(
        Filters=[
            {"Name": "vpc-id", "Values": [vpc_id]},
            {"Name": "tag-key", "Values": ["stlv:tunnel-session"]},
            {"Name": "instance-state-name", "Values": ["running"]},
        ]
    )["Reservations"]
    instances = [i["InstanceId"] for r in reservations for i in r["Instances"]]
    assert len(instances) == 1
    return instances[0]


@mark.parametrize("cli_session", ["multi"], indirect=True)
def test_multi_vpc_partial_failure_distinct_markers_and_crash_recovery(cli_session):
    session = cli_session
    # Establish existing private records before starting OS resolver ownership.
    ordinary = session.command("deploy", "--yes")
    assert "bastion=False disables managed access" in ordinary
    deployed = session.manifest()
    zone_vpc = next(v for v in deployed.enabled_vpcs if v.identity.endswith("::net2"))
    resource = next(r for r in deployed.resources if r.vpc == zone_vpc.identity)
    alias = session.private_zone(zone_vpc.vpc_id, resource.hostnames[0])
    session.start()
    session.wait_ready()
    urls = session.urls()
    baseline = {}
    for number in (1, 2):
        payload = assert_example_response(
            urls[f"api{number}"], marker=session.marker, pid=session.process.pid
        )
        assert payload["data_marker"] == f"{session.run_id}-database-{number}"
        baseline[number] = payload
        session.evidence(f"database-{number}", payload)
    assert baseline[1]["expected_host"] != baseline[2]["expected_host"]
    with raises(HelperError, match="active"):
        cleanup_helper()
    for number in (1, 2):
        assert_example_response(
            urls[f"api{number}"], marker=session.marker, pid=session.process.pid
        )
    session.evidence("active-dev-uninstall-refused", {"both_databases_healthy": True})
    plain = invoke_url(urls["plain"])
    assert plain == {
        "status": 200,
        "body": {
            "plain": True,
            "local_marker": session.marker,
            "pid": session.process.pid,
        },
    }
    assert invoke_url(urls["opted"]) == plain
    manifest = session.manifest()
    opted = next(v for v in manifest.used_vpcs if v.identity.endswith("::opted-net"))
    assert opted not in manifest.enabled_vpcs
    assert opted.access is None
    assert "VPC access disabled:" in session.log.read_text()
    assert "local networking is your responsibility" in session.log.read_text()
    assert "bastion=False disables managed access" in session.log.read_text()
    assert not session.sdk.client("ec2").describe_instances(
        Filters=[{"Name": "vpc-id", "Values": [opted.vpc_id]}]
    )["Reservations"]
    session.evidence("opted-out-with-healthy-paths", {"vpc": opted.vpc_id, "response": plain})
    _assert_private_alias(session, alias, baseline[2]["expected_host"])
    for number in (1, 2):
        _exercise_outage(session, urls, manifest, baseline, number)
    invocations = session.invocations()
    assert len({i["id"] for i in invocations}) == len(invocations)
    session.crash_and_recover()
    assert session.manifest().vpcs
    _exercise_startup_outage(session, urls, manifest, baseline)
    session.stop()
    temporary = next(v for v in manifest.enabled_vpcs if v.identity.endswith("::net1"))
    _assert_no_temporary_instances(session, temporary.vpc_id)
    persistent = next(v for v in manifest.enabled_vpcs if v.identity.endswith("::net2"))
    retained = session.sdk.client("ec2").describe_instances(
        InstanceIds=[persistent.access.instance_id]
    )["Reservations"]
    assert retained[0]["Instances"][0]["State"]["Name"] == "running"
    session.evidence("mixed-policy-stop", {"retained": persistent.access.instance_id})
    ordinary = session.command("deploy", "--yes")
    assert "bastion=False disables managed access" in ordinary
    assert not session.sdk.client("ec2").describe_instances(
        Filters=[{"Name": "vpc-id", "Values": [opted.vpc_id]}]
    )["Reservations"]
    session.evidence("ordinary-deploy-explicit-opt-out", {"vpc": opted.vpc_id})


def _assert_plain(session, url, phase):
    response = invoke_url(url)
    session.evidence(phase, response)
    assert response == {
        "status": 200,
        "body": {"plain": True, "local_marker": session.marker, "pid": session.process.pid},
    }


def _exercise_startup_outage(session, urls, manifest, baseline):
    """Start the real CLI with one owned persistent bastion held unavailable."""
    persistent = next(v for v in manifest.enabled_vpcs if v.identity.endswith("::net2"))
    instance = persistent.access.instance_id
    ec2 = session.sdk.client("ec2")
    ec2.stop_instances(InstanceIds=[instance])
    try:
        ec2.get_waiter("instance_stopped").wait(
            InstanceIds=[instance], WaiterConfig={"Delay": 5, "MaxAttempts": 60}
        )
        session.start()
        deadline = time.monotonic() + 1200
        while True:
            output = session.log.read_text()
            assert session.process.poll() is None
            if "local dev server connected to AppSync" in output and any(
                "::net1: ready " in line for line in output.splitlines()
            ):
                break
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Healthy startup path unavailable; retain {session.project}")
            time.sleep(1)
        offset = len(session.invocations())
        started = time.monotonic()
        rejected = invoke_url(urls["api2"])
        assert rejected["status"] == 500
        assert time.monotonic() - started < 15
        assert len(session.invocations()) == offset
        healthy = assert_example_response(
            urls["api1"], marker=session.marker, pid=session.process.pid
        )
        assert healthy["expected_host"] == baseline[1]["expected_host"]
        for name in ("plain", "opted"):
            _assert_plain(session, urls[name], f"startup-{name}-response")
        session.evidence("partial-startup", {"rejected": rejected, "healthy": healthy})
    finally:
        ec2.start_instances(InstanceIds=[instance])
    session.wait_ready()
    restored = assert_example_response(
        urls["api2"], marker=session.marker, pid=session.process.pid
    )
    assert restored["expected_host"] == baseline[2]["expected_host"]
    session.evidence("startup-recovered", restored)


def _assert_private_alias(session, alias, hostname):
    deadline = time.monotonic() + 120
    while True:
        try:
            actual = {i[4][0] for i in socket.getaddrinfo(alias, 27017)}
            expected = {i[4][0] for i in socket.getaddrinfo(hostname, 27017)}
            assert actual == expected
            break
        except (AssertionError, socket.gaierror) as error:
            session.evidence(
                "private-os-dns-attempt", {"name": alias, "error": type(error).__name__}
            )
            if time.monotonic() >= deadline:
                raise
            time.sleep(1)
    session.evidence("private-os-dns", {"name": alias, "addresses": sorted(actual)})


def _exercise_outage(session, urls, manifest, baseline, number):
    vpc = next(v for v in manifest.enabled_vpcs if v.identity.endswith(f"::net{number}"))
    instance = vpc.access.instance_id if vpc.access else _temporary_instance(session, vpc.vpc_id)
    old = owned_transport(instance, session.transport_owner())["SessionId"]
    ec2 = session.sdk.client("ec2")
    # Stop only this app's recorded bastion to hold a real outage long enough
    # to test admission and private DNS before allowing a new connection.
    ec2.stop_instances(InstanceIds=[instance])
    try:
        ec2.get_waiter("instance_stopped").wait(
            InstanceIds=[instance], WaiterConfig={"Delay": 5, "MaxAttempts": 60}
        )
        deadline = time.monotonic() + 90
        while True:
            offset = len(session.invocations())
            started = time.monotonic()
            rejected = invoke_url(urls[f"api{number}"])
            if rejected["status"] == 500 and len(session.invocations()) == offset:
                assert time.monotonic() - started < 15
                session.evidence(f"closed-admission-{number}", {"response": rejected})
                break
            if time.monotonic() >= deadline:
                raise AssertionError("Outage did not close admission before handler execution")
            time.sleep(1)
        if number == 2:
            # A fresh label bypasses prior positive resolver caches. It exists
            # only in our private zone and must not leak to ordinary fallback.
            fresh = session.private_alias("outage", baseline[2]["expected_host"])
            try:
                socket.getaddrinfo(fresh, 27017)
            except socket.gaierror:
                session.evidence("private-dns-outage-rejected", {"name": fresh})
            else:
                raise AssertionError("Unavailable private VPC DNS returned an address")
        healthy = 3 - number
        assert_example_response(
            urls[f"api{healthy}"], marker=session.marker, pid=session.process.pid
        )
        _assert_plain(session, urls["plain"], f"outage-{number}-plain-response")
        assert socket.getaddrinfo("aws.amazon.com", 443)
    finally:
        ec2.start_instances(InstanceIds=[instance])
    # A reboot includes the transport's 300s SSM-online and 120s bootstrap
    # budgets; live SSM interruption uses the shorter default elsewhere.
    wait_new_transport(instance, old, session.transport_owner(), timeout=600)
    deadline = time.monotonic() + 180
    while True:
        try:
            restored = assert_example_response(
                urls[f"api{number}"], marker=session.marker, pid=session.process.pid
            )
            break
        except AssertionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(1)
    assert restored["expected_host"] == baseline[number]["expected_host"]
    session.evidence(f"reconnected-{number}", restored)
