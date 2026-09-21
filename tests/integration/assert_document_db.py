"""Boto3 read-back assertions for the DocumentDb component's deployed resources."""

from __future__ import annotations

import json
import time

from .assert_helpers import _assert_expected_tags, _boto3_session
from .assert_vpc import get_security_group

# A failed upgrade may leave its snapshot still being created; allow ten minutes.
_SNAPSHOT_READY_ATTEMPTS = 60
_SNAPSHOT_READY_INTERVAL = 10


def disable_document_db_deletion_protection(cluster_id: str) -> None:
    """Allow fixture teardown even when the protection-enabling deployment failed."""
    client = _boto3_session().client("docdb")
    client.modify_db_cluster(DBClusterIdentifier=cluster_id, DeletionProtection=False)


def assert_document_db_cluster(  # noqa: PLR0913
    cluster_id: str,
    *,
    port: int = 27017,
    storage_encrypted: bool = True,
    engine: str = "docdb",
    engine_version: str | None = None,
    master_username: str = "stelvio",
    subnet_group_name: str | None = None,
    parameter_group_name: str | None = None,
    deletion_protection: bool = False,
    backup_retention_period: int = 7,
) -> dict:
    """Assert a DocumentDB cluster's core properties. Returns the cluster description."""
    client = _boto3_session().client("docdb")
    cluster = client.describe_db_clusters(DBClusterIdentifier=cluster_id)["DBClusters"][0]
    assert cluster["Port"] == port
    assert cluster["StorageEncrypted"] is storage_encrypted
    assert cluster["Engine"] == engine
    assert cluster["MasterUsername"] == master_username
    assert cluster["DeletionProtection"] is deletion_protection
    assert cluster["BackupRetentionPeriod"] == backup_retention_period
    if engine_version is not None:
        assert cluster["EngineVersion"] == engine_version
    if subnet_group_name is not None:
        assert cluster["DBSubnetGroup"] == subnet_group_name
    if parameter_group_name is not None:
        assert cluster["DBClusterParameterGroup"] == parameter_group_name
    return cluster


def assert_document_db_subnet_group(subnet_group_name: str, *, subnet_ids: list[str]) -> None:
    """Assert the subnet group contains exactly the given subnet ids."""
    client = _boto3_session().client("docdb")
    group = client.describe_db_subnet_groups(DBSubnetGroupName=subnet_group_name)[
        "DBSubnetGroups"
    ][0]
    actual = {s["SubnetIdentifier"] for s in group["Subnets"]}
    assert actual == set(subnet_ids)


def assert_document_db_instances(  # noqa: PLR0913
    instance_ids: list[str],
    *,
    cluster_id: str,
    instance_count: int,
    publicly_accessible: bool = False,
    instance_class: str | None = None,
    engine_version: str | None = None,
    tags: dict[str, str] | None = None,
) -> None:
    """Assert exact AWS cluster membership, instance ownership, and properties."""
    assert len(instance_ids) == instance_count
    assert len(set(instance_ids)) == instance_count
    client = _boto3_session().client("docdb")
    clusters = client.describe_db_clusters(DBClusterIdentifier=cluster_id)["DBClusters"]
    assert len(clusters) == 1
    cluster = clusters[0]
    assert cluster["DBClusterIdentifier"] == cluster_id
    members = cluster["DBClusterMembers"]
    assert len(members) == instance_count
    assert {member["DBInstanceIdentifier"] for member in members} == set(instance_ids)
    for instance_id in instance_ids:
        instances = client.describe_db_instances(DBInstanceIdentifier=instance_id)["DBInstances"]
        assert len(instances) == 1
        instance = instances[0]
        assert instance["DBInstanceIdentifier"] == instance_id
        assert instance["DBClusterIdentifier"] == cluster_id
        assert instance["PubliclyAccessible"] is publicly_accessible
        if instance_class is not None:
            assert instance["DBInstanceClass"] == instance_class
        if engine_version is not None:
            assert instance["EngineVersion"] == engine_version
            assert instance["DBInstanceStatus"] == "available"
        if tags is not None:
            assert_document_db_tags(instance["DBInstanceArn"], tags)


def assert_document_db_tls_parameter(
    parameter_group_name: str, *, family: str | None = None
) -> None:
    """Assert the cluster parameter group requires TLS."""
    client = _boto3_session().client("docdb")
    if family is not None:
        group = client.describe_db_cluster_parameter_groups(
            DBClusterParameterGroupName=parameter_group_name
        )["DBClusterParameterGroups"][0]
        assert group["DBParameterGroupFamily"] == family
    paginator = client.get_paginator("describe_db_cluster_parameters")
    parameters = []
    for page in paginator.paginate(DBClusterParameterGroupName=parameter_group_name):
        parameters.extend(page["Parameters"])
    tls = next(p["ParameterValue"] for p in parameters if p["ParameterName"] == "tls")
    assert tls == "enabled"


def delete_document_db_upgrade_snapshots(cluster_id: str) -> None:
    """Remove only pre-upgrade snapshots belonging to this disposable test cluster."""
    client = _boto3_session().client("docdb")
    paginator = client.get_paginator("describe_db_cluster_snapshots")
    for page in paginator.paginate(DBClusterIdentifier=cluster_id, SnapshotType="manual"):
        for snapshot in page["DBClusterSnapshots"]:
            snapshot_id = snapshot["DBClusterSnapshotIdentifier"]
            if snapshot["DBClusterIdentifier"] == cluster_id and snapshot_id.startswith(
                f"preupgrade-{cluster_id}-"
            ):
                for _ in range(_SNAPSHOT_READY_ATTEMPTS):
                    current = client.describe_db_cluster_snapshots(
                        DBClusterSnapshotIdentifier=snapshot_id
                    )["DBClusterSnapshots"][0]
                    if current["Status"] == "available":
                        break
                    time.sleep(_SNAPSHOT_READY_INTERVAL)
                else:
                    raise TimeoutError(f"Upgrade snapshot {snapshot_id} is not ready for cleanup")
                client.delete_db_cluster_snapshot(DBClusterSnapshotIdentifier=snapshot_id)


def assert_document_db_secret_exists(secret_arn: str) -> None:
    """Assert the AWS-managed master-user secret exists."""
    client = _boto3_session().client("secretsmanager")
    secret = client.describe_secret(SecretId=secret_arn)
    assert secret["ARN"] == secret_arn


def assert_document_db_secret_rotation(
    secret_arn: str, *, enabled: bool, automatically_after_days: int | None = None
) -> None:
    """Assert the managed DocumentDB secret's automatic rotation configuration."""
    client = _boto3_session().client("secretsmanager")
    secret = client.describe_secret(SecretId=secret_arn)
    assert secret.get("RotationEnabled", False) is enabled
    if automatically_after_days is not None:
        assert secret["RotationRules"]["AutomaticallyAfterDays"] == automatically_after_days


def document_db_secret(secret_arn: str) -> dict:
    """Return the managed master-user secret JSON."""
    client = _boto3_session().client("secretsmanager")
    return json.loads(client.get_secret_value(SecretId=secret_arn)["SecretString"])


def assert_document_db_tags(arn: str, expected_tags: dict[str, str]) -> None:
    """Assert a DocumentDB cluster or instance has the expected tag values."""
    client = _boto3_session().client("docdb")
    response = client.list_tags_for_resource(ResourceName=arn)
    tags = {t["Key"]: t["Value"] for t in response.get("TagList", [])}
    _assert_expected_tags(tags, expected_tags, resource_label=f"DocumentDB {arn}")


def assert_security_group_ingress(
    security_group_id: str,
    *,
    source_security_group_id: str,
    port: int,
    protocol: str = "tcp",
) -> None:
    """Assert the SG's ingress list is exactly one rule from the source SG on this port."""
    sg = get_security_group(security_group_id)
    assert [
        {
            "FromPort": p.get("FromPort"),
            "ToPort": p.get("ToPort"),
            "IpProtocol": p["IpProtocol"],
            "UserIdGroupPairs": [pair["GroupId"] for pair in p.get("UserIdGroupPairs", [])],
            "IpRanges": p.get("IpRanges", []),
            "Ipv6Ranges": p.get("Ipv6Ranges", []),
            "PrefixListIds": p.get("PrefixListIds", []),
        }
        for p in sg["IpPermissions"]
    ] == [
        {
            "FromPort": port,
            "ToPort": port,
            "IpProtocol": protocol,
            "UserIdGroupPairs": [source_security_group_id],
            "IpRanges": [],
            "Ipv6Ranges": [],
            "PrefixListIds": [],
        }
    ]
