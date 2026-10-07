"""Transport validates deployed ownership and content before local SSH execution."""

import base64
import hashlib
import os
import select
import subprocess
import sys
from dataclasses import replace
from ipaddress import IPv4Network
from threading import Event, Thread
from unittest.mock import Mock

from botocore.exceptions import ClientError
from pytest import fixture, mark, raises

from stelvio.tunnel.bastion import identity_document
from stelvio.tunnel.manifest import AccessDescriptor, VpcNetwork
from stelvio.tunnel.policy import BastionPolicy
from stelvio.tunnel.processes import identity
from stelvio.tunnel.socks import TransportInterruptedError
from stelvio.tunnel.transport import (
    HOST_KEY_PREFIX,
    ServerIdentityError,
    SshTransport,
    TransportAuthorizationError,
    TransportRetryError,
    terminate_owned_session,
)


@fixture
def transport_world():
    owner = "11111111-1111-4111-8111-111111111111"
    network = VpcNetwork(
        "vpc-owner",
        "123456789012",
        "us-east-1",
        "vpc-12345678",
        "provider",
        (IPv4Network("10.254.0.0/16"),),
        BastionPolicy.TEMPORARY,
        (),
        (),
        AccessDescriptor("i-12345678", "sg-12345678", "us-east-1a", "owned-identity", owner),
    )
    session = Mock(region_name="us-east-1")
    clients = {service: Mock() for service in ("ec2", "ssm", "sts")}
    session.client.side_effect = lambda name, **kwargs: clients[name]
    clients["sts"].get_caller_identity.return_value = {"Account": "123456789012"}
    tags = [{"Key": "stlv:tunnel-session", "Value": owner}]
    node = {
        "VpcId": "vpc-12345678",
        "Placement": {"AvailabilityZone": "us-east-1a"},
        "SecurityGroups": [{"GroupId": "sg-12345678"}],
        "Tags": tags,
        "State": {"Name": "running"},
    }
    group = {"VpcId": "vpc-12345678", "OwnerId": "123456789012", "IpPermissions": []}
    clients["ec2"].describe_instances.return_value = {"Reservations": [{"Instances": [node]}]}
    clients["ec2"].describe_security_groups.return_value = {"SecurityGroups": [group]}
    document = {"DocumentVersion": "3", "Content": identity_document()}
    description = {
        "DocumentVersion": "3",
        "HashType": "Sha256",
        "Hash": hashlib.sha256(document["Content"].encode()).hexdigest(),
    }
    clients["ssm"].get_document.return_value = document
    clients["ssm"].describe_document.return_value = {"Document": description}
    clients["ssm"].list_tags_for_resource.return_value = {"TagList": tags}
    clients["ssm"].describe_instance_information.return_value = {
        "InstanceInformationList": [{"PingStatus": "Online"}]
    }
    clients["ssm"].send_command.return_value = {"Command": {"CommandId": "command-owned"}}
    encoded = base64.b64encode(HOST_KEY_PREFIX + bytes(range(32))).decode()
    clients["ssm"].get_command_invocation.return_value = {
        "Status": "Success",
        "StandardOutputContent": f"ssh-ed25519 {encoded}\nstelvio-tunnel-ready\n",
    }
    transport = SshTransport(network, session, Event())
    transport.pin = "another previously authenticated server key"
    return transport, clients, node, group, document, description


def test_verified_document_version_and_content_hash_are_bound_to_execution(transport_world):
    transport, clients, _, _, _, description = transport_world
    with raises(ServerIdentityError, match="server identity changed"):
        transport.start()
    clients["ssm"].send_command.assert_called_once_with(
        InstanceIds=["i-12345678"],
        DocumentName="owned-identity",
        DocumentVersion="3",
        DocumentHash=description["Hash"],
        DocumentHashType="Sha256",
        TimeoutSeconds=90,
    )
    assert transport.process is None
    assert transport.session_id is None
    transport.close()


def test_actual_child_birth_registration_finishes_before_shutdown_fence(  # noqa: PLR0915 - full concurrent child lifecycle
    transport_world, monkeypatch
):
    transport, clients, _, _, _, _ = transport_world
    transport.pin = None
    monkeypatch.setattr(transport, "_bootstrap", lambda: "fixture authenticated pin")
    monkeypatch.setattr("stelvio.tunnel.transport.shutil.which", lambda _: "/usr/bin/true")
    clients["ssm"].start_session.return_value = {"SessionId": "owned-fixture"}
    clients["ssm"].meta.endpoint_url = "https://ssm.us-east-1.amazonaws.com"
    eic = Mock()
    eic.send_ssh_public_key.return_value = {"Success": True}
    original_client = transport.session.client.side_effect
    transport.session.client.side_effect = lambda name, **kwargs: (
        eic if name == "ec2-instance-connect" else original_client(name, **kwargs)
    )
    original_popen = subprocess.Popen
    spawned, release, fenced = Event(), Event(), Event()
    children, failures = [], []

    def launch(arguments, **options):
        if arguments[0] != "/usr/bin/ssh":
            return original_popen(arguments, **options)
        child = original_popen(
            [sys.executable, "-I", "-S", "-c", "import time;time.sleep(60)"],
            start_new_session=True,
        )
        children.append(child)
        spawned.set()
        assert release.wait(5)
        return child

    def start():
        try:
            transport.start()
        except Exception as error:
            failures.append(error)

    def fence():
        transport.stop_local()
        fenced.set()

    monkeypatch.setattr("stelvio.tunnel.transport.subprocess.Popen", launch)
    attempt = Thread(target=start)
    stopping = Thread(target=fence)
    attempt.start()
    try:
        assert spawned.wait(5)
        transport.stop.set()
        stopping.start()
        assert not fenced.wait(0.05)
        release.set()
        attempt.join(5)
        stopping.join(5)
        assert not attempt.is_alive()
        assert not stopping.is_alive()
        assert fenced.is_set()
        assert len(failures) == 1
        assert isinstance(failures[0], TransportInterruptedError)
        assert len(children) == 1
        assert children[0].returncode is not None
        assert identity(children[0].pid) is None
        transport.close()
        assert clients["ssm"].terminate_session.call_args.kwargs == {"SessionId": "owned-fixture"}
        with raises(TransportInterruptedError):
            transport.start()
        assert len(children) == 1
    finally:
        release.set()
        attempt.join(5)
        if stopping.ident is not None:
            stopping.join(5)
        for child in children:
            if child.returncode is None:
                os.killpg(child.pid, 9)
                child.wait(timeout=5)
        transport.close()


def test_birth_identity_mismatch_refuses_to_signal_actual_child(transport_world, monkeypatch):
    transport = transport_world[0]
    child = subprocess.Popen(  # noqa: S603 - fixed owned child fixture
        [sys.executable, "-I", "-S", "-c", "import time;time.sleep(60)"], start_new_session=True
    )
    actual = None
    try:
        actual = identity(child.pid)
        assert actual is not None
        transport.process = child
        transport._process_identity = replace(actual, birth_seconds=actual.birth_seconds + 1)
        with raises(RuntimeError, match="identity changed"):
            transport.stop_local()
        assert identity(child.pid).same_process(actual)
        transport._process_identity = actual
        transport.stop_local()
        assert child.returncode is not None
    finally:
        if actual is None:
            child.kill()
            child.wait(timeout=5)
        else:
            transport._process_identity = actual
            transport.stop_local()


def test_exited_leader_does_not_leave_its_term_ignoring_descendant(transport_world):
    transport = transport_world[0]
    script = """import subprocess,sys,time
child=subprocess.Popen([sys.executable,'-I','-S','-c',
    'import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);'
    'print("ready",flush=True);time.sleep(60)'],stdout=subprocess.PIPE)
assert child.stdout.readline()==b'ready\\n'
print(child.pid,flush=True)
time.sleep(60)
"""
    child = subprocess.Popen(  # noqa: S603 - fixed descendant fixture
        [sys.executable, "-I", "-S", "-c", script], start_new_session=True, stdout=subprocess.PIPE
    )
    transport.process = child
    transport._process_identity = identity(child.pid)
    try:
        assert select.select([child.stdout], [], [], 5)[0]
        descendant_pid = int(child.stdout.readline())
        descendant = identity(descendant_pid)
        assert descendant is not None
        assert descendant.group == child.pid
        transport.stop_local()
        assert child.returncode is not None
        assert identity(descendant_pid) is None
    finally:
        transport.stop_local()
        child.stdout.close()


@mark.parametrize(
    ("field", "value"),
    [
        ("Target", "i-foreign"),
        ("Reason", "another-owner"),
        ("Status", "Failed"),
        ("SessionId", "foreign-session"),
    ],
)
def test_external_session_termination_requires_exact_history_ownership(field, value):
    ssm = Mock()
    ssm.terminate_session.side_effect = ClientError(
        {"Error": {"Code": "ValidationException", "Message": "not a valid state"}},
        "TerminateSession",
    )
    history = {
        "SessionId": "known",
        "Target": "i-owned",
        "Reason": "reason-owned",
        "Status": "Terminated",
    }
    ssm.get_paginator.return_value.paginate.return_value = [{"Sessions": [history]}]
    terminate_owned_session(ssm, "known", "i-owned", "reason-owned")
    history[field] = value
    with raises(TransportRetryError, match="not certified"):
        terminate_owned_session(ssm, "known", "i-owned", "reason-owned")


@mark.parametrize(
    ("target", "field", "value", "message"),
    [
        ("node", "VpcId", "vpc-other", "instance ownership changed"),
        ("node", "Tags", [], "instance ownership changed"),
        ("group", "OwnerId", "999999999999", "security group ownership changed"),
        (
            "group",
            "IpPermissions",
            [{"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22}],
            "admits inbound SSH",
        ),
        ("document", "Content", '{"unverified":"command"}', "document ownership/content changed"),
        ("description", "Hash", "0" * 64, "document version/hash"),
        ("description", "DocumentVersion", "4", "document version/hash"),
    ],
)
def test_changed_access_ownership_or_recreated_document_never_executes_bootstrap(
    transport_world, target, field, value, message
):
    transport, clients, node, group, document, description = transport_world
    {"node": node, "group": group, "document": document, "description": description}[target][
        field
    ] = value
    with raises(TransportAuthorizationError, match=message):
        transport.start()
    clients["ssm"].send_command.assert_not_called()
    assert transport.process is None
    assert transport.session_id is None
    transport.close()
