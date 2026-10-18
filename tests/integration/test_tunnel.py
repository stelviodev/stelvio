"""Exclusive actual macOS CLI → Function URL → local DocumentDB scenarios."""

import json
import platform
import signal
import socket
import subprocess
import time

from pytest import fixture, mark, raises

from stelvio.tunnel.helper_client import HelperError, NativeHelper
from stelvio.tunnel.installation import INSTALLED_HELPER, uninstall_helper

from .assert_tunnel import (
    assert_example_response,
    interrupt_transport,
    invoke_url,
    owned_transport,
    wait_new_transport,
)
from .cli_session import CliSession

pytestmark = [mark.integration_tunnel, mark.integration_macos_privileged]


@fixture
def cli_session(request, tmp_path):
    assert platform.system() == "Darwin"
    assert int(platform.mac_ver()[0].split(".")[0]) >= 15
    assert INSTALLED_HELPER.exists(), (
        "Install the approved helper before the exclusive tunnel lane"
    )
    baseline = NativeHelper().inspect()
    assert not baseline.owned
    assert not baseline.uncertain
    assert not baseline.units
    session = CliSession(request.param, project=tmp_path / "application")
    if request.param == "multi":
        session.make_multi_vpc()
    try:
        yield session
    finally:
        session.destroy()


@mark.parametrize("cli_session", ["True", "{}"], indirect=True)
def test_example_actual_cli_tls_discovery_reconnect_and_cleanup(cli_session):  # noqa: PLR0915 - complete session acceptance
    session = cli_session
    session.start()  # Actual stlv dev with no additional arguments.
    session.wait_ready()
    url = session.url()
    manifest = session.manifest()
    vpc = manifest.enabled_vpcs[0]
    before = assert_example_response(url, marker=session.marker, pid=session.process.pid)
    assert before["expected_host"].endswith(".docdb.amazonaws.com")
    instance = vpc.access.instance_id
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
    _exercise_host_key_rotation(session, vpc, url)
    session.stop()
    host = NativeHelper().inspect()
    assert not host.owned
    assert not host.uncertain
    assert not host.units
    # Normal stop retains the application and its explicitly deployed bastion.
    assert session.manifest().vpcs[0].vpc_id == vpc.vpc_id
    retained = session.sdk.client("ec2").describe_instances(InstanceIds=[instance])["Reservations"]
    assert retained[0]["Instances"][0]["State"]["Name"] == "running"
    before_state = session.state()
    session.start(tunnel=True)
    session.wait_ready()
    assert "local dev server connected to AppSync" not in session.log.read_text()
    _standalone_database(session)
    old = interrupt_transport(instance, session.transport_owner())
    wait_new_transport(instance, old, session.transport_owner())
    session.wait_ready()
    _standalone_database(session)
    # Signal the actual standalone command, rather than its whole process group.
    session.process.send_signal(signal.SIGINT)
    session.process.wait(timeout=60)
    assert session.process.returncode == 0
    assert "Tunnel stopped." in session.log.read_text().splitlines()
    session.recover_access()
    assert session.state() == before_state
    retained = session.sdk.client("ec2").describe_instances(InstanceIds=[instance])["Reservations"]
    assert retained[0]["Instances"][0]["State"]["Name"] == "running"
    saved = json.loads((session.project / "resolved-ownership.json").read_text())
    from .assert_tunnel import wait_owned_transports_absent

    for transport in saved["transports"]:
        wait_owned_transports_absent(
            transport["instance"], transport["owner_hash"], sdk=session.sdk
        )
    session.evidence("standalone-sigint-clean", {"bastion": instance, "state_unchanged": True})
    session.start(tunnel=True)
    session.wait_ready()
    _standalone_database(session)
    session.crash_and_recover()
    assert session.state() == before_state


@mark.parametrize("cli_session", ["multi"], indirect=True)
def test_multi_vpc_partial_failure_distinct_markers_and_crash_recovery(cli_session):
    session = cli_session
    # Establish existing private records before starting OS resolver ownership.
    ordinary = session.command("deploy", "--yes")
    assert ordinary
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
        uninstall_helper()
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
    persistent = next(v for v in manifest.enabled_vpcs if v.identity.endswith("::net2"))
    retained = session.sdk.client("ec2").describe_instances(
        InstanceIds=[persistent.access.instance_id]
    )["Reservations"]
    assert retained[0]["Instances"][0]["State"]["Name"] == "running"
    session.evidence("mixed-policy-stop", {"retained": persistent.access.instance_id})
    ordinary = session.command("deploy", "--yes")
    assert ordinary
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
    receipts = json.loads((session.project / "resolved-ownership.json").read_text())["transports"]
    assert {item["instance"] for item in receipts} == {
        vpc.access.instance_id for vpc in manifest.enabled_vpcs
    }
    assert len(receipts) == len(manifest.enabled_vpcs)
    assert len({item["session_id"] for item in receipts}) == len(receipts)
    session.evidence("startup-recovery-one-transport-per-bastion", {"transports": receipts})
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
    instance = vpc.access.instance_id
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


def _standalone_database(session):
    script = r"""
import json,os,sys,uuid
import boto3
from pymongo import MongoClient
sdk=boto3.Session()
cluster=sdk.client("docdb").describe_db_clusters(DBClusterIdentifier=sys.argv[1])["DBClusters"][0]
secret=sdk.client("secretsmanager").get_secret_value(SecretId=cluster["MasterUserSecret"]["SecretArn"])
password=json.loads(secret["SecretString"])["password"]
client=MongoClient(cluster["Endpoint"],cluster["Port"],username=cluster["MasterUsername"],password=password,
 tls=True,tlsCAFile=sys.argv[2],replicaSet="rs0",retryWrites=False,serverSelectionTimeoutMS=30000)
try:
 assert client.admin.command("ping")["ok"]==1
 marker=uuid.uuid4().hex
 collection=client.stelvio.standalone_proof
 collection.replace_one({"_id":"proof"},{"_id":"proof","marker":marker},upsert=True)
 assert collection.find_one({"_id":"proof"})["marker"]==marker
 assert client.nodes and client.topology_description.replica_set_name=="rs0"
 result={"read_write":True,"replica_set":"rs0"}
 result["members"]=sorted(host for host,port in client.nodes)
 print(json.dumps(result))
finally: client.close()
"""
    resource = session.manifest().resources[0]
    ca_file = session.project / ".stelvio/aws/documentdb/global-bundle.pem"
    result = subprocess.run(  # noqa: S603 - fixture wheel interpreter, fixed proof script
        [
            str(session.cli.with_name("python")),
            "-I",
            "-c",
            script,
            resource.resource_id,
            str(ca_file),
        ],
        cwd=session.project,
        env=session.environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    observed = json.loads(result.stdout)
    assert observed["read_write"] is True
    assert observed["replica_set"] == "rs0"
    assert observed["members"]
    session.evidence("standalone-database", observed)


def _remote_fixture(session, instance, command):
    """Run only against the instance recorded by this disposable application."""
    assert instance in {v.access.instance_id for v in session.manifest().enabled_vpcs}
    ssm = session.sdk.client("ssm")
    identity = ssm.send_command(
        InstanceIds=[instance],
        DocumentName="AWS-RunShellScript",
        Parameters={"commands": ["set -eu\n" + command]},
        TimeoutSeconds=90,
    )["Command"]["CommandId"]
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            result = ssm.get_command_invocation(CommandId=identity, InstanceId=instance)
        except ssm.exceptions.InvocationDoesNotExist:
            time.sleep(1)
            continue
        if result["Status"] == "Success":
            return
        assert result["Status"] in ("Pending", "InProgress", "Delayed"), result["Status"]
        time.sleep(1)
    raise TimeoutError("Proof-owned remote fault command exceeded its budget")


def _exercise_host_key_rotation(session, vpc, url):
    instance = vpc.access.instance_id
    backup = "/run/stelvio-test-key-" + session.run_id
    key = "/etc/ssh/ssh_host_ed25519_key"
    try:
        _remote_fixture(
            session,
            instance,
            f"mkdir -m 0700 {backup}\ncp -p {key} {backup}/original\n"
            f"cp -p {key}.pub {backup}/original.pub\n"
            f'ssh-keygen -q -t ed25519 -N "" -f {backup}/changed\n'
            f"install -m 0600 {backup}/changed {key}\n"
            f"install -m 0644 {backup}/changed.pub {key}.pub\nsystemctl restart sshd",
        )
        interrupt_transport(instance, session.transport_owner())
        deadline = time.monotonic() + 180
        while True:
            lines = session.log.read_text().splitlines()
            if any("identity-or-configuration" in line and "failed" in line for line in lines):
                break
            assert session.process.poll() is None
            if time.monotonic() >= deadline:
                raise AssertionError(
                    "Changed SSH identity did not cause terminal admission failure"
                )
            time.sleep(1)
        before = len(session.invocations())
        assert invoke_url(url)["status"] == 500
        assert len(session.invocations()) == before
        # Prove rejection reached Traforo rather than a prior positive OS cache.
        # Exact DocumentDB names are owned, but their OS result may remain cached;
        # direct UDP uses a fixed independent DNS wire query through the scoped port.
        from .macos_dns_query import query_scoped

        assert query_scoped(vpc, session.manifest()) == 2  # SERVFAIL
        session.evidence("host-key-change-refused", {"instance": instance})
    finally:
        _remote_fixture(
            session,
            instance,
            f"if [ -f {backup}/original ]; then cp -p {backup}/original {key}; "
            f"cp -p {backup}/original.pub {key}.pub; systemctl restart sshd; "
            f"rm -r {backup}; fi",
        )
    # Identity failure is terminal for that session. Restore the key and explicitly
    # start a new session; do not silently teach the old session to trust it.
    session.stop()
    session.start()
    session.wait_ready()
    assert_example_response(url, marker=session.marker, pid=session.process.pid)
    session.evidence("host-key-restored-new-session", {"instance": instance})


def test_non_vpc_actual_cli_without_helper(macos_host, tmp_path):
    from stelvio.tunnel import installation

    session = CliSession(project=tmp_path / "plain-application")
    source = f"""from stelvio.app import StelvioApp
from stelvio.config import StelvioAppConfig, AwsConfig
from stelvio.aws.function import Function
app=StelvioApp({session.app!r})
@app.config
def configuration(env):
    return StelvioAppConfig(aws={session.provider_source()})
@app.run
def run():
    Function("plain",handler="functions/plain.handler",url="public")
"""
    (session.project / "stlv_app.py").write_text(source)
    (session.project / "functions/plain.py").write_text(
        "import json,os\ndef handler(event,context):\n"
        '    return {"statusCode":200,"body":json.dumps({"plain":True,'
        '"marker":os.environ["STLV_TEST_TUNNEL_MARKER"],"pid":os.getpid()})}\n'
    )
    installation.uninstall_helper()
    try:
        session.start()
        deadline = time.monotonic() + 1200
        while "local dev server connected to AppSync" not in session.log.read_text():
            assert session.process.poll() is None
            assert time.monotonic() < deadline
            time.sleep(1)
        assert invoke_url(session.url()) == {
            "status": 200,
            "body": {"plain": True, "marker": session.marker, "pid": session.process.pid},
        }
        assert not INSTALLED_HELPER.exists()
        session.evidence("plain-without-helper", {"passed": True})
    finally:
        try:
            session.destroy()
        finally:
            installation.install_helper()
