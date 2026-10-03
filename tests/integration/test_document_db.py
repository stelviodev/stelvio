from typing import Literal

import pytest

from stelvio.aws.document_db import DocumentDb
from stelvio.aws.function import Function
from stelvio.aws.vpc import NatConfig, Vpc

from .assert_document_db import (
    assert_document_db_cluster,
    assert_document_db_instances,
    assert_document_db_secret_exists,
    assert_document_db_secret_rotation,
    assert_document_db_subnet_group,
    assert_document_db_tags,
    assert_document_db_tls_parameter,
    assert_security_group_ingress,
    delete_document_db_upgrade_snapshots,
    disable_document_db_deletion_protection,
)
from .assert_helpers import (
    _boto3_session,
    assert_lambda_function,
    assert_lambda_role_permissions,
    get_lambda_vpc_config,
    invoke_lambda,
)
from .assert_vpc import get_app_security_group, get_security_group
from .export_helpers import export_document_db, export_function, export_vpc

pytestmark = pytest.mark.integration_vpc


def _regional_az_count(region: str) -> int:
    """How many AZs the test VPC should span.

    Graviton classes such as db.t4g.medium are often out of capacity in every AZ
    but one. Vpc(az=2) takes the first two, and CreateDBInstance then fails with
    InvalidVPCNetworkStateFault. A subnet in every regional AZ lets AWS place the
    instance where capacity exists.
    """
    zones = (
        _boto3_session(region)
        .client("ec2")
        .describe_availability_zones(
            Filters=[
                {"Name": "state", "Values": ["available"]},
                {"Name": "zone-type", "Values": ["availability-zone"]},
            ]
        )["AvailabilityZones"]
    )
    return len(zones)


def _deploy_and_assert_secret_rotation(  #  noqa: PLR0913
    stelvio_env,
    infra,
    *,
    cluster_id: str,
    secret_arn: str,
    enabled: bool,
    days: int | None,
    last_rotated_date,
) -> None:
    outputs = stelvio_env.deploy(infra)
    assert outputs["document_db_todos_cluster_id"] == cluster_id
    rotated = assert_document_db_secret_rotation(
        secret_arn, enabled=enabled, automatically_after_days=days
    )
    assert rotated == last_rotated_date


def test_document_db_default_and_rotation(stelvio_env):  # noqa: PLR0915
    deletion_protection = None
    backup_retention_period = None
    # None = omit the kwarg (seven-day default); False/int = pass explicitly.
    secret_rotation: int | Literal[False] | None = False
    cluster_identifier = None
    az_count = _regional_az_count(stelvio_env.aws_region)

    def infra():
        vpc = Vpc("net", az=az_count)
        opts: dict = {}
        if secret_rotation is not None:
            opts["secret_rotation"] = secret_rotation
        if deletion_protection is not None:
            opts["deletion_protection"] = deletion_protection
        if backup_retention_period is not None:
            opts["backup_retention_period"] = backup_retention_period
        customize = (
            {"cluster": {"cluster_identifier": cluster_identifier}}
            if cluster_identifier is not None
            else None
        )
        db = DocumentDb(
            "todos",
            vpc=vpc,
            tags={"Team": "platform"},
            customize=customize,
            **opts,
        )
        export_vpc(vpc)
        export_document_db(db)

    # Create with rotation disabled.
    first = stelvio_env.deploy(infra)
    first_cluster = assert_document_db_cluster(first["document_db_todos_cluster_id"])
    first_secret_arn = first_cluster["MasterUserSecret"]["SecretArn"]
    # RDS's initial managed-password rotation sets this before we can disable it.
    first_rotated = assert_document_db_secret_rotation(first_secret_arn, enabled=False)

    # Omit the override entirely so the seven-day default applies on the same cluster.
    secret_rotation = None
    outputs = stelvio_env.deploy(infra)
    assert outputs["document_db_todos_cluster_id"] == first["document_db_todos_cluster_id"]

    expected_tags = {
        "stelvio:app": f"stlv-{stelvio_env.run_id}",
        "stelvio:env": "test",
        "Team": "platform",
    }
    cluster = assert_document_db_cluster(
        outputs["document_db_todos_cluster_id"],
        port=27017,
        storage_encrypted=True,
        engine="docdb",
        engine_version="8.0.0",
        master_username="stelvio",
        deletion_protection=False,
        backup_retention_period=7,
        subnet_group_name=outputs["document_db_todos_subnet_group_name"],
        parameter_group_name=outputs["document_db_todos_parameter_group_name"],
        identifier_prefix=f"stlv-{stelvio_env.run_id}-test-todos-",
    )
    assert cluster["DBClusterArn"] == outputs["document_db_todos_cluster_arn"]
    assert cluster["Endpoint"] == outputs["document_db_todos_endpoint"]
    assert cluster["ReaderEndpoint"] == outputs["document_db_todos_reader_endpoint"]
    assert cluster["Port"] == outputs["document_db_todos_port"]
    assert {g["VpcSecurityGroupId"] for g in cluster["VpcSecurityGroups"]} == {
        outputs["document_db_todos_security_group_id"]
    }
    assert_document_db_subnet_group(
        outputs["document_db_todos_subnet_group_name"],
        subnet_ids=outputs["vpc_net_isolated_subnet_ids"],
    )
    instance_ids = assert_document_db_instances(
        outputs["document_db_todos_cluster_id"],
        instance_count=1,
        publicly_accessible=False,
        instance_class="db.t4g.medium",
        engine_version="8.0.0",
        tags=expected_tags,
        identifier_prefix=f"stlv-{stelvio_env.run_id}-test-todos-",
    )
    assert_document_db_tls_parameter(
        outputs["document_db_todos_parameter_group_name"], family="docdb8.0"
    )
    assert_document_db_secret_exists(cluster["MasterUserSecret"]["SecretArn"])
    rotated = assert_document_db_secret_rotation(
        cluster["MasterUserSecret"]["SecretArn"],
        enabled=True,
        automatically_after_days=7,
    )
    assert rotated == first_rotated
    assert_security_group_ingress(
        outputs["document_db_todos_security_group_id"],
        source_security_group_id=get_app_security_group(outputs["vpc_net_id"])["GroupId"],
        port=27017,
    )
    cluster_sg = get_security_group(outputs["document_db_todos_security_group_id"])
    assert cluster_sg["IpPermissionsEgress"] == []
    assert_document_db_tags(cluster["DBClusterArn"], expected_tags)

    redeployed = stelvio_env.deploy(infra)
    for field in ("cluster_id", "cluster_arn", "endpoint", "reader_endpoint"):
        key = f"document_db_todos_{field}"
        assert redeployed[key] == outputs[key]
    redeployed_ids = assert_document_db_instances(
        redeployed["document_db_todos_cluster_id"], instance_count=1
    )
    assert redeployed_ids == instance_ids

    for requested_rotation, enabled, days in (
        (30, True, 30),
        (False, False, None),
        (7, True, 7),
    ):
        secret_rotation = requested_rotation
        _deploy_and_assert_secret_rotation(
            stelvio_env,
            infra,
            cluster_id=outputs["document_db_todos_cluster_id"],
            secret_arn=cluster["MasterUserSecret"]["SecretArn"],
            enabled=enabled,
            days=days,
            last_rotated_date=first_rotated,
        )

    try:
        deletion_protection = True
        backup_retention_period = 14
        protected = stelvio_env.deploy(infra)
        assert protected["document_db_todos_cluster_id"] == outputs["document_db_todos_cluster_id"]
        assert_document_db_cluster(
            protected["document_db_todos_cluster_id"],
            deletion_protection=True,
            backup_retention_period=14,
        )

        deletion_protection = False
        unprotected = stelvio_env.deploy(infra)
        assert_document_db_cluster(
            unprotected["document_db_todos_cluster_id"],
            deletion_protection=False,
            backup_retention_period=14,
        )
    finally:
        # A failed update may already have enabled protection. Use the last known
        # cluster ID and the AWS API so cleanup does not depend on another deploy.
        disable_document_db_deletion_protection(outputs["document_db_todos_cluster_id"])

    # Force replacement with rotation disabled and verify the new secret.
    secret_rotation = False
    cluster_identifier = f"stlv-{stelvio_env.run_id[:8]}-second"
    second = stelvio_env.deploy(infra)
    second_cluster = assert_document_db_cluster(second["document_db_todos_cluster_id"])
    second_secret_arn = second_cluster["MasterUserSecret"]["SecretArn"]
    assert second["document_db_todos_cluster_id"] != outputs["document_db_todos_cluster_id"]
    assert_document_db_secret_rotation(second_secret_arn, enabled=False)


def test_document_db_link_and_upgrade(stelvio_env, project_dir):  # noqa: PLR0915
    engine, instance_class = "5.0", "t4g.medium"
    az_count = _regional_az_count(stelvio_env.aws_region)

    def infra():
        vpc = Vpc("net", az=az_count, nat=NatConfig(type="managed", single=True))
        db = DocumentDb(
            "todos",
            vpc=vpc,
            instances=2,
            engine=engine,
            instance_class=instance_class,
            customize={
                "cluster": {
                    "apply_immediately": True,
                    "allow_major_version_upgrade": True,
                },
                "instance": {"apply_immediately": True},
            },
        )
        fn = Function(
            "client",
            handler="handlers/docdb_client::main.main",
            vpc=vpc,
            links=[db],
            requirements=["pymongo"],
        )
        export_vpc(vpc)
        export_document_db(db)
        export_function(fn)

    original = stelvio_env.deploy(infra)
    cluster_id = original["document_db_todos_cluster_id"]
    try:
        cluster = assert_document_db_cluster(
            cluster_id,
            port=27017,
            storage_encrypted=True,
            engine="docdb",
            engine_version="5.0.0",
            master_username="stelvio",
            subnet_group_name=original["document_db_todos_subnet_group_name"],
            parameter_group_name=original["document_db_todos_parameter_group_name"],
        )
        assert cluster["DBClusterArn"] == original["document_db_todos_cluster_arn"]
        assert cluster["Endpoint"] == original["document_db_todos_endpoint"]
        assert cluster["ReaderEndpoint"] == original["document_db_todos_reader_endpoint"]
        assert cluster["Port"] == original["document_db_todos_port"]
        secret_arn = cluster["MasterUserSecret"]["SecretArn"]
        assert_document_db_secret_exists(secret_arn)
        original_ids = assert_document_db_instances(
            cluster_id,
            instance_count=2,
            publicly_accessible=False,
            instance_class="db.t4g.medium",
            engine_version="5.0.0",
        )
        assert_document_db_tls_parameter(
            original["document_db_todos_parameter_group_name"], family="docdb5.0"
        )

        expected_env = {
            "STLV_TODOS_HOST": cluster["Endpoint"],
            "STLV_TODOS_READER_HOST": cluster["ReaderEndpoint"],
            "STLV_TODOS_PORT": str(cluster["Port"]),
            "STLV_TODOS_USERNAME": cluster["MasterUsername"],
            "STLV_TODOS_SECRET_ARN": secret_arn,
            "STLV_TODOS_REPLICA_SET": "rs0",
            "STLV_TODOS_CA_FILE": "stlv_docdb_ca.pem",
            "STLV_TODOS_CONNECTION_URI": (
                f"mongodb://{cluster['Endpoint']}:{cluster['Port']}/"
                "?tls=true&tlsCAFile=stlv_docdb_ca.pem&replicaSet=rs0&retryWrites=false"
            ),
        }
        lambda_env = assert_lambda_function(original["function_client_arn"])
        assert {k: v for k, v in lambda_env.items() if k.startswith("STLV_")} == expected_env
        assert_lambda_role_permissions(
            original["function_client_role_name"],
            expected_actions=["secretsmanager:GetSecretValue"],
            expected_resources=[secret_arn],
        )

        app_sg_id = get_app_security_group(original["vpc_net_id"])["GroupId"]
        vpc_config = get_lambda_vpc_config(original["function_client_arn"])
        assert set(vpc_config["SubnetIds"]) == set(original["vpc_net_private_subnet_ids"])
        assert vpc_config["SecurityGroupIds"] == [app_sg_id]
        assert_security_group_ingress(
            original["document_db_todos_security_group_id"],
            source_security_group_id=app_sg_id,
            port=27017,
        )

        assert invoke_lambda(original["function_client_arn"]) == {
            "username": "stelvio",
            "mongo_ok": True,
        }
        assert invoke_lambda(
            original["function_client_arn"],
            {"operation": "write", "document": {"_id": "before-upgrade", "value": 42}},
        ) == {"document": {"_id": "before-upgrade", "value": 42}}

        # Resize in a separate update: the engine change must not race the resize.
        instance_class = "r6g.large"
        resized = stelvio_env.deploy(infra)
        assert resized["document_db_todos_cluster_id"] == cluster_id
        assert_document_db_cluster(cluster_id, engine_version="5.0.0")
        resized_ids = assert_document_db_instances(
            cluster_id,
            instance_count=2,
            instance_class="db.r6g.large",
            engine_version="5.0.0",
        )
        assert resized_ids == original_ids

        engine = "8.0"
        upgraded = stelvio_env.deploy(infra)
        # An upgrade must preserve physical resources, not quietly replace the database.
        for field in ("cluster_id", "cluster_arn", "endpoint", "reader_endpoint"):
            key = f"document_db_todos_{field}"
            assert upgraded[key] == original[key]
        parameter_group = upgraded["document_db_todos_parameter_group_name"]
        assert parameter_group != original["document_db_todos_parameter_group_name"]
        upgraded_cluster = assert_document_db_cluster(
            cluster_id, engine_version="8.0.0", parameter_group_name=parameter_group
        )
        assert upgraded_cluster["Status"] == "available"
        assert [
            m["DBClusterParameterGroupStatus"] for m in upgraded_cluster["DBClusterMembers"]
        ] == ["in-sync", "in-sync"]
        upgraded_ids = assert_document_db_instances(
            cluster_id,
            instance_count=2,
            instance_class="db.r6g.large",
            engine_version="8.0.0",
        )
        assert upgraded_ids == original_ids
        assert_document_db_tls_parameter(parameter_group, family="docdb8.0")
        assert invoke_lambda(upgraded["function_client_arn"]) == {
            "username": "stelvio",
            "mongo_ok": True,
        }
        assert invoke_lambda(
            upgraded["function_client_arn"], {"operation": "read", "id": "before-upgrade"}
        ) == {"document": {"_id": "before-upgrade", "value": 42}}
        assert invoke_lambda(
            upgraded["function_client_arn"],
            {"operation": "write", "document": {"_id": "after-upgrade", "value": 84}},
        ) == {"document": {"_id": "after-upgrade", "value": 84}}
        assert invoke_lambda(
            upgraded["function_client_arn"], {"operation": "read", "id": "after-upgrade"}
        ) == {"document": {"_id": "after-upgrade", "value": 84}}
    finally:
        # AWS keeps its pre-upgrade snapshot even after the test stack is destroyed.
        delete_document_db_upgrade_snapshots(cluster_id)
