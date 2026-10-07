"""Serial live P2 proof using the production temporary access lifecycle.

The disposable SDK fixture is an independently owned VPC/subnet/target group.
No user application is evaluated or deployed. All access effects use the actual
registered native runner, journal, backend, SDK inventory and cleanup path.
Run with an explicit UUID; recover the same UUID after failure before reusing it.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import asdict
from pathlib import Path
from uuid import UUID

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from stelvio.pulumi import get_stelvio_config_dir
from stelvio.tunnel.access_backend import AccessBackend
from stelvio.tunnel.access_inventory import AccessInventory
from stelvio.tunnel.access_program import CapturedCredentials
from stelvio.tunnel.access_state import AccessIntent, AccessJournal, AccessTarget
from stelvio.tunnel.access_unit import AccessUnit
from stelvio.tunnel.actors import AWS_VERSION, ActorRegistry, stop_registered_actors
from stelvio.tunnel.bastion import AMI_PARAMETER
from stelvio.tunnel.engine import TrackedPulumiCommand
from stelvio.tunnel.processes import ProcessIdentity, identity

API_CONFIG = Config(
    connect_timeout=15, read_timeout=15, retries={"mode": "standard", "max_attempts": 3}
)
_IDENTITY_LINES = 2


def report(phase: str, **details: object) -> None:
    print(json.dumps({"phase": phase, **details}), flush=True)  # noqa: T201 - manual proof evidence


class Fixture:
    def __init__(self, args: argparse.Namespace) -> None:
        if str(UUID(args.owner)) != args.owner or os.geteuid() == 0:
            raise ValueError("Require a canonical owner UUID and nonroot caller")
        self.args = args
        self.cidr = getattr(args, "cidr", "10.254.0.0/16")
        if self.cidr not in {"10.254.0.0/16", "10.253.0.0/16"}:
            raise ValueError("Unsupported disposable fixture range")
        self.subnet_cidr = self.cidr.replace(".0.0/16", ".1.0/24")
        self.session = boto3.Session(profile_name="default", region_name=args.region)
        self.account = self.session.client("sts", config=API_CONFIG).get_caller_identity()[
            "Account"
        ]
        self.ec2 = self.session.client("ec2", config=API_CONFIG)
        self.ssm = self.session.client("ssm", config=API_CONFIG)
        self.s3 = self.session.client("s3", config=API_CONFIG)
        self.s3.head_bucket(Bucket=args.bucket, ExpectedBucketOwner=self.account)
        self.root = Path(__file__).parent / "build" / "access-lifecycle" / args.owner
        self.root.mkdir(parents=True, exist_ok=True)
        self.sync_directory(self.root.parent)
        self.path = self.root / "fixture.json"
        self.tags = [{"Key": "stlv:g2-owner", "Value": args.owner}]
        expected = {
            "owner": args.owner,
            "account": self.account,
            "region": args.region,
            "bucket": args.bucket,
        }
        if self.path.exists():
            self.state = json.loads(self.path.read_text())
            if self.state["context"] != expected:
                raise RuntimeError("Fixture recovery account/region/bucket differs")
        else:
            self.state = {"context": expected}
            self.save()

    def save(self) -> None:
        temporary = self.path.with_suffix(".tmp")
        with temporary.open("w") as stream:
            stream.write(json.dumps(self.state, indent=2))
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o600)
        temporary.replace(self.path)
        self.sync_directory(self.root)

    @staticmethod
    def sync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def specs(self, kind: str) -> list[dict]:
        return [{"ResourceType": kind, "Tags": self.tags}]

    def creating(self, kind: str) -> None:
        self.state.setdefault("creates", {})[kind] = {"started": True}
        self.save()

    def up(self) -> None:
        if set(self.state) != {"context"}:
            raise RuntimeError("Fixture already attempted; run recover with this owner")
        self.state["attempted"] = True
        self.save()
        self.state["ami"] = self.ssm.get_parameter(Name=AMI_PARAMETER)["Parameter"]["Value"]
        self.state["az"] = self.ec2.describe_availability_zones(
            Filters=[{"Name": "state", "Values": ["available"]}]
        )["AvailabilityZones"][0]["ZoneName"]
        self.creating("vpc")
        self.state["vpc"] = self.ec2.create_vpc(
            CidrBlock=self.cidr, TagSpecifications=self.specs("vpc")
        )["Vpc"]["VpcId"]
        self.save()
        self.ec2.modify_vpc_attribute(VpcId=self.state["vpc"], EnableDnsSupport={"Value": True})
        self.creating("subnet")
        self.state["subnet"] = self.ec2.create_subnet(
            VpcId=self.state["vpc"],
            CidrBlock=self.subnet_cidr,
            AvailabilityZone=self.state["az"],
            TagSpecifications=self.specs("subnet"),
        )["Subnet"]["SubnetId"]
        self.save()
        self.creating("gateway")
        self.state["gateway"] = self.ec2.create_internet_gateway(
            TagSpecifications=self.specs("internet-gateway")
        )["InternetGateway"]["InternetGatewayId"]
        self.save()
        self.ec2.attach_internet_gateway(
            InternetGatewayId=self.state["gateway"], VpcId=self.state["vpc"]
        )
        self.creating("route_table")
        self.state["route_table"] = self.ec2.create_route_table(
            VpcId=self.state["vpc"], TagSpecifications=self.specs("route-table")
        )["RouteTable"]["RouteTableId"]
        self.save()
        self.ec2.create_route(
            RouteTableId=self.state["route_table"],
            DestinationCidrBlock="0.0.0.0/0",
            GatewayId=self.state["gateway"],
        )
        self.ec2.associate_route_table(
            RouteTableId=self.state["route_table"], SubnetId=self.state["subnet"]
        )
        self.creating("target")
        self.state["target"] = self.ec2.create_security_group(
            GroupName=f"stlv-g2-{self.args.owner}-target",
            Description="Disposable P2 application target",
            VpcId=self.state["vpc"],
            TagSpecifications=self.specs("security-group"),
        )["GroupId"]
        self.save()
        self.ec2.authorize_security_group_ingress(
            GroupId=self.state["target"],
            IpPermissions=[
                {
                    "IpProtocol": "tcp",
                    "FromPort": 27018,
                    "ToPort": 27018,
                    "IpRanges": [{"CidrIp": self.cidr}],
                }
            ],
        )
        self.state["baseline"] = self.target_permissions()
        self.state["fixture_ready"] = True
        self.save()

    def target_permissions(self) -> list:
        group = self.ec2.describe_security_groups(GroupIds=[self.state["target"]])[
            "SecurityGroups"
        ][0]
        return group["IpPermissions"]

    def intent(self) -> AccessIntent:
        if "access_intent" in self.state:
            return AccessIntent.from_dict(self.state["access_intent"])
        return AccessIntent(
            session=self.args.owner,
            unit="00000001",
            app="g2-access-proof",
            environment="dev",
            owner_uid=os.geteuid(),
            account=self.account,
            region=self.args.region,
            vpc_id=self.state["vpc"],
            subnet_id=self.state["subnet"],
            availability_zone=self.state["az"],
            ami=self.state["ami"],
            targets=(AccessTarget(self.state["target"], 27018),),
        )

    def unit(self, *, recover: bool) -> tuple[AccessUnit | None, ActorRegistry | None]:
        intent = self.intent()
        journal = AccessJournal(self.s3, self.args.bucket, self.account, intent)
        if recover and not list(journal.records()):
            return None, None
        if recover and journal.read("metadata-cleanup.json") is not None:
            # The exact tombstone certifies completed AWS/native disposal. This
            # path performs only versioned metadata deletion and needs no actor
            # image, provider, old claim or original creation program.
            return AccessUnit(
                journal,
                AccessBackend(journal, self.session),
                AccessInventory(journal, self.session),
                CapturedCredentials.capture(self.session),
                lambda _, operation: operation(),
                lambda: None,
            ), None
        root = get_stelvio_config_dir()
        registry = ActorRegistry(
            journal,
            root / "bin/pulumi",
            root / ".pulumi/plugins" / f"resource-aws-v{AWS_VERSION}" / "pulumi-resource-aws",
        )
        if recover:
            claim = journal.read("claim.json")
            if claim is not None:
                stopped = stop_registered_actors(journal, ProcessIdentity(**claim["creator"]))
                journal.recover_claim(stopped, identity(os.getpid()))
            elif journal.read("metadata-cleanup.json") is None:
                journal.claim(identity(os.getpid()))
            if journal.read("metadata-cleanup.json") is None:
                registry.bind()
        command = TrackedPulumiCommand(
            registry, provider_credentials=lambda: CapturedCredentials.capture(self.session)
        )
        backend = AccessBackend(journal, self.session, command=command)
        unit = AccessUnit(
            journal,
            backend,
            AccessInventory(journal, self.session),
            CapturedCredentials.capture(self.session),
            registry,
            registry.require_stopped,
        )
        return unit, registry

    def readiness(self, descriptor: dict) -> None:
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            entries = self.ssm.describe_instance_information(
                Filters=[{"Key": "InstanceIds", "Values": [descriptor["instance_id"]]}]
            )["InstanceInformationList"]
            if entries and entries[0]["PingStatus"] == "Online":
                break
            time.sleep(2)
        else:
            raise RuntimeError("Temporary instance did not become SSM online")
        command = self.ssm.send_command(
            InstanceIds=[descriptor["instance_id"]], DocumentName=descriptor["identity_document"]
        )["Command"]["CommandId"]
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            try:
                result = self.ssm.get_command_invocation(
                    CommandId=command, InstanceId=descriptor["instance_id"]
                )
            except ClientError as error:
                if error.response["Error"]["Code"] != "InvocationDoesNotExist":
                    raise
            else:
                if result["Status"] == "Success":
                    lines = result["StandardOutputContent"].strip().splitlines()
                    if (
                        len(lines) != _IDENTITY_LINES
                        or not lines[0].startswith("ssh-ed25519 ")
                        or lines[1] != "stelvio-tunnel-ready"
                    ):
                        raise RuntimeError(
                            "SSM identity document returned an invalid readiness response"
                        )
                    report("ssm-ready", instance=descriptor["instance_id"])
                    return
                if result["Status"] not in {"Pending", "InProgress", "Delayed"}:
                    raise RuntimeError(f"SSM readiness failed: {result['Status']}")
            time.sleep(1)
        raise RuntimeError("SSM identity command did not complete")

    def down(self) -> None:  # noqa: C901, PLR0912 - serial dependency and ownership fences
        # Exact owner tags recover fixture API returns lost before the local ID
        # receipt. Refuse foreign VPC/group/route dependencies instead of sweeping.
        filters = [{"Name": "tag:stlv:g2-owner", "Values": [self.args.owner]}]
        groups = self.ec2.describe_security_groups(Filters=filters)["SecurityGroups"]
        subnets = self.ec2.describe_subnets(Filters=filters)["Subnets"]
        vpcs = self.ec2.describe_vpcs(Filters=filters)["Vpcs"]
        tables = self.ec2.describe_route_tables(Filters=filters)["RouteTables"]
        gateways = self.ec2.describe_internet_gateways(Filters=filters)["InternetGateways"]
        if any(len(items) > 1 for items in (groups, subnets, vpcs, tables, gateways)):
            raise RuntimeError("Ambiguous fixture ownership; teardown refused")
        graph = (
            (groups, "target", "GroupId"),
            (subnets, "subnet", "SubnetId"),
            (vpcs, "vpc", "VpcId"),
            (tables, "route_table", "RouteTableId"),
            (gateways, "gateway", "InternetGatewayId"),
        )
        for items, key, field in graph:
            if items and self.state.get(key) not in {None, items[0][field]}:
                raise RuntimeError("Fixture physical ID differs from its WAL; teardown refused")
        vpc_id = vpcs[0]["VpcId"] if vpcs else self.state.get("vpc")
        if vpcs and vpcs[0]["CidrBlock"] != self.cidr:
            raise RuntimeError("Foreign fixture VPC; teardown refused")
        if any(item["VpcId"] != vpc_id for item in groups + subnets + tables):
            raise RuntimeError("Fixture graph belongs to a different VPC; teardown refused")
        subnet_ids = {subnet["SubnetId"] for subnet in subnets}
        for group in groups:
            if group["GroupName"] != f"stlv-g2-{self.args.owner}-target":
                raise RuntimeError("Foreign fixture group; teardown refused")
        for subnet in subnets:
            if subnet["CidrBlock"] != self.subnet_cidr:
                raise RuntimeError("Foreign fixture subnet; teardown refused")
        for table in tables:
            if any(
                association.get("Main") or association.get("SubnetId") not in subnet_ids
                for association in table["Associations"]
            ):
                raise RuntimeError("Foreign fixture route association; teardown refused")
        for gateway in gateways:
            if any(attachment["VpcId"] != vpc_id for attachment in gateway["Attachments"]):
                raise RuntimeError("Foreign fixture gateway attachment; teardown refused")
        self.check_known_ids(graph, absent=False)
        for group in groups:
            if group["GroupName"] != f"stlv-g2-{self.args.owner}-target":
                raise RuntimeError("Foreign fixture group; teardown refused")
            self.ec2.delete_security_group(GroupId=group["GroupId"])
        for table in tables:
            for association in table["Associations"]:
                if association.get("Main") or association.get("SubnetId") not in {
                    s["SubnetId"] for s in subnets
                }:
                    raise RuntimeError("Foreign fixture route association; teardown refused")
                self.ec2.disassociate_route_table(
                    AssociationId=association["RouteTableAssociationId"]
                )
            self.ec2.delete_route_table(RouteTableId=table["RouteTableId"])
        for subnet in subnets:
            if subnet["CidrBlock"] != self.subnet_cidr:
                raise RuntimeError("Foreign fixture subnet; teardown refused")
            self.ec2.delete_subnet(SubnetId=subnet["SubnetId"])
        for gateway in gateways:
            for attachment in gateway["Attachments"]:
                if attachment["VpcId"] not in {v["VpcId"] for v in vpcs}:
                    raise RuntimeError("Foreign fixture gateway attachment; teardown refused")
                self.ec2.detach_internet_gateway(
                    InternetGatewayId=gateway["InternetGatewayId"], VpcId=attachment["VpcId"]
                )
            self.ec2.delete_internet_gateway(InternetGatewayId=gateway["InternetGatewayId"])
        for vpc in vpcs:
            if vpc["CidrBlock"] != self.cidr:
                raise RuntimeError("Foreign fixture VPC; teardown refused")
            self.ec2.delete_vpc(VpcId=vpc["VpcId"])
        self.check_known_ids(graph, absent=True)
        if any(
            [
                self.ec2.describe_vpcs(Filters=filters)["Vpcs"],
                self.ec2.describe_subnets(Filters=filters)["Subnets"],
                self.ec2.describe_security_groups(Filters=filters)["SecurityGroups"],
                self.ec2.describe_route_tables(Filters=filters)["RouteTables"],
                self.ec2.describe_internet_gateways(Filters=filters)["InternetGateways"],
            ]
        ):
            raise RuntimeError("Fixture teardown absence not confirmed")
        report("fixture-removed", owner=self.args.owner)

    def check_known_ids(self, graph: tuple, *, absent: bool) -> None:
        queries = {
            "target": (
                self.ec2.describe_security_groups,
                "GroupIds",
                "SecurityGroups",
                "InvalidGroup.NotFound",
            ),
            "subnet": (
                self.ec2.describe_subnets,
                "SubnetIds",
                "Subnets",
                "InvalidSubnetID.NotFound",
            ),
            "vpc": (self.ec2.describe_vpcs, "VpcIds", "Vpcs", "InvalidVpcID.NotFound"),
            "route_table": (
                self.ec2.describe_route_tables,
                "RouteTableIds",
                "RouteTables",
                "InvalidRouteTableID.NotFound",
            ),
            "gateway": (
                self.ec2.describe_internet_gateways,
                "InternetGatewayIds",
                "InternetGateways",
                "InvalidInternetGatewayID.NotFound",
            ),
        }
        for items, key, field in graph:
            known = self.state.get(key)
            if not known:
                if items and not absent:
                    # Adopt only the previously validated exact owner-tagged
                    # graph, before deleting creates with lost SDK responses.
                    self.state[key] = items[0][field]
                    self.save()
                elif self.state.get("creates", {}).get(key) and not absent:
                    raise RuntimeError(
                        "Fixture create result is uncertain; retain creation intent"
                    )
                continue
            query, argument, response_key, not_found = queries[key]
            try:
                result = query(**{argument: [known]})[response_key]
            except ClientError as error:
                if error.response["Error"]["Code"] != not_found:
                    raise
            else:
                if result and (absent or not items or items[0][field] != known):
                    raise RuntimeError(
                        "Known fixture resource remains or lost ownership; retain WAL"
                    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("run", "recover"))
    parser.add_argument("--owner", required=True)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--region", default="us-east-1")
    args = parser.parse_args()
    os.umask(0o077)
    fixture = Fixture(args)
    unit = None
    registry = None
    try:
        if args.command == "run":
            report("fixture-create", owner=args.owner, profile="default", region=args.region)
            fixture.up()
            fixture.state["access_attempted"] = True
            fixture.state["access_intent"] = fixture.intent().to_dict()
            fixture.save()
        if fixture.state.get("access_attempted"):
            unit, registry = fixture.unit(recover=args.command == "recover")
            if args.command == "run":
                descriptor = asdict(unit.start())
                fixture.state["descriptor"] = descriptor
                fixture.save()
                report("access-created", **descriptor)
                fixture.readiness(descriptor)
            if unit:
                unit.stop()
                unit.stop()
            if (
                not fixture.state.get("fixture_disposal")
                and fixture.target_permissions() != fixture.state["baseline"]
            ):
                raise RuntimeError("Application target ingress changed during access cleanup")
            if unit and any(unit.journal.records()):
                raise RuntimeError("Access metadata remains after disposal")
            report("access-removed", application_preserved=True)
        fixture.state["fixture_disposal"] = True
        fixture.save()
        fixture.down()
        report("PASS", owner=args.owner)
    finally:
        if registry:
            for _, process in registry.children:
                registry.stop(process)
            registry.require_stopped()
        if unit and unit.journal.read("claim.json"):
            # If cleanup failed, keep its immutable receipts for a fresh caller.
            # No application/fixture deletion follows an unclean access unit.
            report(
                "recovery-retained", owner=args.owner, command="recover", path=str(fixture.root)
            )


if __name__ == "__main__":
    main()
