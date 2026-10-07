"""Disposable two-VPC proof of the production P4 runtime, with durable recovery IDs.

Run: python spikes/dev-vpc-v1/p4_proof.py run --owner <UUID>
Recover: same command with recover instead of run. No application is deployed.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import subprocess
import sys
import time
import urllib.request
from hashlib import sha256
from ipaddress import IPv4Network
from pathlib import Path
from uuid import UUID, uuid4

import boto3
from botocore.exceptions import ClientError

from stelvio.pulumi import get_stelvio_config_dir
from stelvio.tunnel.credentials import AWS_IO, AwsExecutionContext
from stelvio.tunnel.manifest import (
    EndpointNetwork,
    NetworkManifest,
    ResourceNetwork,
    SessionDescription,
    SubnetNetwork,
    VpcNetwork,
)
from stelvio.tunnel.policy import BastionPolicy
from stelvio.tunnel.runtime import NetworkRuntime


def report(phase: str, **details: object) -> None:
    print(json.dumps({"phase": phase, **details}), flush=True)


class Proof:
    def __init__(self, owner: str) -> None:
        if str(UUID(owner)) != owner or not os.geteuid():
            raise ValueError("Canonical owner UUID and nonroot caller required")
        self.owner = owner
        self.root = Path(__file__).parent / "build" / "p4" / owner
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "fixture.json"
        self.session = boto3.Session(profile_name="default", region_name="us-east-1")
        self.account = self.session.client("sts", config=AWS_IO).get_caller_identity()["Account"]
        self.ec2 = self.session.client("ec2", config=AWS_IO)
        self.docdb = self.session.client("docdb", config=AWS_IO)
        self.r53 = self.session.client("route53", config=AWS_IO)
        self.ssm = self.session.client("ssm", config=AWS_IO)
        self.tags = [{"Key": "stlv:p4-owner", "Value": owner}]
        self.filters = [{"Name": "tag:stlv:p4-owner", "Values": [owner]}]
        self.state = (
            json.loads(self.path.read_text())
            if self.path.exists()
            else {"owner": owner, "account": self.account, "region": "us-east-1", "vpcs": []}
        )
        if (self.state["owner"], self.state["account"], self.state["region"]) != (
            owner,
            self.account,
            "us-east-1",
        ):
            raise RuntimeError("Proof recovery context differs")
        self.save()
        self.passwords: dict[str, str] = {}

    @property
    def network_owner(self) -> str:
        return self.state.get("network_owner", self.owner)

    def save(self) -> None:
        temporary = self.path.with_suffix(".tmp")
        with temporary.open("w") as stream:
            json.dump(self.state, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o600)
        temporary.replace(self.path)
        descriptor = os.open(self.root, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def specs(self, kind: str) -> list[dict]:
        return [{"ResourceType": kind, "Tags": self.tags}]

    def create(self) -> None:
        if self.state["vpcs"]:
            raise RuntimeError("Existing proof intent; recover before a fresh UUID run")
        zones = self.ec2.describe_availability_zones(
            Filters=[{"Name": "state", "Values": ["available"]}]
        )["AvailabilityZones"]
        for index, octet in enumerate((254, 253)):
            name = f"stlv-p4-{UUID(self.owner).hex[:20]}-{index}"
            item = {
                "index": index,
                "name": name,
                "cidr": f"10.{octet}.0.0/16",
                "domain": "example.com"
                if index == 0
                else f"v{index}-{UUID(self.owner).hex}.invalid",
            }
            self.state["vpcs"].append(item)
            self.save()  # all deterministic API names and tag ownership precede effects
            item["vpc"] = self.ec2.create_vpc(
                CidrBlock=item["cidr"], TagSpecifications=self.specs("vpc")
            )["Vpc"]["VpcId"]
            self.save()
            self.ec2.modify_vpc_attribute(VpcId=item["vpc"], EnableDnsSupport={"Value": True})
            self.ec2.modify_vpc_attribute(VpcId=item["vpc"], EnableDnsHostnames={"Value": True})
            item["subnets"] = []
            for offset in (1, 2):
                subnet = self.ec2.create_subnet(
                    VpcId=item["vpc"],
                    CidrBlock=f"10.{octet}.{offset}.0/24",
                    AvailabilityZone=zones[offset - 1]["ZoneName"],
                    TagSpecifications=self.specs("subnet"),
                )["Subnet"]
                item["subnets"].append(
                    {
                        "id": subnet["SubnetId"],
                        "az": subnet["AvailabilityZone"],
                        "cidr": subnet["CidrBlock"],
                    }
                )
                self.save()
            item["gateway"] = self.ec2.create_internet_gateway(
                TagSpecifications=self.specs("internet-gateway")
            )["InternetGateway"]["InternetGatewayId"]
            self.save()
            self.ec2.attach_internet_gateway(InternetGatewayId=item["gateway"], VpcId=item["vpc"])
            item["table"] = self.ec2.create_route_table(
                VpcId=item["vpc"], TagSpecifications=self.specs("route-table")
            )["RouteTable"]["RouteTableId"]
            self.save()
            self.ec2.create_route(
                RouteTableId=item["table"],
                DestinationCidrBlock="0.0.0.0/0",
                GatewayId=item["gateway"],
            )
            self.ec2.associate_route_table(
                RouteTableId=item["table"], SubnetId=item["subnets"][0]["id"]
            )
            item["group"] = self.ec2.create_security_group(
                GroupName=name,
                Description="Disposable P4 DocumentDB target",
                VpcId=item["vpc"],
                TagSpecifications=self.specs("security-group"),
            )["GroupId"]
            self.save()
            self.docdb.create_db_subnet_group(
                DBSubnetGroupName=name,
                DBSubnetGroupDescription="Disposable P4 fixture",
                Tags=self.tags,
                SubnetIds=[subnet["id"] for subnet in item["subnets"]],
            )
            password = secrets.token_urlsafe(30)
            self.passwords[name] = password  # never written to receipts or evidence
            self.docdb.create_db_cluster(
                DBClusterIdentifier=name,
                Engine="docdb",
                EngineVersion="5.0.0",
                MasterUsername="proofadmin",
                MasterUserPassword=password,
                DBSubnetGroupName=name,
                VpcSecurityGroupIds=[item["group"]],
                Port=27018,
                StorageEncrypted=True,
                BackupRetentionPeriod=1,
                Tags=self.tags,
            )
            self.docdb.create_db_instance(
                DBInstanceIdentifier=name + "-member",
                DBClusterIdentifier=name,
                Engine="docdb",
                DBInstanceClass="db.t3.medium",
                Tags=self.tags,
            )
            zone = self.r53.create_hosted_zone(
                Name=item["domain"],
                CallerReference=self.owner + f"/{index}",
                HostedZoneConfig={"Comment": self.owner, "PrivateZone": True},
                VPC={"VPCRegion": "us-east-1", "VPCId": item["vpc"]},
            )["HostedZone"]
            item["zone"] = zone["Id"]
            self.save()
            self.r53.change_resource_record_sets(
                HostedZoneId=item["zone"],
                ChangeBatch={
                    "Changes": [
                        {
                            "Action": "CREATE",
                            "ResourceRecordSet": {
                                "Name": "marker." + item["domain"],
                                "Type": "A",
                                "TTL": 1,
                                "ResourceRecords": [{"Value": f"10.{octet}.9.{11 + index}"}],
                            },
                        }
                    ]
                },
            )
            if index == 0:
                self.r53.change_resource_record_sets(
                    HostedZoneId=item["zone"],
                    ChangeBatch={
                        "Changes": [
                            {
                                "Action": "CREATE",
                                "ResourceRecordSet": {
                                    "Name": item["domain"],
                                    "Type": "A",
                                    "TTL": 1,
                                    "ResourceRecords": [{"Value": "10.254.9.11"}],
                                },
                            }
                        ]
                    },
                )
            report("fixture-created", vpc=item["vpc"], cluster=name, domain=item["domain"])

    def available(self) -> None:
        deadline = time.monotonic() + 1800
        previous = None
        while time.monotonic() < deadline:
            statuses = []
            for item in self.state["vpcs"]:
                cluster = self.docdb.describe_db_clusters(DBClusterIdentifier=item["name"])[
                    "DBClusters"
                ][0]
                member = self.docdb.describe_db_instances(
                    DBInstanceIdentifier=item["name"] + "-member"
                )["DBInstances"][0]
                statuses.append((cluster["Status"], member["DBInstanceStatus"]))
                item["hosts"] = [cluster["Endpoint"], cluster["ReaderEndpoint"]]
            if statuses != previous:
                report("database-status", statuses=statuses)
                previous = statuses
            if all(pair == ("available", "available") for pair in statuses):
                self.save()
                return
            time.sleep(5)
        raise TimeoutError("Proof databases did not become available")

    def description(self) -> SessionDescription:
        networks, resources = [], []
        for item in self.state["vpcs"]:
            identity = item["name"]
            subnet = item["subnets"][0]
            networks.append(
                VpcNetwork(
                    identity,
                    self.account,
                    "us-east-1",
                    item["vpc"],
                    "default",
                    (IPv4Network(item["cidr"]),),
                    BastionPolicy.TEMPORARY,
                    (item["domain"],),
                    (SubnetNetwork(subnet["id"], subnet["az"], IPv4Network(subnet["cidr"])),),
                )
            )
            resources.append(
                ResourceNetwork(
                    identity + "-db",
                    identity,
                    "documentdb",
                    item["name"],
                    (27018,),
                    tuple(item["hosts"]),
                    (item["group"],),
                )
            )
        return SessionDescription(
            self.network_owner,
            "p4-proof",
            "dev",
            os.geteuid(),
            NetworkManifest(
                tuple(networks),
                tuple(resources),
                (EndpointNetwork("proof", "proof", tuple(n.identity for n in networks)),),
            ),
        )

    def wait_ready(self, runtime: NetworkRuntime, *, after: int = 0) -> list[dict]:
        deadline = time.monotonic() + 600
        previous = None
        while time.monotonic() < deadline:
            statuses = runtime.status()
            if statuses != previous:
                report("runtime-status", statuses=statuses)
                previous = statuses
            if any(status["state"] == "failed" for status in statuses):
                raise RuntimeError("Production VPC worker failed")
            if (
                all(status["state"] == "ready" for status in statuses)
                and statuses[0]["attempt"] > after
            ):
                return statuses
            time.sleep(1)
        raise TimeoutError("Production VPC workers did not become ready")

    def driver(self, index: int, *, read_only: bool = False) -> None:
        item = self.state["vpcs"][index]
        environment = dict(os.environ)
        environment.update(
            PROOF_HOST=item["hosts"][0],
            PROOF_PASSWORD=self.passwords[item["name"]],
            PROOF_MARKER=self.owner + f"/{index}",
            PROOF_CA=str(self.root / "ca.pem"),
            PROOF_READ_ONLY="1" if read_only else "0",
        )
        script = """import os,json
from pymongo import MongoClient
with MongoClient(os.environ['PROOF_HOST'],27018,username='proofadmin',password=os.environ['PROOF_PASSWORD'],tls=True,tlsCAFile=os.environ['PROOF_CA'],replicaSet='rs0',retryWrites=False,serverSelectionTimeoutMS=15000,connectTimeoutMS=5000,socketTimeoutMS=5000) as client:
    assert client.admin.command('ping')['ok'] == 1
    collection=client.stelvio.p4_proof
    marker=os.environ['PROOF_MARKER']
    if os.environ['PROOF_READ_ONLY']=='0': collection.replace_one({'_id':marker},{'_id':marker,'marker':marker},upsert=True)
    assert collection.find_one({'_id':marker})['marker']==marker
    hosts=sorted(host for host,port in client.nodes)
    assert hosts and any('.docdb.amazonaws.com' in host for host in hosts)
    print(json.dumps({'marker':marker,'tls':True,'replica_set':client.topology_description.replica_set_name,'members':hosts}))
"""
        python = Path(__file__).parents[1] / "vpc-tunnel-app/.venv/bin/python"
        result = subprocess.run(
            [str(python), "-I", "-c", script],
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode:
            # Password must never be exposed by exception text/driver diagnostics.
            raise RuntimeError(
                "Verified TLS DocumentDB driver proof failed: "
                + result.stderr.replace(self.passwords[item["name"]], "[redacted]")
            )
        report("documentdb-driver", **json.loads(result.stdout))

    def exercise(self) -> None:
        with urllib.request.urlopen(
            "https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem", timeout=30
        ) as response:
            (self.root / "ca.pem").write_bytes(response.read())
        description = self.description()
        from stelvio.tunnel.access_state import plan_access
        from stelvio.tunnel.bastion import AMI_PARAMETER

        ami = self.ssm.get_parameter(Name=AMI_PARAMETER)["Parameter"]["Value"]
        self.state.setdefault("access_intents", []).extend(
            [
                plan_access(description, network, ami).to_dict()
                for network in description.manifest.enabled_vpcs
            ]
        )
        self.save()
        context = AwsExecutionContext.capture(
            provider="default", account=self.account, region="us-east-1", profile="default"
        )
        config = get_stelvio_config_dir()
        from stelvio.tunnel.actors import AWS_VERSION

        settings = {
            "bucket": "stlv-state-c054b7aca79c",
            "home_provider": "default",
            "cli": str(config / "bin/pulumi"),
            "aws_provider": str(
                config / ".pulumi/plugins" / f"resource-aws-v{AWS_VERSION}" / "pulumi-resource-aws"
            ),
        }
        runtime = NetworkRuntime(description, (context,), access_settings=settings)
        try:
            ready = self.wait_ready(runtime)
            for index, item in enumerate(self.state["vpcs"]):
                expected = f"10.{254 - index}.9.{11 + index}"
                assert socket.gethostbyname("marker." + item["domain"]) == expected
                report("ordinary-os-private-dns", vpc=item["vpc"], address=expected)
                self.driver(index)
            # Handler environment mutations cannot reach the already isolated child.
            os.environ["AWS_ACCESS_KEY_ID"] = "HANDLER_INVALID_KEY"
            os.environ["AWS_SECRET_ACCESS_KEY"] = "HANDLER_INVALID_SECRET"
            first = self.state["vpcs"][0]
            owned_prefix = "stelvio/" + sha256(self.network_owner.encode()).hexdigest()[:16] + "/"
            sessions = self.ssm.describe_sessions(State="Active")["Sessions"]
            owned = [s for s in sessions if s.get("Reason", "").startswith(owned_prefix)]
            assert len(owned) == 2
            target = next(
                s
                for s in owned
                if s["Target"]
                in [
                    n["InstanceId"]
                    for r in self.ec2.describe_instances(
                        Filters=[
                            {"Name": "vpc-id", "Values": [first["vpc"]]},
                            {"Name": "tag:stlv:tunnel-session", "Values": [self.network_owner]},
                        ]
                    )["Reservations"]
                    for n in r["Instances"]
                ]
            )
            self.ssm.terminate_session(SessionId=target["SessionId"])
            report("interrupt-owned-transport", vpc=first["vpc"])
            self.driver(1, read_only=True)
            self.wait_ready(runtime, after=ready[0]["attempt"])
            self.driver(0, read_only=True)
            self.driver(1, read_only=True)
            assert socket.getaddrinfo("iana.org", 443)
            self.faults(runtime, target["Target"])
            report(
                "PASS",
                checks=[
                    "two-vpc",
                    "ordinary-private-dns",
                    "verified-docdb-tls",
                    "member-discovery",
                    "reconnect",
                    "healthy-vpc-survives",
                    "handler-env-isolation",
                ],
            )
        finally:
            os.environ.pop("AWS_ACCESS_KEY_ID", None)
            os.environ.pop("AWS_SECRET_ACCESS_KEY", None)
            runtime.close()

    def remote(self, instance: str, command: str) -> None:
        nodes = [
            n
            for r in self.ec2.describe_instances(InstanceIds=[instance])["Reservations"]
            for n in r["Instances"]
        ]
        assert len(nodes) == 1
        assert nodes[0]["VpcId"] == self.state["vpcs"][0]["vpc"]
        assert {t["Key"]: t["Value"] for t in nodes[0]["Tags"]}[
            "stlv:tunnel-session"
        ] == self.network_owner
        identifier = self.ssm.send_command(
            InstanceIds=[instance],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": [command]},
            TimeoutSeconds=60,
        )["Command"]["CommandId"]
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            try:
                result = self.ssm.get_command_invocation(CommandId=identifier, InstanceId=instance)
            except ClientError as error:
                if error.response["Error"]["Code"] != "InvocationDoesNotExist":
                    raise
            else:
                if result["Status"] == "Success":
                    return
                if result["Status"] not in {"Pending", "InProgress", "Delayed"}:
                    raise RuntimeError("Owned remote fault command did not complete")
            time.sleep(1)
        raise TimeoutError("Owned remote fault command exceeded budget")

    @staticmethod
    def os_lookup(name: str) -> set[str]:
        script = "import json,socket,sys; print(json.dumps(sorted({r[4][0] for r in socket.getaddrinfo(sys.argv[1],None,socket.AF_INET)})))"
        result = subprocess.run(
            [sys.executable, "-I", "-S", "-c", script, name],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        return set(json.loads(result.stdout)) if not result.returncode else set()

    def faults(self, runtime: NetworkRuntime, instance: str) -> None:
        assert self.os_lookup("example.com") == {"10.254.9.11"}
        # A public answer exists for the declared private name. DNS failure
        # must reject rather than leaking that public answer into this session.
        attempt = runtime.status()[0]["attempt"]
        self.remote(instance, "ip route add blackhole 10.254.0.2/32")
        try:
            deadline = time.monotonic() + 60
            while runtime.status()[0]["state"] == "ready":
                if time.monotonic() >= deadline:
                    raise TimeoutError("Private DNS loss did not invalidate readiness")
                time.sleep(1)
            assert not self.os_lookup("example.com")
            assert self.os_lookup("iana.org")
            self.driver(1, read_only=True)
            report("private-dns-outage", public_fallback=False, healthy_other_vpc=True)
        finally:
            self.remote(instance, "ip route del blackhole 10.254.0.2/32")
        self.wait_ready(runtime, after=attempt)
        assert self.os_lookup("example.com") == {"10.254.9.11"}
        self.driver(0, read_only=True)
        self.remote(
            instance,
            "set -eu; ssh-keygen -q -t ed25519 -N '' -f /run/stlv-proof-key; "
            "mv /run/stlv-proof-key /etc/ssh/ssh_host_ed25519_key; "
            "mv /run/stlv-proof-key.pub /etc/ssh/ssh_host_ed25519_key.pub; systemctl restart sshd",
        )
        current = self.ssm.describe_sessions(
            State="Active", Filters=[{"key": "Target", "value": instance}]
        )["Sessions"]
        prefix = "stelvio/" + sha256(self.network_owner.encode()).hexdigest()[:16] + "/"
        for session in current:
            if session.get("Reason", "").startswith(prefix):
                self.ssm.terminate_session(SessionId=session["SessionId"])
        deadline = time.monotonic() + 600
        while True:
            status = runtime.status()[0]
            if status["state"] == "failed" and status["dns_retained"]:
                assert status["cause"] == "identity-or-configuration"
                break
            if time.monotonic() >= deadline:
                raise TimeoutError("Changed server identity was not terminally fenced")
            time.sleep(1)
        assert not self.os_lookup("example.com")
        self.driver(1, read_only=True)
        assert self.os_lookup("iana.org")
        report("terminal-host-identity", private_dns_retained=True, healthy_other_vpc=True)

    def recover_access(self) -> None:
        from stelvio.tunnel.access_backend import AccessBackend
        from stelvio.tunnel.access_inventory import AccessInventory
        from stelvio.tunnel.access_program import CapturedCredentials
        from stelvio.tunnel.access_state import AccessIntent, AccessJournal
        from stelvio.tunnel.access_unit import AccessUnit
        from stelvio.tunnel.actors import AWS_VERSION, ActorRegistry, stop_registered_actors
        from stelvio.tunnel.engine import TrackedPulumiCommand
        from stelvio.tunnel.processes import ProcessIdentity, identity

        s3 = self.session.client("s3", config=AWS_IO)
        config = get_stelvio_config_dir()
        for saved in self.state.get("access_intents", []):
            intent = AccessIntent.from_dict(saved)
            journal = AccessJournal(s3, "stlv-state-c054b7aca79c", self.account, intent)
            if not list(journal.records()):
                continue
            registry = ActorRegistry(
                journal,
                config / "bin/pulumi",
                config
                / ".pulumi/plugins"
                / f"resource-aws-v{AWS_VERSION}"
                / "pulumi-resource-aws",
            )
            if journal.read("metadata-cleanup.json") is None:
                claim = journal.read("claim.json")
                if claim is not None:
                    stopped = stop_registered_actors(journal, ProcessIdentity(**claim["creator"]))
                    journal.recover_claim(stopped, identity(os.getpid()))
                else:
                    journal.claim(identity(os.getpid()))
                registry.bind()
                for key in tuple(journal.records()):
                    filename = key.removeprefix(intent.prefix)
                    if not filename.startswith("transport-start-"):
                        continue
                    nonce = filename.removeprefix("transport-start-").removesuffix(".json")
                    if journal.read(f"transport-stop-{nonce}.json"):
                        continue
                    observed = journal.read(f"transport-observed-{nonce}.json")
                    if observed is None:
                        raise RuntimeError(
                            "Unknown SSM start outcome retained; explicit reconciliation needed"
                        )
                    from stelvio.tunnel.transport import terminate_owned_session

                    started = journal.read(filename)
                    terminate_owned_session(
                        self.ssm, observed["session_id"], started["instance"], started["reason"]
                    )
                    journal.record(f"transport-stop-{nonce}.json", {"complete": True})
            command = TrackedPulumiCommand(
                registry, provider_credentials=lambda: CapturedCredentials.capture(self.session)
            )
            unit = AccessUnit(
                journal,
                AccessBackend(journal, self.session, command=command),
                AccessInventory(journal, self.session),
                CapturedCredentials.capture(self.session),
                registry,
                registry.require_stopped,
            )
            try:
                unit.stop()
            finally:
                for _, process in registry.children:
                    registry.stop(process)
                registry.require_stopped()
            assert not list(journal.records())
            report("access-recovered-and-removed", unit=intent.unit)

    def down(self) -> None:
        # Deterministic names and owner tags recover lost API returns. Refuse
        # fixture deletion while temporary access still owns an instance.
        instances = [
            n
            for r in self.ec2.describe_instances(
                Filters=[{"Name": "tag:stlv:tunnel-session", "Values": [self.network_owner]}]
            )["Reservations"]
            for n in r["Instances"]
            if n["State"]["Name"] != "terminated"
        ]
        if instances:
            raise RuntimeError(
                "Access resources remain; recover their ownership before fixture teardown"
            )
        for item in self.state["vpcs"]:
            name = item["name"]
            for operation, field, identifier, missing in (
                (
                    self.docdb.delete_db_instance,
                    "DBInstanceIdentifier",
                    name + "-member",
                    "DBInstanceNotFound",
                ),
                (
                    self.docdb.delete_db_cluster,
                    "DBClusterIdentifier",
                    name,
                    "DBClusterNotFoundFault",
                ),
            ):
                deadline = time.monotonic() + 1200
                while True:
                    try:
                        parameters = {field: identifier}
                        if field == "DBClusterIdentifier":
                            parameters["SkipFinalSnapshot"] = True
                        operation(**parameters)
                    except ClientError as error:
                        code = error.response["Error"]["Code"]
                        if code in {missing, "DBInstanceNotFound", "DBInstanceNotFoundFault"}:
                            break
                        if code not in {
                            "InvalidDBClusterStateFault",
                            "InvalidDBInstanceState",
                            "InvalidDBInstanceStateFault",
                        }:
                            raise
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Database teardown still pending; retain fixture IDs")
                    time.sleep(5)
            try:
                self.docdb.delete_db_subnet_group(DBSubnetGroupName=name)
            except ClientError as error:
                if error.response["Error"]["Code"] != "DBSubnetGroupNotFoundFault":
                    raise
            zones = self.r53.list_hosted_zones_by_name(DNSName=item["domain"])["HostedZones"]
            for zone in zones:
                if zone["Name"].rstrip(".") != item["domain"]:
                    continue
                if zone["CallerReference"] != self.owner + f"/{item['index']}":
                    continue  # unrelated same-name private zones are preserved
                records = self.r53.list_resource_record_sets(HostedZoneId=zone["Id"])[
                    "ResourceRecordSets"
                ]
                changes = [
                    {"Action": "DELETE", "ResourceRecordSet": r}
                    for r in records
                    if r["Type"] not in {"NS", "SOA"}
                ]
                if changes:
                    self.r53.change_resource_record_sets(
                        HostedZoneId=zone["Id"], ChangeBatch={"Changes": changes}
                    )
                self.r53.delete_hosted_zone(Id=zone["Id"])
        groups = self.ec2.describe_security_groups(Filters=self.filters)["SecurityGroups"]
        subnets = self.ec2.describe_subnets(Filters=self.filters)["Subnets"]
        tables = self.ec2.describe_route_tables(Filters=self.filters)["RouteTables"]
        gateways = self.ec2.describe_internet_gateways(Filters=self.filters)["InternetGateways"]
        vpcs = self.ec2.describe_vpcs(Filters=self.filters)["Vpcs"]
        allowed = {v["VpcId"] for v in vpcs}
        if any(v["CidrBlock"] not in {"10.254.0.0/16", "10.253.0.0/16"} for v in vpcs):
            raise RuntimeError("Owned VPC CIDR differs")
        if any(r["VpcId"] not in allowed for r in groups + subnets + tables):
            raise RuntimeError("Fixture graph ownership differs")
        for group in groups:
            self.ec2.delete_security_group(GroupId=group["GroupId"])
        for table in tables:
            for association in table["Associations"]:
                if association.get("Main") or association.get("SubnetId") not in {
                    s["SubnetId"] for s in subnets
                }:
                    raise RuntimeError("Foreign route association; preserve fixture")
                self.ec2.disassociate_route_table(
                    AssociationId=association["RouteTableAssociationId"]
                )
            self.ec2.delete_route_table(RouteTableId=table["RouteTableId"])
        for subnet in subnets:
            self.ec2.delete_subnet(SubnetId=subnet["SubnetId"])
        for gateway in gateways:
            for attachment in gateway["Attachments"]:
                if attachment["VpcId"] not in allowed:
                    raise RuntimeError("Foreign gateway attachment; preserve fixture")
                self.ec2.detach_internet_gateway(
                    InternetGatewayId=gateway["InternetGatewayId"], VpcId=attachment["VpcId"]
                )
            self.ec2.delete_internet_gateway(InternetGatewayId=gateway["InternetGatewayId"])
        for vpc in vpcs:
            self.ec2.delete_vpc(VpcId=vpc["VpcId"])
        assert not self.ec2.describe_vpcs(Filters=self.filters)["Vpcs"]
        report("fixture-removed", owner=self.owner)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("run", "resume", "recover"))
    parser.add_argument("--owner", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    proof = Proof(args.owner)
    try:
        if args.command in {"run", "resume"}:
            if args.command == "run":
                proof.create()
            else:
                proof.recover_access()
                for item in proof.state["vpcs"]:
                    password = secrets.token_urlsafe(30)
                    proof.passwords[item["name"]] = password
                    proof.docdb.modify_db_cluster(
                        DBClusterIdentifier=item["name"],
                        MasterUserPassword=password,
                        ApplyImmediately=True,
                    )
            proof.state["network_owner"] = str(uuid4())
            proof.save()
            proof.available()
            proof.exercise()
    finally:
        proof.recover_access()
        proof.down()


if __name__ == "__main__":
    main()
