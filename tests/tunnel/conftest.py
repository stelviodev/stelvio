"""Shared deterministic AWS and OpenSSH boundaries for tunnel command tests."""

import base64
import os
import subprocess
from ipaddress import IPv4Network
from pathlib import Path
from threading import Event
from unittest.mock import Mock

from pytest import fixture

from stelvio.tunnel.manifest import AccessDescriptor, SubnetNetwork, VpcNetwork
from stelvio.tunnel.policy import BastionPolicy
from stelvio.tunnel.transport import SshTransport


@fixture
def transport_world(tmp_path, monkeypatch):
    network = VpcNetwork(
        identity="vpc-owner",
        account="123456789012",
        region="us-east-1",
        vpc_id="vpc-12345678",
        provider="provider",
        cidrs=(IPv4Network("10.254.0.0/16"),),
        policy=BastionPolicy.PERSISTENT,
        dns_domains=(),
        public_subnets=(
            SubnetNetwork(
                "subnet-public",
                "us-east-1a",
                IPv4Network("10.254.0.0/24"),
            ),
        ),
        private_subnets=(
            SubnetNetwork(
                "subnet-private",
                "us-east-1a",
                IPv4Network("10.254.1.0/24"),
            ),
        ),
        isolated_subnets=(
            SubnetNetwork(
                "subnet-isolated",
                "us-east-1a",
                IPv4Network("10.254.2.0/24"),
            ),
        ),
        access=AccessDescriptor("i-12345678", "sg-12345678", "us-east-1a", "vpc-owner"),
    )
    session = Mock(region_name="us-east-1")
    session.get_partition_for_region.return_value = "aws"
    cache = tmp_path.resolve() / "cache"
    cache.mkdir(mode=0o700)
    monkeypatch.setattr("stelvio.tunnel.host_trust.get_bin_path", lambda: cache)
    monkeypatch.setattr("stelvio.tunnel.assets._no_acl", lambda *args: None)
    monkeypatch.setattr("stelvio.tunnel.host_trust._acl", lambda *args: None)
    clients = {service: Mock() for service in ("ec2", "ssm", "sts")}
    session.client.side_effect = lambda name, **kwargs: clients[name]
    clients["sts"].get_caller_identity.return_value = {"Account": "123456789012"}
    tags = [{"Key": "stelvio:app", "Value": "app"}]
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
    clients["ssm"].describe_instance_information.return_value = {
        "InstanceInformationList": [{"PingStatus": "Online"}]
    }
    transport = SshTransport(network, session, Event(), ownership={"stelvio:app": "app"})
    return transport, clients, node, group


@fixture
def ssh_world(transport_world, monkeypatch):
    """Model OpenSSH's host checks, without replacing the Python startup flow."""
    import io
    from types import SimpleNamespace

    from stelvio.tunnel.host_trust import HOST_KEY_PREFIX

    transport, clients, *_ = transport_world
    key = "ssh-ed25519 " + base64.b64encode(HOST_KEY_PREFIX + bytes(range(32))).decode()
    changed_key = (
        "ssh-ed25519 " + base64.b64encode(HOST_KEY_PREFIX + bytes(reversed(range(32)))).decode()
    )
    server = SimpleNamespace(
        key=key, changed_key=changed_key, fail_auth=False, exited=False, calls=[]
    )
    clients["ec2-instance-connect"] = Mock()
    clients["ec2-instance-connect"].send_ssh_public_key.return_value = {"Success": True}
    clients["ssm"].start_session.return_value = {"SessionId": "owned-test"}
    clients["ssm"].meta.endpoint_url = "https://ssm.us-east-1.amazonaws.com"
    monkeypatch.setattr("stelvio.tunnel.transport.installed_plugin", lambda: Path("/usr/bin/true"))

    def run(arguments, **kwargs):
        if arguments[0] == "/usr/bin/ssh-keygen":
            path = Path(arguments[arguments.index("-f") + 1])
            path.write_text("ephemeral private fixture")
            path.with_suffix(".pub").write_text(key)
        return subprocess.CompletedProcess(arguments, 0)

    def spawn(arguments, **kwargs):
        from stelvio.tunnel.host_trust import fingerprint

        options = dict(
            arguments[i + 1].split("=", 1) for i, arg in enumerate(arguments) if arg == "-o"
        )
        hosts = Path(options["UserKnownHostsFile"])
        existing = hosts.read_text()
        mismatch = bool(existing and existing != f"{options['HostKeyAlias']} {server.key}\n")
        if not existing:
            assert options["StrictHostKeyChecking"] == "accept-new"
            hosts.write_text(f"{options['HostKeyAlias']} {server.key}\n")
        else:
            assert options["StrictHostKeyChecking"] == "yes"
        server.exited = mismatch or server.fail_auth
        error = (
            f"REMOTE HOST IDENTIFICATION HAS CHANGED\n{fingerprint(server.key)}\n"
            if mismatch
            else ""
        )
        child = SimpleNamespace(pid=123456789, stderr=io.BytesIO(error.encode()))
        if not server.exited:
            Path(arguments[arguments.index("-S") + 1]).touch()
        server.calls.append((options, kwargs))
        return child

    def current_identity(pid):
        value = SimpleNamespace(group=pid, uid=os.geteuid(), status=5 if server.exited else 1)
        value.same_process = lambda other: True
        return value

    monkeypatch.setattr("stelvio.tunnel.transport.subprocess.run", run)
    monkeypatch.setattr("stelvio.tunnel.transport.subprocess.Popen", spawn)
    monkeypatch.setattr("stelvio.tunnel.transport.identity", current_identity)
    original = SshTransport._stop_local

    def stop(self):
        self.process = None
        self._process_identity = None
        self._join_ssh_errors()

    monkeypatch.setattr(SshTransport, "_stop_local", stop)
    yield transport, server, key
    transport.close()
    monkeypatch.setattr(SshTransport, "_stop_local", original)
