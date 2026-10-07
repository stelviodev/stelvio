"""Two real VPCs, uncached private DNS, independent TCP markers and host-key fencing."""

# ruff: noqa: C901, PLR0912, PLR0915, S101 - explicit disposable proof harness

import argparse
import os
import shlex
import socket
import time
from hashlib import sha256
from ipaddress import IPv4Network
from uuid import uuid4

from aws_access_lifecycle import Fixture
from p4_proof import Proof, report

from stelvio.pulumi import get_stelvio_config_dir
from stelvio.tunnel.access_state import plan_access
from stelvio.tunnel.actors import AWS_VERSION
from stelvio.tunnel.credentials import AwsExecutionContext
from stelvio.tunnel.manifest import (
    EndpointNetwork,
    NetworkManifest,
    SessionDescription,
    SubnetNetwork,
    VpcNetwork,
)
from stelvio.tunnel.policy import BastionPolicy
from stelvio.tunnel.runtime import NetworkRuntime


def marker(node: dict, expected: str) -> None:
    with socket.create_connection((node["PrivateIpAddress"], 3333), timeout=5) as stream:
        assert stream.recv(1024).decode().strip() == expected


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("run", "recover"))
    parser.add_argument("--owner", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    proof = Proof(args.owner)
    if args.command == "run":
        if proof.state["vpcs"]:
            raise RuntimeError("Recover this owner before starting a fresh UUID")
        proof.state["fixture_ids"] = [str(uuid4()), str(uuid4())]
        proof.state["network_owner"] = args.owner
        proof.save()
    fixtures = [
        Fixture(
            argparse.Namespace(
                owner=owner,
                region="us-east-1",
                bucket="stlv-state-c054b7aca79c",
                cidr=f"10.{254 - index}.0.0/16",
            )
        )
        for index, owner in enumerate(proof.state["fixture_ids"])
    ]
    runtime = None
    try:
        if args.command == "run":
            for index, fixture in enumerate(fixtures):
                fixture.up()
                fixture.ec2.modify_vpc_attribute(
                    VpcId=fixture.state["vpc"], EnableDnsHostnames={"Value": True}
                )
                domain = (
                    "1.1.1.1.nip.io"
                    if not index
                    else f"other-{args.owner.replace('-', '')}.invalid"
                )
                item = {
                    "index": index,
                    "domain": domain,
                    "vpc": fixture.state["vpc"],
                    "name": f"network-{index}",
                }
                proof.state["vpcs"].append(item)
                proof.save()
                zone = proof.r53.create_hosted_zone(
                    Name=domain,
                    CallerReference=args.owner + f"/{index}",
                    HostedZoneConfig={"PrivateZone": True, "Comment": args.owner},
                    VPC={"VPCRegion": "us-east-1", "VPCId": item["vpc"]},
                )["HostedZone"]
                item["zone"] = zone["Id"]
                proof.save()
                proof.r53.change_resource_record_sets(
                    HostedZoneId=zone["Id"],
                    ChangeBatch={
                        "Changes": [
                            {
                                "Action": "CREATE",
                                "ResourceRecordSet": {
                                    "Name": "*." + domain,
                                    "Type": "A",
                                    "TTL": 1,
                                    "ResourceRecords": [
                                        {"Value": f"10.{254 - index}.9.{11 + index}"}
                                    ],
                                },
                            }
                        ]
                    },
                )
            networks = tuple(
                VpcNetwork(
                    f"network-{index}",
                    proof.account,
                    "us-east-1",
                    fixture.state["vpc"],
                    "default",
                    (IPv4Network(f"10.{254 - index}.0.0/16"),),
                    BastionPolicy.TEMPORARY,
                    (proof.state["vpcs"][index]["domain"],),
                    (
                        SubnetNetwork(
                            fixture.state["subnet"],
                            fixture.state["az"],
                            IPv4Network(f"10.{254 - index}.1.0/24"),
                        ),
                    ),
                )
                for index, fixture in enumerate(fixtures)
            )
            description = SessionDescription(
                args.owner,
                "p4-network-faults",
                "dev",
                os.geteuid(),
                NetworkManifest(
                    networks,
                    (),
                    (EndpointNetwork("fn", "fn", tuple(n.identity for n in networks)),),
                ),
            )
            proof.state["access_intents"] = [
                plan_access(description, n, fixtures[i].state["ami"]).to_dict()
                for i, n in enumerate(networks)
            ]
            proof.save()
            context = AwsExecutionContext.capture(
                provider="default", account=proof.account, region="us-east-1", profile="default"
            )
            config = get_stelvio_config_dir()
            runtime = NetworkRuntime(
                description,
                (context,),
                access_settings={
                    "bucket": "stlv-state-c054b7aca79c",
                    "home_provider": "default",
                    "cli": str(config / "bin/pulumi"),
                    "aws_provider": str(
                        config
                        / ".pulumi/plugins"
                        / f"resource-aws-v{AWS_VERSION}"
                        / "pulumi-resource-aws"
                    ),
                },
            )
            proof.wait_ready(runtime)
            nodes = [
                n
                for r in proof.ec2.describe_instances(
                    Filters=[{"Name": "tag:stlv:tunnel-session", "Values": [args.owner]}]
                )["Reservations"]
                for n in r["Instances"]
                if n["State"]["Name"] == "running"
            ]
            ordered = [
                next(n for n in nodes if n["VpcId"] == fixture.state["vpc"])
                for fixture in fixtures
            ]
            for index, node in enumerate(ordered):
                command = (
                    "nohup runuser -u stlv-tunnel -- python3 -u -c "
                    + shlex.quote(
                        "import socket;server=socket.socket();"
                        "server.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);"
                        "server.bind(('0.0.0.0',3333));server.listen();\n"
                        "while True:\n c,a=server.accept();"
                        f"c.sendall({(args.owner + f'/{index}' + chr(10)).encode()!r});c.close()"
                    )
                    + " >/run/stlv-marker.log 2>&1 </dev/null &"
                )
                # Own SSM targets verified independently; second target belongs to its own VPC.
                identifier = proof.ssm.send_command(
                    InstanceIds=[node["InstanceId"]],
                    DocumentName="AWS-RunShellScript",
                    Parameters={"commands": [command]},
                )["Command"]["CommandId"]
                deadline = time.monotonic() + 90
                while time.monotonic() < deadline:
                    try:
                        result = proof.ssm.get_command_invocation(
                            CommandId=identifier, InstanceId=node["InstanceId"]
                        )
                    except proof.ssm.exceptions.InvocationDoesNotExist:
                        time.sleep(1)
                        continue
                    if result["Status"] == "Success":
                        break
                    if result["Status"] not in {"Pending", "InProgress", "Delayed"}:
                        raise RuntimeError("Owned marker startup failed")
                    time.sleep(1)
                else:
                    raise TimeoutError("Owned marker startup timed out")
                marker(node, args.owner + f"/{index}")
            report("two-vpc-tcp-markers", nodes=[n["InstanceId"] for n in ordered])
            domain = proof.state["vpcs"][0]["domain"]
            assert proof.os_lookup(uuid4().hex + "." + domain) == {"10.254.9.11"}
            attempt = runtime.status()[0]["attempt"]
            proof.remote(ordered[0]["InstanceId"], "ip route add blackhole 10.254.0.2/32")
            try:
                deadline = time.monotonic() + 60
                while runtime.status()[0]["state"] == "ready":
                    if time.monotonic() > deadline:
                        raise TimeoutError("DNS failure did not invalidate readiness")
                    time.sleep(1)
                addresses = proof.os_lookup(uuid4().hex + "." + domain)
                report("fresh-private-outage", addresses=sorted(addresses))
                assert not addresses
                marker(ordered[1], args.owner + "/1")
                assert proof.os_lookup("iana.org")
            finally:
                proof.remote(ordered[0]["InstanceId"], "ip route del blackhole 10.254.0.2/32")
            proof.wait_ready(runtime, after=attempt)
            marker(ordered[0], args.owner + "/0")
            assert proof.os_lookup(uuid4().hex + "." + domain) == {"10.254.9.11"}
            proof.remote(
                ordered[0]["InstanceId"],
                "set -eu; ssh-keygen -q -t ed25519 -N '' -f /run/stlv-proof-key; "
                "mv /run/stlv-proof-key /etc/ssh/ssh_host_ed25519_key; "
                "mv /run/stlv-proof-key.pub /etc/ssh/ssh_host_ed25519_key.pub; "
                "systemctl restart sshd",
            )
            for session in proof.ssm.describe_sessions(
                State="Active", Filters=[{"key": "Target", "value": ordered[0]["InstanceId"]}]
            )["Sessions"]:
                prefix = "stelvio/" + sha256(args.owner.encode()).hexdigest()[:16] + "/"
                if session.get("Reason", "").startswith(prefix):
                    proof.ssm.terminate_session(SessionId=session["SessionId"])
            deadline = time.monotonic() + 600
            while True:
                status = runtime.status()[0]
                if status["state"] == "failed" and status["dns_retained"]:
                    assert status["cause"] == "identity-or-configuration"
                    break
                if time.monotonic() > deadline:
                    raise TimeoutError("Changed identity not terminally fenced")
                time.sleep(1)
            assert not proof.os_lookup(uuid4().hex + "." + domain)
            marker(ordered[1], args.owner + "/1")
            report(
                "PASS",
                private_dns_loss=True,
                automatic_recovery=True,
                changed_host_key_terminal=True,
                rejection_retained=True,
                healthy_other_vpc=True,
            )
    finally:
        try:
            if runtime:
                runtime.close()
        finally:
            proof.recover_access()
            for item in proof.state["vpcs"]:
                for zone in proof.r53.list_hosted_zones_by_name(DNSName=item["domain"])[
                    "HostedZones"
                ]:
                    if (
                        zone["Name"].rstrip(".") != item["domain"]
                        or zone["CallerReference"] != args.owner + f"/{item['index']}"
                    ):
                        continue
                    records = proof.r53.list_resource_record_sets(HostedZoneId=zone["Id"])[
                        "ResourceRecordSets"
                    ]
                    changes = [
                        {"Action": "DELETE", "ResourceRecordSet": r}
                        for r in records
                        if r["Type"] not in {"NS", "SOA"}
                    ]
                    if changes:
                        proof.r53.change_resource_record_sets(
                            HostedZoneId=zone["Id"], ChangeBatch={"Changes": changes}
                        )
                    proof.r53.delete_hosted_zone(Id=zone["Id"])
            for fixture in fixtures:
                fixture.down()


if __name__ == "__main__":
    main()
