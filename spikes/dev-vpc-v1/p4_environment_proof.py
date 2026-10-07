"""Live startup-environment proof with no declared resource and one owned VPC."""

# ruff: noqa: C901, PLR0912, PLR0915, S101, S106 - explicit disposable proof harness

import argparse
import os
import shlex
import time
from ipaddress import IPv4Network
from uuid import uuid4

import dns.message
import dns.query
import dns.rcode
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


def remote_dns(fixture: Fixture, owner: str, name: str) -> None:
    nodes = [
        n
        for r in fixture.ec2.describe_instances(
            Filters=[{"Name": "tag:stlv:tunnel-session", "Values": [owner]}]
        )["Reservations"]
        for n in r["Instances"]
        if n["State"]["Name"] == "running"
    ]
    assert len(nodes) == 1
    assert nodes[0]["VpcId"] == fixture.state["vpc"]
    query = dns.message.make_query(name, "A").to_wire().hex()
    script = (
        "import socket,struct; s=socket.create_connection(('10.254.0.2',53),5);"
        f"q=bytes.fromhex('{query}');s.sendall(struct.pack('!H',len(q))+q);"
        "size=struct.unpack('!H',s.recv(2))[0];data=b'';\n"
        "while len(data)<size:\n part=s.recv(size-len(data));"
        "assert part;data+=part\nprint(data.hex())"
    )
    command = fixture.ssm.send_command(
        InstanceIds=[nodes[0]["InstanceId"]],
        DocumentName="AWS-RunShellScript",
        Parameters={"commands": ["runuser -u stlv-tunnel -- python3 -c " + shlex.quote(script)]},
    )["Command"]["CommandId"]
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            result = fixture.ssm.get_command_invocation(
                CommandId=command, InstanceId=nodes[0]["InstanceId"]
            )
        except fixture.ssm.exceptions.InvocationDoesNotExist:
            time.sleep(1)
            continue
        if result["Status"] == "Success":
            answer = dns.message.from_wire(bytes.fromhex(result["StandardOutputContent"].strip()))
            report(
                "aws-node-private-dns",
                rcode=dns.rcode.to_text(answer.rcode()),
                answers=[r.to_text() for r in answer.answer],
                authority=[r.to_text() for r in answer.authority],
            )
            return
        if result["Status"] not in {"Pending", "InProgress", "Delayed"}:
            raise RuntimeError("Owned remote DNS diagnostic failed")
        time.sleep(1)
    raise TimeoutError("Owned remote DNS diagnostic timed out")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("run", "recover"))
    parser.add_argument("--owner", required=True)
    parser.add_argument("--private-dns-diagnostic", action="store_true")
    args = parser.parse_args()
    args.bucket = "stlv-state-c054b7aca79c"
    args.region = "us-east-1"
    os.umask(0o077)
    fixture = Fixture(args)
    recovery = Proof(args.owner)
    r53 = fixture.session.client("route53")
    domain = f"env-{args.owner.replace('-', '')}.example.com"
    runtime = None
    originals = {
        key: os.environ.get(key)
        for key in (
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
            "AWS_PROFILE",
            "AWS_DEFAULT_PROFILE",
        )
    }
    try:
        if args.command == "run":
            fixture.up()
            fixture.ec2.modify_vpc_attribute(
                VpcId=fixture.state["vpc"], EnableDnsHostnames={"Value": True}
            )
            fixture.state["zone_started"] = True
            fixture.save()
            zone = r53.create_hosted_zone(
                Name=domain,
                CallerReference=args.owner,
                HostedZoneConfig={"PrivateZone": True, "Comment": args.owner},
                VPC={"VPCRegion": "us-east-1", "VPCId": fixture.state["vpc"]},
            )["HostedZone"]
            fixture.state["zone"] = zone["Id"]
            fixture.save()
            change = r53.change_resource_record_sets(
                HostedZoneId=zone["Id"],
                ChangeBatch={
                    "Changes": [
                        {
                            "Action": "CREATE",
                            "ResourceRecordSet": {
                                "Name": "*." + domain,
                                "Type": "A",
                                "TTL": 1,
                                "ResourceRecords": [{"Value": "10.254.9.13"}],
                            },
                        }
                    ]
                },
            )
            r53.get_waiter("resource_record_sets_changed").wait(
                Id=change["ChangeInfo"]["Id"], WaiterConfig={"Delay": 2, "MaxAttempts": 90}
            )
            network = VpcNetwork(
                "environment-proof",
                fixture.account,
                "us-east-1",
                fixture.state["vpc"],
                "default",
                (IPv4Network("10.254.0.0/16"),),
                BastionPolicy.TEMPORARY,
                (domain,),
                (
                    SubnetNetwork(
                        fixture.state["subnet"], fixture.state["az"], IPv4Network("10.254.1.0/24")
                    ),
                ),
            )
            description = SessionDescription(
                args.owner,
                "p4-environment-proof",
                "dev",
                os.geteuid(),
                NetworkManifest(
                    (network,), (), (EndpointNetwork("fn", "fn", (network.identity,)),)
                ),
            )
            intent = plan_access(description, network, fixture.state["ami"])
            recovery.state["access_intents"] = [intent.to_dict()]
            recovery.save()
            credentials = fixture.session.get_credentials().get_frozen_credentials()
            report(
                "startup-source",
                method=fixture.session.get_credentials().method,
                temporary_token=bool(credentials.token),
            )
            os.environ.update(
                AWS_ACCESS_KEY_ID=credentials.access_key,
                AWS_SECRET_ACCESS_KEY=credentials.secret_key,
            )
            if credentials.token:
                os.environ["AWS_SESSION_TOKEN"] = credentials.token
            else:
                os.environ.pop("AWS_SESSION_TOKEN", None)
            os.environ.pop("AWS_PROFILE", None)
            os.environ.pop("AWS_DEFAULT_PROFILE", None)
            captured = AwsExecutionContext.capture(
                provider="default", account=fixture.account, region="us-east-1"
            )
            config = get_stelvio_config_dir()
            runtime = NetworkRuntime(
                description,
                (captured,),
                access_settings={
                    "bucket": args.bucket,
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
            os.environ.update(
                AWS_ACCESS_KEY_ID="HANDLER_MUTATION",
                AWS_SECRET_ACCESS_KEY="INVALID",
                AWS_PROFILE="HANDLER_NONEXISTENT_PROFILE",
            )
            deadline = time.monotonic() + 600
            previous = None
            while True:
                statuses = runtime.status()
                if statuses != previous:
                    report("runtime-status", statuses=statuses)
                    previous = statuses
                if statuses[0]["state"] == "ready":
                    assert statuses[0]["resource_probe"] == "unverified"
                    break
                if statuses[0]["state"] == "failed" or time.monotonic() >= deadline:
                    raise RuntimeError("Environment-source runtime did not become ready")
                time.sleep(1)
            # Tunnel identity remains stable across an idle interval after handler mutation.
            attempt = statuses[0]["attempt"]
            time.sleep(6)
            assert runtime.status()[0]["state"] == "ready"
            assert runtime.status()[0]["attempt"] == attempt
            report(
                "PASS",
                startup_environment=True,
                handler_mutation_isolated=True,
                idle_attempt_stable=True,
                full_host_dns_payload=True,
                resource_probe="unverified",
            )
            if not args.private_dns_diagnostic:
                return
            deadline = time.monotonic() + 600
            diagnosed = False
            while True:
                name = uuid4().hex + "." + domain
                answer = dns.query.tcp(dns.message.make_query(name, "A"), "10.254.0.2", timeout=5)
                addresses = {r.address for rr in answer.answer if rr.rdtype == 1 for r in rr}
                if addresses == {"10.254.9.13"}:
                    assert Proof.os_lookup(name) == {"10.254.9.13"}
                    report("host-private-dns", addresses=sorted(addresses))
                    break
                if not diagnosed:
                    remote_dns(fixture, args.owner, name)
                    report(
                        "await-private-zone-publication",
                        rcode=dns.rcode.to_text(answer.rcode()),
                        authority=[record.to_text() for record in answer.authority],
                        association=r53.get_hosted_zone(Id=zone["Id"])["VPCs"],
                    )
                    diagnosed = True
                if time.monotonic() >= deadline:
                    raise TimeoutError("Private fixture DNS publication did not become visible")
                time.sleep(5)
    finally:
        for key, value in originals.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        try:
            if runtime:
                runtime.close()
        finally:
            recovery.recover_access()
            for zone in r53.list_hosted_zones_by_name(DNSName=domain)["HostedZones"]:
                if zone["Name"].rstrip(".") != domain or zone["CallerReference"] != args.owner:
                    continue
                records = r53.list_resource_record_sets(HostedZoneId=zone["Id"])[
                    "ResourceRecordSets"
                ]
                changes = [
                    {"Action": "DELETE", "ResourceRecordSet": r}
                    for r in records
                    if r["Type"] not in {"NS", "SOA"}
                ]
                if changes:
                    r53.change_resource_record_sets(
                        HostedZoneId=zone["Id"], ChangeBatch={"Changes": changes}
                    )
                r53.delete_hosted_zone(Id=zone["Id"])
            fixture.down()


if __name__ == "__main__":
    main()
