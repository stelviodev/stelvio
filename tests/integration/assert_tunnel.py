"""Bounded AWS read-back checks for standalone tunnel acceptance."""

from __future__ import annotations

from dataclasses import dataclass
from time import monotonic, sleep
from typing import TYPE_CHECKING

from botocore.config import Config
from botocore.exceptions import ClientError

if TYPE_CHECKING:
    from collections.abc import Sequence

ABSENCE_TIMEOUT = 300
ABSENCE_POLL_INTERVAL = 5
AWS_CONNECT_TIMEOUT = 2
AWS_READ_TIMEOUT = 6
AWS_REQUEST_WINDOW = AWS_CONNECT_TIMEOUT + AWS_READ_TIMEOUT + 0.5
AWS_MAX_REQUESTS = 32


@dataclass(frozen=True)
class AbsenceAuditOptions:
    """Extra scope and retry bounds for the post-destroy inventory audit."""

    volume_ids: tuple[str, ...] = ()
    timeout: float = ABSENCE_TIMEOUT
    poll_interval: float = ABSENCE_POLL_INTERVAL


DEFAULT_ABSENCE_AUDIT_OPTIONS = AbsenceAuditOptions()


class _AuditDeadlineExceededError(TimeoutError):
    pass


class _RequestBudget:
    def __init__(self, deadline: float) -> None:
        self.deadline = deadline
        self.requests = 0

    def before_call(self, **_kwargs) -> None:
        remaining = self.deadline - monotonic()
        if remaining < AWS_REQUEST_WINDOW or self.requests >= AWS_MAX_REQUESTS:
            raise _AuditDeadlineExceededError(
                "AWS absence audit exhausted its bounded time or request budget"
            )
        self.requests += 1


class _BoundedSession:
    def __init__(self, sdk, deadline: float) -> None:
        self.sdk = sdk
        self.budget = _RequestBudget(deadline)

    def client(self, service: str):
        client = self.sdk.client(
            service,
            config=Config(
                connect_timeout=AWS_CONNECT_TIMEOUT,
                read_timeout=AWS_READ_TIMEOUT,
                retries={"total_max_attempts": 1},
            ),
        )
        events = getattr(getattr(getattr(client, "meta", None), "events", None), "register", None)
        if events is not None:
            events(f"before-call.{service}.*", self.budget.before_call)
        return client


def assert_application_absent(
    app: str,
    env: str,
    *,
    sdk,
    options: AbsenceAuditOptions = DEFAULT_ABSENCE_AUDIT_OPTIONS,
) -> None:
    """Wait for owned application resources to disappear after stack destruction.

    Terminated EC2 instances and deleted NAT gateways can remain visible in AWS
    inventory for a period after destruction. Those terminal states count as absent.
    """
    if options.timeout < 0 or options.poll_interval <= 0:
        raise ValueError("Absence timeout must be nonnegative and poll interval must be positive")

    deadline = monotonic() + options.timeout
    while True:
        # Each inventory pass gets its own call-count budget. The shared absolute
        # deadline still bounds the whole retry loop.
        bounded_sdk = _BoundedSession(sdk, deadline)
        try:
            remaining = _application_resources_remaining(
                app, env, sdk=bounded_sdk, volume_ids=options.volume_ids
            )
        except _AuditDeadlineExceededError as error:
            raise AssertionError(
                f"AWS resource audit could not certify absence within {options.timeout}s: {error}"
            ) from error
        if not remaining:
            return

        delay = min(options.poll_interval, deadline - monotonic())
        if delay <= 0:
            raise AssertionError(
                f"AWS resources remain for {app}/{env} after {options.timeout}s: "
                + ", ".join(remaining)
            )
        sleep(delay)


def _application_resources_remaining(
    app: str, env: str, *, sdk, volume_ids: Sequence[str] = ()
) -> list[str]:
    """Return tagged or name-owned resources that have not reached absence."""
    prefix = f"{app}-{env}-"
    return sorted(
        [
            *_ec2_resources(app, sdk=sdk, volume_ids=volume_ids),
            *_database_resources(prefix, sdk=sdk),
            *_lambda_resources(prefix, sdk=sdk),
            *_iam_resources(prefix, sdk=sdk),
            *_ssm_resources(prefix, sdk=sdk),
        ]
    )


def capture_bastion_volume_ids(*, sdk, instance_ids: Sequence[str]) -> tuple[str, ...]:
    """Capture exact attached-volume IDs before destroying the proof stack.

    Root block devices may not inherit application tags, so they must be retained
    from the bastion inventory before the instances disappear.
    """
    expected = set(instance_ids)
    if not expected or len(expected) != len(instance_ids):
        raise ValueError("Bastion instance IDs must be nonempty and unique")

    # Keep this pre-destroy lookup from delaying cleanup if AWS is unresponsive.
    # A failed capture is reported after destroy, while inventory reads still run.
    capture_deadline = monotonic() + AWS_REQUEST_WINDOW + 1
    bounded_sdk = _BoundedSession(sdk, capture_deadline)
    response = bounded_sdk.client("ec2").describe_instances(InstanceIds=list(instance_ids))
    instances = [
        instance
        for reservation in response.get("Reservations", [])
        for instance in reservation.get("Instances", [])
    ]
    observed = {instance.get("InstanceId") for instance in instances}
    if observed != expected:
        raise AssertionError(
            "Could not capture every proof bastion before destroy; "
            f"expected {sorted(expected)}, observed {sorted(observed)}"
        )

    captured: list[str] = []
    for instance in instances:
        volume_ids = [
            mapping.get("Ebs", {}).get("VolumeId")
            for mapping in instance.get("BlockDeviceMappings", [])
        ]
        volume_ids = [volume_id for volume_id in volume_ids if volume_id]
        if not volume_ids:
            raise AssertionError(
                f"Could not capture a block-device volume for bastion {instance['InstanceId']}"
            )
        captured.extend(volume_ids)
    return tuple(sorted(set(captured)))


def _ec2_resources(app: str, *, sdk, volume_ids: Sequence[str] = ()) -> list[str]:
    ec2 = sdk.client("ec2")
    tag_filter = [{"Name": "tag:stelvio:app", "Values": [app]}]
    remaining: list[str] = []

    for operation, field, identifier, label in (
        ("describe_vpcs", "Vpcs", "VpcId", "VPC"),
        ("describe_security_groups", "SecurityGroups", "GroupId", "security group"),
        ("describe_addresses", "Addresses", "AllocationId", "elastic IP"),
        ("describe_subnets", "Subnets", "SubnetId", "subnet"),
        ("describe_route_tables", "RouteTables", "RouteTableId", "route table"),
        (
            "describe_internet_gateways",
            "InternetGateways",
            "InternetGatewayId",
            "internet gateway",
        ),
        (
            "describe_network_interfaces",
            "NetworkInterfaces",
            "NetworkInterfaceId",
            "network interface",
        ),
    ):
        response = getattr(ec2, operation)(Filters=tag_filter)
        remaining.extend(
            f"{label} {item.get(identifier, 'unknown')}" for item in response.get(field, [])
        )

    # Include exact bastion volumes captured before destroy, even when they have
    # no Stelvio application tag. DescribeVolumes can retain deleted volumes in
    # eventual-consistency windows, so its terminal state also counts as absent.
    tagged_volumes = ec2.describe_volumes(Filters=tag_filter).get("Volumes", [])
    remaining.extend(
        f"volume {volume.get('VolumeId', 'unknown')}"
        for volume in tagged_volumes
        if volume.get("State") != "deleted"
    )
    tagged_volume_ids = {volume.get("VolumeId") for volume in tagged_volumes}
    for volume_id in sorted(set(volume_ids) - tagged_volume_ids):
        try:
            volumes = ec2.describe_volumes(VolumeIds=[volume_id]).get("Volumes", [])
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "InvalidVolume.NotFound":
                continue
            raise
        remaining.extend(
            f"volume {volume.get('VolumeId', volume_id)}"
            for volume in volumes
            if volume.get("State") != "deleted"
        )

    # AWS can retain terminated instances in DescribeInstances results. Only
    # nonterminal instances indicate an incomplete destroy.
    for reservation in ec2.describe_instances(Filters=tag_filter).get("Reservations", []):
        for instance in reservation.get("Instances", []):
            state = instance.get("State", {}).get("Name", "unknown")
            if state != "terminated":
                remaining.append(f"EC2 instance {instance.get('InstanceId', 'unknown')} ({state})")

    for page in _pages(ec2, "describe_nat_gateways", Filter=tag_filter):
        remaining.extend(
            "NAT gateway "
            f"{gateway.get('NatGatewayId', 'unknown')} ({gateway.get('State', 'unknown')})"
            for gateway in page.get("NatGateways", [])
            if gateway.get("State") != "deleted"
        )
    return remaining


def _database_resources(prefix: str, *, sdk) -> list[str]:
    docdb = sdk.client("docdb")
    remaining = []
    for operation, field, identifier in (
        ("describe_db_clusters", "DBClusters", "DBClusterIdentifier"),
        ("describe_db_instances", "DBInstances", "DBInstanceIdentifier"),
        ("describe_db_subnet_groups", "DBSubnetGroups", "DBSubnetGroupName"),
        (
            "describe_db_cluster_parameter_groups",
            "DBClusterParameterGroups",
            "DBClusterParameterGroupName",
        ),
    ):
        for page in _pages(docdb, operation):
            remaining.extend(
                f"DocumentDB {identifier} {item.get(identifier, 'unknown')}"
                for item in page.get(field, [])
                if item.get(identifier, "").startswith(prefix)
            )

    return remaining


def _lambda_resources(prefix: str, *, sdk) -> list[str]:
    functions = sdk.client("lambda")
    remaining = []
    for page in _pages(functions, "list_functions"):
        remaining.extend(
            f"Lambda function {function.get('FunctionName', 'unknown')}"
            for function in page.get("Functions", [])
            if function.get("FunctionName", "").startswith(prefix)
        )

    return remaining


def _iam_resources(prefix: str, *, sdk) -> list[str]:
    iam = sdk.client("iam")
    remaining = []
    for operation, field, identifier, options in (
        ("list_roles", "Roles", "RoleName", {}),
        ("list_instance_profiles", "InstanceProfiles", "InstanceProfileName", {}),
        ("list_policies", "Policies", "PolicyName", {"Scope": "Local"}),
    ):
        for page in _pages(iam, operation, **options):
            remaining.extend(
                f"IAM {identifier} {resource.get(identifier, 'unknown')}"
                for resource in page.get(field, [])
                if resource.get(identifier, "").startswith(prefix)
            )

    return remaining


def _ssm_resources(prefix: str, *, sdk) -> list[str]:
    ssm = sdk.client("ssm")
    remaining = []
    for page in _pages(ssm, "list_documents", Filters=[{"Key": "Owner", "Values": ["Self"]}]):
        remaining.extend(
            f"SSM document {document.get('Name', 'unknown')}"
            for document in page.get("DocumentIdentifiers", [])
            if document.get("Name", "").startswith(prefix)
        )

    return remaining


def _pages(client, operation: str, **kwargs):
    yield from client.get_paginator(operation).paginate(**kwargs)
