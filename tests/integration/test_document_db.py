from urllib.parse import urlsplit

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
    assert_lambda_function,
    assert_lambda_role_permissions,
    get_lambda_vpc_config,
    invoke_lambda,
)
from .assert_vpc import get_app_security_group, get_security_group
from .export_helpers import export_document_db, export_function, export_vpc

pytestmark = pytest.mark.integration_docdb


def _deploy_and_assert_secret_rotation(  #  noqa: PLR0913
    stelvio_env,
    infra,
    *,
    cluster_id: str,
    secret_arn: str,
    enabled: bool,
    days: int | None,
) -> None:
    outputs = stelvio_env.deploy(infra)
    assert outputs["document_db_todos_cluster_id"] == cluster_id
    assert_document_db_secret_rotation(secret_arn, enabled=enabled, automatically_after_days=days)


def test_document_db_default(stelvio_env):
    deletion_protection = None
    backup_retention_period = None
    secret_rotation = 7

    def infra():
        vpc = Vpc("net", az=2)
        opts = {}
        if deletion_protection is not None:
            opts["deletion_protection"] = deletion_protection
        if backup_retention_period is not None:
            opts["backup_retention_period"] = backup_retention_period
        opts["secret_rotation"] = secret_rotation
        db = DocumentDb("todos", vpc=vpc, tags={"Team": "platform"}, **opts)
        export_vpc(vpc)
        export_document_db(db)

    outputs = stelvio_env.deploy(infra)

    expected_tags = {
        "stelvio:app": f"stlv-{stelvio_env.run_id}",
        "stelvio:env": "test",
        "Team": "platform",
    }
    cluster = assert_document_db_cluster(
        outputs["document_db_todos_cluster_id"],
        port=27017,
        storage_encrypted=True,
        engine_version="8.0.0",
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
    assert_document_db_instances(
        outputs["document_db_todos_instance_ids"],
        cluster_id=outputs["document_db_todos_cluster_id"],
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
    assert_document_db_secret_rotation(
        cluster["MasterUserSecret"]["SecretArn"], enabled=True, automatically_after_days=7
    )
    assert_security_group_ingress(
        outputs["document_db_todos_security_group_id"],
        source_security_group_id=get_app_security_group(outputs["vpc_net_id"])["GroupId"],
        port=27017,
    )
    cluster_sg = get_security_group(outputs["document_db_todos_security_group_id"])
    assert cluster_sg["IpPermissionsEgress"] == []
    assert_document_db_tags(cluster["DBClusterArn"], expected_tags)

    redeployed = stelvio_env.deploy(infra)
    for field in ("cluster_id", "cluster_arn", "endpoint", "reader_endpoint", "instance_ids"):
        key = f"document_db_todos_{field}"
        assert redeployed[key] == outputs[key]

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


def test_document_db_rotation_disabled_on_first_create_and_replacement(stelvio_env):
    cluster_identifier = f"stlv-{stelvio_env.run_id[:8]}-first"

    def infra():
        vpc = Vpc("net", az=2)
        db = DocumentDb(
            "todos",
            vpc=vpc,
            secret_rotation=False,
            customize={"cluster": {"cluster_identifier": cluster_identifier}},
        )
        export_document_db(db)

    first = stelvio_env.deploy(infra)
    first_cluster = assert_document_db_cluster(first["document_db_todos_cluster_id"])
    first_secret_arn = first_cluster["MasterUserSecret"]["SecretArn"]
    assert_document_db_secret_rotation(first_secret_arn, enabled=False)

    cluster_identifier = f"stlv-{stelvio_env.run_id[:8]}-second"
    second = stelvio_env.deploy(infra)
    second_cluster = assert_document_db_cluster(second["document_db_todos_cluster_id"])
    second_secret_arn = second_cluster["MasterUserSecret"]["SecretArn"]
    assert second["document_db_todos_cluster_id"] != first["document_db_todos_cluster_id"]
    assert_document_db_secret_rotation(second_secret_arn, enabled=False)


def test_document_db_linked_function(stelvio_env, project_dir):
    def infra():
        vpc = Vpc("net", az=2, nat=NatConfig(type="managed", single=True))
        db = DocumentDb("todos", vpc=vpc, instances=2)
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

    outputs = stelvio_env.deploy(infra)

    cluster = assert_document_db_cluster(
        outputs["document_db_todos_cluster_id"],
        port=27017,
        storage_encrypted=True,
        engine_version="8.0.0",
        subnet_group_name=outputs["document_db_todos_subnet_group_name"],
        parameter_group_name=outputs["document_db_todos_parameter_group_name"],
    )
    assert cluster["DBClusterArn"] == outputs["document_db_todos_cluster_arn"]
    assert cluster["Endpoint"] == outputs["document_db_todos_endpoint"]
    assert cluster["ReaderEndpoint"] == outputs["document_db_todos_reader_endpoint"]
    assert cluster["Port"] == outputs["document_db_todos_port"]
    secret_arn = cluster["MasterUserSecret"]["SecretArn"]
    assert_document_db_secret_exists(secret_arn)
    assert_document_db_instances(
        outputs["document_db_todos_instance_ids"],
        cluster_id=outputs["document_db_todos_cluster_id"],
        instance_count=2,
        publicly_accessible=False,
        instance_class="db.t4g.medium",
        engine_version="8.0.0",
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
    lambda_env = assert_lambda_function(outputs["function_client_arn"], environment=expected_env)
    assert {k: v for k, v in lambda_env.items() if k.startswith("STLV_")} == expected_env
    assert "STLV_TODOS_PASSWORD" not in lambda_env
    parsed_uri = urlsplit(lambda_env["STLV_TODOS_CONNECTION_URI"])
    assert parsed_uri.username is None
    assert parsed_uri.password is None
    assert_lambda_role_permissions(
        outputs["function_client_role_name"],
        expected_actions=["secretsmanager:GetSecretValue"],
        expected_resources=[secret_arn],
    )

    app_sg_id = get_app_security_group(outputs["vpc_net_id"])["GroupId"]
    vpc_config = get_lambda_vpc_config(outputs["function_client_arn"])
    assert set(vpc_config["SubnetIds"]) == set(outputs["vpc_net_private_subnet_ids"])
    assert vpc_config["SecurityGroupIds"] == [app_sg_id]
    assert_security_group_ingress(
        outputs["document_db_todos_security_group_id"],
        source_security_group_id=app_sg_id,
        port=27017,
    )

    assert invoke_lambda(outputs["function_client_arn"]) == {
        "username": "stelvio",
        "mongo_ok": True,
    }


def test_document_db_major_upgrade(stelvio_env, project_dir):
    engine, instance_class = "5.0", "t4g.medium"

    def infra():
        vpc = Vpc("net", az=2, nat=NatConfig(type="managed", single=True))
        db = DocumentDb(
            "todos",
            vpc=vpc,
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
        export_document_db(db)
        export_function(fn)

    original = stelvio_env.deploy(infra)
    cluster_id = original["document_db_todos_cluster_id"]
    try:
        assert invoke_lambda(
            original["function_client_arn"],
            {"operation": "write", "document": {"_id": "before-upgrade", "value": 42}},
        ) == {"document": {"_id": "before-upgrade", "value": 42}}
        # Resize in a separate update: the engine change must not race the resize.
        instance_class = "r6g.large"
        resized = stelvio_env.deploy(infra)
        assert resized["document_db_todos_cluster_id"] == cluster_id
        assert (
            resized["document_db_todos_instance_ids"] == original["document_db_todos_instance_ids"]
        )
        assert_document_db_cluster(cluster_id, engine_version="5.0.0")
        assert_document_db_instances(
            resized["document_db_todos_instance_ids"],
            cluster_id=cluster_id,
            instance_count=1,
            instance_class="db.r6g.large",
            engine_version="5.0.0",
        )

        engine = "8.0"
        upgraded = stelvio_env.deploy(infra)
        # An upgrade must preserve physical resources, not quietly replace the database.
        for field in ("cluster_id", "cluster_arn", "endpoint", "reader_endpoint", "instance_ids"):
            key = f"document_db_todos_{field}"
            assert upgraded[key] == original[key]
        parameter_group = upgraded["document_db_todos_parameter_group_name"]
        assert parameter_group != original["document_db_todos_parameter_group_name"]
        cluster = assert_document_db_cluster(
            cluster_id, engine_version="8.0.0", parameter_group_name=parameter_group
        )
        assert cluster["Status"] == "available"
        assert [m["DBClusterParameterGroupStatus"] for m in cluster["DBClusterMembers"]] == [
            "in-sync"
        ]
        assert_document_db_instances(
            upgraded["document_db_todos_instance_ids"],
            cluster_id=cluster_id,
            instance_count=1,
            instance_class="db.r6g.large",
            engine_version="8.0.0",
        )
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
