import json
import os
import re
import time
from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal
from urllib.error import URLError
from urllib.parse import quote_plus

import pulumi
from pulumi import FileAsset
from pulumi_aws.docdb import ClusterArgs
from pytest import mark, param, raises

from stelvio.aws.document_db import DocumentDb, DocumentDbConfig, DocumentDbConfigDict
from stelvio.aws.function import Function
from stelvio.aws.permission import AwsPermission
from stelvio.aws.vpc import Vpc, VpcAttachment
from stelvio.context import _ContextStore
from tests.aws.pulumi_mocks import (
    ACCOUNT_ID,
    DEFAULT_REGION,
    TP,
    R,
    tid,
    tn,
)
from tests.test_utils import assert_config_dict_matches_dataclass

from .conftest import FAKE_DOCDB_CA_PEM, FakeUrlopenResponse

DB_NAME = "todos"
VPC_NAME = "main_vpc"
APP_SG_NAME = f"{VPC_NAME}-app-sg"
APP_SG_ID = tid(TP + APP_SG_NAME)
CLUSTER_SG_NAME = f"{DB_NAME}-sg"
CLUSTER_SG_ID = tid(TP + CLUSTER_SG_NAME)
CLUSTER_ID = tid(TP + DB_NAME)
DOCDB_HOST = f"{CLUSTER_ID}.cluster-{DEFAULT_REGION}.docdb.amazonaws.com"
DOCDB_SECRET_ARN = f"arn:aws:secretsmanager:{DEFAULT_REGION}:{ACCOUNT_ID}:secret:{CLUSTER_ID}"
PRIVATE_SUBNET_IDS = [tid(TP + f"{VPC_NAME}-private-subnet-{az}") for az in "ab"]
DOCDB_CA_ZIP_PATH = "stlv_docdb_ca.pem"
CA_BUNDLE_URL = "https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem"
CA_CACHE_RELATIVE_PATH = Path(".stelvio") / "aws" / "documentdb" / "global-bundle.pem"
CA_CACHE_TTL_SECONDS = 24 * 60 * 60
_OLD_CA_PEM = b"-----BEGIN CERTIFICATE-----\nold\n-----END CERTIFICATE-----\n"
SIMPLE_HANDLER = "functions/simple.handler"
# Function in VPC with a DocumentDb link: basic + VPC access + the function policy
FUNCTION_VPC_LINKED_COUNTS = {
    R.FUNCTION: 1,
    R.ROLE: 1,
    R.POLICY: 1,
    R.ROLE_POLICY_ATTACHMENT: 3,
}

VPC_AZ2_COUNTS = {
    R.VPC: 1,
    R.INTERNET_GATEWAY: 1,
    R.DEFAULT_SECURITY_GROUP: 1,
    R.SUBNET: 6,
    R.ROUTE_TABLE: 6,
    R.ROUTE_TABLE_ASSOCIATION: 6,
}
VPC_AZ3_COUNTS = VPC_AZ2_COUNTS | {
    R.SUBNET: 9,
    R.ROUTE_TABLE: 9,
    R.ROUTE_TABLE_ASSOCIATION: 9,
}
APP_SG_COUNTS = {R.SECURITY_GROUP: 1, R.SECURITY_GROUP_EGRESS_RULE: 1}
DOCDB_COUNTS = {
    R.DOCDB_SUBNET_GROUP: 1,
    R.DOCDB_PARAMETER_GROUP: 1,
    R.SECURITY_GROUP: 1,
    R.SECURITY_GROUP_INGRESS_RULE: 1,
    R.DOCDB_CLUSTER: 1,
    R.DOCDB_INSTANCE: 1,
    R.SECRET_ROTATION: 1,
}


def _counts(*parts: dict[R, int]) -> dict[R, int]:
    total: Counter[R] = Counter()
    for part in parts:
        total.update(part)
    return dict(total)


def _set_app_context(app: str = "test", env: str = "test", customize=None) -> None:
    ctx = replace(_ContextStore.get(), name=app, env=env, customize=customize or {})
    _ContextStore.clear()
    _ContextStore.set(ctx)


@mark.parametrize(
    ("opts", "error_type", "error_message"),
    [
        param(
            {"instances": 0},
            ValueError,
            "`instances` must be between 1 and 16, got 0",
            id="instances-zero",
        ),
        param(
            {"instances": True},
            TypeError,
            "`instances` must be an int, got bool",
            id="instances-bool",
        ),
        param(
            {"instance_class": 123},
            TypeError,
            "`instance_class` must be a str, got int",
            id="instance-class-int",
        ),
        param(
            {"instance_class": "t4g"},
            ValueError,
            "`instance_class` must be family.size (e.g. 't4g.medium' or 'db.t4g.medium'), "
            "got 't4g'",
            id="instance-class-no-size",
        ),
        param(
            {"instance_class": ""},
            ValueError,
            "`instance_class` must be a non-empty string, got ''",
            id="instance-class-empty",
        ),
        param(
            {"engine": "4.0"},
            ValueError,
            "`engine` must be '5.0' or '8.0', got '4.0'",
            id="engine-4",
        ),
        param(
            {"engine": 5.0},
            TypeError,
            "`engine` must be a str, got float",
            id="engine-float",
        ),
        param(
            {"deletion_protection": 1},
            TypeError,
            "`deletion_protection` must be a bool, got int",
            id="deletion-protection-int",
        ),
        param(
            {"backup_retention_period": 0},
            ValueError,
            "`backup_retention_period` must be between 1 and 35, got 0",
            id="backup-retention-zero",
        ),
        param(
            {"backup_retention_period": True},
            TypeError,
            "`backup_retention_period` must be an int, got bool",
            id="backup-retention-bool",
        ),
        param(
            {"secret_rotation": 0},
            ValueError,
            "`secret_rotation` must be between 1 and 1000, or False, got 0",
            id="secret-rotation-zero",
        ),
        param(
            {"secret_rotation": True},
            TypeError,
            "`secret_rotation` must be False or an int, got bool",
            id="secret-rotation-true",
        ),
    ],
)
def test_document_db_raises_when_kwargs_invalid(opts, error_type, error_message):
    vpc = Vpc(VPC_NAME)
    with raises(error_type, match=f"^{re.escape(error_message)}$"):
        DocumentDb(DB_NAME, vpc=vpc, **opts)


def test_document_db_raises_when_vpc_is_attachment():
    vpc = Vpc(VPC_NAME)
    with raises(TypeError, match=re.escape("`vpc` must be a Vpc instance, got VpcAttachment")):
        DocumentDb(DB_NAME, vpc=VpcAttachment(vpc=vpc))


@mark.parametrize(
    "name",
    [
        param("my_db", id="underscore"),
        param("Todos", id="uppercase"),
        param("1todos", id="leading-digit"),
        param("todos-", id="trailing-hyphen"),
        param("to--dos", id="double-hyphen"),
    ],
)
def test_document_db_raises_when_name_invalid(name):
    vpc = Vpc(VPC_NAME)
    error = (
        "`name` must contain only lowercase letters, digits and single hyphens, start "
        "with a letter and not end with a hyphen (AWS DocumentDB identifier rules), "
        f"got {name!r}"
    )
    with raises(ValueError, match=re.escape(error)):
        DocumentDb(name, vpc=vpc)


@mark.parametrize(
    ("app", "env", "name", "instances", "cluster_prefix", "instance_prefix"),
    [
        param(
            "test",
            "test",
            "a" * 26,
            1,
            "test-test-aaaaaaaaaaaaaaaaaaaaaaaaaa-",
            "test-test-aaaaaaaaaaaaaaaaaa-6c82424-",
            id="longest-untruncated-cluster",
        ),
        param(
            "test",
            "test",
            "a" * 24,
            16,
            "test-test-aaaaaaaaaaaaaaaaaaaaaaaa-",
            "test-test-aaaaaaaaaaaaaaaaaa-e655d24-",
            id="two-digit-instance",
        ),
        param(
            "test",
            "test",
            "a" * 17 + "-" + "b" * 30,
            1,
            "test-test-aaaaaaaaaaaaaaaaa-96f9dcd-",
            "test-test-aaaaaaaaaaaaaaaaa-33aefd9-",
            id="hyphen-at-truncation-boundary",
        ),
        param(
            "my_app",
            "test",
            DB_NAME,
            1,
            "my-app-test-todos-",
            "my-app-test-todos-1-",
            id="app-underscore",
        ),
        param(
            "123app",
            "test",
            DB_NAME,
            1,
            "stlv-123app-test-todos-",
            "stlv-123app-test-todos-1-",
            id="app-leading-digit",
        ),
    ],
)
def test_document_db_aws_identifiers(  # noqa: PLR0913 — parametrized inputs and expectations
    pulumi_mocks,
    app,
    env,
    name,
    instances,
    cluster_prefix,
    instance_prefix,
):
    _set_app_context(app, env)

    @pulumi.runtime.test
    def deploy():
        return DocumentDb(name, vpc=Vpc(VPC_NAME), instances=instances).resources

    deploy()
    logical_name = f"{app}-{env}-{name}"
    cluster = pulumi_mocks.assert_res(
        logical_name,
        R.DOCDB_CLUSTER,
        {"clusterIdentifierPrefix": cluster_prefix},
        partial=True,
        prefixed=False,
    )
    pulumi_mocks.assert_res(
        f"{logical_name}-{instances}",
        R.DOCDB_INSTANCE,
        {"identifierPrefix": instance_prefix},
        partial=True,
        prefixed=False,
    )
    assert "clusterIdentifier" not in cluster.inputs
    if name == DB_NAME:
        # Sanitize cases also pin the parameter-group prefix; truncation cases only
        # need the cluster/instance budget (parameter-group hashing is the same path).
        parameter_group = pulumi_mocks.assert_res(
            f"{logical_name}-parameter-group", R.DOCDB_PARAMETER_GROUP, prefixed=False
        )
        assert parameter_group.inputs["namePrefix"] == (
            cluster_prefix.removesuffix("-") + "-parameter-group-"
        )
    pulumi_mocks.assert_res_counts(
        _counts(VPC_AZ2_COUNTS, APP_SG_COUNTS, DOCDB_COUNTS | {R.DOCDB_INSTANCE: instances})
    )


def test_document_db_raises_when_vpc_missing():
    with raises(TypeError, match=re.escape("DocumentDb 'todos' requires vpc=")):
        DocumentDb(DB_NAME)


def test_document_db_raises_when_config_and_kwargs_combined():
    vpc = Vpc(VPC_NAME)
    with raises(
        ValueError,
        match=re.escape(
            "Invalid configuration: cannot combine 'config' parameter with additional options "
            "- provide all settings either in 'config' or as separate options"
        ),
    ):
        DocumentDb(DB_NAME, config=DocumentDbConfig(vpc=vpc), instances=2)


def test_document_db_raises_when_config_type_invalid():
    with raises(
        TypeError,
        match=re.escape("Invalid config type: expected DocumentDbConfig or dict, got str"),
    ):
        DocumentDb(DB_NAME, config="invalid")


def test_document_db_raises_when_vpc_has_fewer_than_two_azs():
    with raises(
        ValueError,
        match=re.escape(
            "DocumentDb 'todos' requires a Vpc with at least 2 availability zones, "
            "got 1 from Vpc 'main_vpc'."
        ),
    ):
        DocumentDb(DB_NAME, vpc=Vpc(VPC_NAME, az=1))


def test_document_db_raises_when_customize_key_unknown():
    vpc = Vpc(VPC_NAME)
    error = (
        "Unknown customization key(s) ['ingress'] for DocumentDb 'todos'. "
        "Valid keys are: ['cluster', 'instance', 'parameter_group', 'secret_rotation', "
        "'security_group', 'subnet_group']"
    )
    with raises(ValueError, match=re.escape(error)):
        DocumentDb(DB_NAME, vpc=vpc, customize={"ingress": {}})


@dataclass
class DocumentDbTestCase:
    """Public inputs and independently specified resource expectations."""

    test_id: str
    opts: dict[str, Any] = field(default_factory=dict)
    style: Literal["kwargs", "object", "dict"] = "kwargs"
    tags: dict[str, str] | None = None
    family: str = "docdb8.0"
    engine_version: str = "8.0.0"
    aws_instance_class: str = "db.t4g.medium"
    expected_instances: tuple[str, ...] = ("todos-1",)
    backup_retention_period: int = 7
    deletion_protection: bool = False
    secret_rotation: int | Literal[False] = 7


DEFAULT_TC = DocumentDbTestCase(test_id="default")


def _build_document_db(tc: DocumentDbTestCase) -> DocumentDb:
    opts = {"vpc": Vpc(VPC_NAME), **tc.opts}
    if tc.style == "object":
        return DocumentDb(DB_NAME, config=DocumentDbConfig(**opts), tags=tc.tags)
    if tc.style == "dict":
        return DocumentDb(DB_NAME, config=opts, tags=tc.tags)
    return DocumentDb(DB_NAME, tags=tc.tags, **opts)


def verify_document_db(pulumi_mocks, tc: DocumentDbTestCase):
    user_tags = tc.tags or {}
    vpc_name = TP + VPC_NAME
    isolated_ids = [
        tid(TP + f"{VPC_NAME}-isolated-subnet-a"),
        tid(TP + f"{VPC_NAME}-isolated-subnet-b"),
    ]
    subnet_group_name = f"{DB_NAME}-subnet-group"
    parameter_group_name = f"{DB_NAME}-parameter-group"

    pulumi_mocks.assert_res(
        subnet_group_name,
        R.DOCDB_SUBNET_GROUP,
        {
            "description": "Managed by Pulumi",
            "subnetIds": isolated_ids,
            "tags": {"Name": TP + subnet_group_name} | user_tags,
        },
    )
    pulumi_mocks.assert_res(
        parameter_group_name,
        R.DOCDB_PARAMETER_GROUP,
        {
            "namePrefix": "test-test-todos-parameter-group-",
            "family": tc.family,
            "parameters": [{"name": "tls", "value": "enabled"}],
            "tags": {"Name": TP + parameter_group_name} | user_tags,
        },
    )
    pulumi_mocks.assert_res(
        CLUSTER_SG_NAME,
        R.SECURITY_GROUP,
        {
            "description": "Managed by Pulumi",
            "vpcId": tid(vpc_name),
            "tags": {"Name": TP + CLUSTER_SG_NAME} | user_tags,
        },
    )
    pulumi_mocks.assert_res(
        APP_SG_NAME,
        R.SECURITY_GROUP,
        {
            "vpcId": tid(vpc_name),
            "description": "Stelvio app tier: shared by functions attached to this VPC",
            "tags": {"Name": TP + APP_SG_NAME},
        },
    )
    pulumi_mocks.assert_res(
        DB_NAME,
        R.DOCDB_CLUSTER,
        {
            "clusterIdentifierPrefix": TP + DB_NAME + "-",
            "engine": "docdb",
            "engineVersion": tc.engine_version,
            "masterUsername": "stelvio",
            "manageMasterUserPassword": True,
            "storageEncrypted": True,
            "port": 27017,
            "backupRetentionPeriod": tc.backup_retention_period,
            "skipFinalSnapshot": True,
            "deletionProtection": tc.deletion_protection,
            "allowMajorVersionUpgrade": False,
            "dbSubnetGroupName": tn(TP + subnet_group_name),
            "vpcSecurityGroupIds": [CLUSTER_SG_ID],
            "dbClusterParameterGroupName": tn(TP + parameter_group_name),
            "tags": {"Name": TP + DB_NAME} | user_tags,
        },
    )
    rotation_inputs: dict[str, Any] = {
        "secretId": DOCDB_SECRET_ARN,
        "rotationEnabled": tc.secret_rotation is not False,
        "rotateImmediately": False,
    }
    if tc.secret_rotation is not False:
        rotation_inputs["rotationRules"] = {"automaticallyAfterDays": tc.secret_rotation}
    pulumi_mocks.assert_res(
        f"{DB_NAME}-secret-rotation",
        R.SECRET_ROTATION,
        rotation_inputs,
    )
    ingress_inputs: dict[str, Any] = {
        "securityGroupId": CLUSTER_SG_ID,
        "referencedSecurityGroupId": APP_SG_ID,
        "ipProtocol": "tcp",
        "fromPort": 27017,
        "toPort": 27017,
    }
    if tc.tags is not None:
        ingress_inputs["tags"] = tc.tags
    pulumi_mocks.assert_res(f"{DB_NAME}-ingress", R.SECURITY_GROUP_INGRESS_RULE, ingress_inputs)
    for instance_name in tc.expected_instances:
        pulumi_mocks.assert_res(
            instance_name,
            R.DOCDB_INSTANCE,
            {
                "clusterIdentifier": tid(TP + DB_NAME),
                "identifierPrefix": TP + instance_name + "-",
                "instanceClass": tc.aws_instance_class,
                "engine": "docdb",
                "tags": {"Name": TP + instance_name} | user_tags,
            },
        )
    pulumi_mocks.assert_res_counts(
        _counts(
            VPC_AZ2_COUNTS,
            APP_SG_COUNTS,
            DOCDB_COUNTS | {R.DOCDB_INSTANCE: len(tc.expected_instances)},
        )
    )


@mark.parametrize(
    "tc",
    [
        DEFAULT_TC,
        DocumentDbTestCase(
            "two-instances", {"instances": 2}, expected_instances=("todos-1", "todos-2")
        ),
        DocumentDbTestCase(
            "engine-5", {"engine": "5.0"}, family="docdb5.0", engine_version="5.0.0"
        ),
        DocumentDbTestCase(
            "instance-class", {"instance_class": "t4g.large"}, aws_instance_class="db.t4g.large"
        ),
        DocumentDbTestCase(
            "deletion-protection", {"deletion_protection": True}, deletion_protection=True
        ),
        DocumentDbTestCase(
            "config-object",
            {"secret_rotation": 30},
            style="object",
            secret_rotation=30,
            tags={"Team": "platform"},
        ),
        DocumentDbTestCase(
            "config-dict", {"secret_rotation": False}, style="dict", secret_rotation=False
        ),
    ],
    ids=lambda tc: tc.test_id,
)
def test_document_db(pulumi_mocks, tc):
    @pulumi.runtime.test
    def deploy():
        return _build_document_db(tc).resources

    deploy()
    verify_document_db(pulumi_mocks, tc)


def test_document_db_instance_class_accepts_db_prefix(pulumi_mocks):
    @pulumi.runtime.test
    def deploy():
        return DocumentDb(DB_NAME, vpc=Vpc(VPC_NAME), instance_class="db.t4g.large").resources

    deploy()
    pulumi_mocks.assert_res(
        f"{DB_NAME}-1", R.DOCDB_INSTANCE, {"instanceClass": "db.t4g.large"}, partial=True
    )
    pulumi_mocks.assert_res_counts(_counts(VPC_AZ2_COUNTS, APP_SG_COUNTS, DOCDB_COUNTS))


def test_document_db_uses_all_three_isolated_subnets(pulumi_mocks):
    @pulumi.runtime.test
    def deploy():
        return DocumentDb(DB_NAME, vpc=Vpc(VPC_NAME, az=3)).resources

    deploy()

    pulumi_mocks.assert_res(
        f"{DB_NAME}-subnet-group",
        R.DOCDB_SUBNET_GROUP,
        {
            "subnetIds": [
                tid(TP + f"{VPC_NAME}-isolated-subnet-a"),
                tid(TP + f"{VPC_NAME}-isolated-subnet-b"),
                tid(TP + f"{VPC_NAME}-isolated-subnet-c"),
            ]
        },
        partial=True,
    )
    pulumi_mocks.assert_res_counts(_counts(VPC_AZ3_COUNTS, APP_SG_COUNTS, DOCDB_COUNTS))


CUSTOMIZE_KEY_RESOURCES = [
    ("cluster", R.DOCDB_CLUSTER, [DB_NAME]),
    ("instance", R.DOCDB_INSTANCE, [f"{DB_NAME}-1", f"{DB_NAME}-2"]),
    ("subnet_group", R.DOCDB_SUBNET_GROUP, [f"{DB_NAME}-subnet-group"]),
    ("parameter_group", R.DOCDB_PARAMETER_GROUP, [f"{DB_NAME}-parameter-group"]),
    ("security_group", R.SECURITY_GROUP, [CLUSTER_SG_NAME]),
]


@mark.parametrize(
    ("key", "customization", "typ", "resource_names"),
    [
        *(
            param(key, {"tags": {"customized": key}}, typ, names, id=key)
            for key, typ, names in CUSTOMIZE_KEY_RESOURCES
        ),
        param(
            "cluster",
            ClusterArgs(tags={"customized": "cluster"}),
            R.DOCDB_CLUSTER,
            [DB_NAME],
            id="cluster-args",
        ),
    ],
)
def test_document_db_customize_targets_resource(
    pulumi_mocks, key, customization, typ, resource_names
):
    @pulumi.runtime.test
    def deploy():
        vpc = Vpc(VPC_NAME)
        return DocumentDb(DB_NAME, vpc=vpc, instances=2, customize={key: customization}).resources

    deploy()

    for name in resource_names:
        pulumi_mocks.assert_res(name, typ, {"tags": {"customized": key}}, partial=True)
    targeted = {TP + name for name in resource_names}
    for r in pulumi_mocks.created_resources:
        if r.name not in targeted:
            assert "customized" not in (r.inputs.get("tags") or {}), r.name
    pulumi_mocks.assert_res_counts(
        _counts(VPC_AZ2_COUNTS, APP_SG_COUNTS, DOCDB_COUNTS | {R.DOCDB_INSTANCE: 2})
    )


def test_document_db_customize_callable_receives_per_instance_props(pulumi_mocks):
    seen = []

    def bump_first(props: dict[str, Any]) -> dict[str, Any]:
        seen.append(props)
        if props["tags"]["Name"] == TP + f"{DB_NAME}-1":
            return props | {"instance_class": "db.t4g.large"}
        return props

    @pulumi.runtime.test
    def deploy():
        vpc = Vpc(VPC_NAME)
        return DocumentDb(
            DB_NAME, vpc=vpc, instances=2, customize={"instance": bump_first}
        ).resources

    deploy()

    assert [p["tags"]["Name"] for p in seen] == [TP + f"{DB_NAME}-1", TP + f"{DB_NAME}-2"]
    pulumi_mocks.assert_res(
        f"{DB_NAME}-1", R.DOCDB_INSTANCE, {"instanceClass": "db.t4g.large"}, partial=True
    )
    pulumi_mocks.assert_res(
        f"{DB_NAME}-2", R.DOCDB_INSTANCE, {"instanceClass": "db.t4g.medium"}, partial=True
    )
    pulumi_mocks.assert_res_counts(
        _counts(VPC_AZ2_COUNTS, APP_SG_COUNTS, DOCDB_COUNTS | {R.DOCDB_INSTANCE: 2})
    )


@mark.parametrize(
    ("customization", "expected"),
    [
        param(
            lambda props: props
            | {"vpc_security_group_ids": [*props["vpc_security_group_ids"], "sg-extra"]},
            [CLUSTER_SG_ID, "sg-extra"],
            id="append",
        ),
        param({"vpc_security_group_ids": ["sg-external"]}, ["sg-external"], id="replace"),
    ],
)
def test_document_db_customize_cluster_security_groups(pulumi_mocks, customization, expected):
    @pulumi.runtime.test
    def deploy():
        return DocumentDb(
            DB_NAME, vpc=Vpc(VPC_NAME), customize={"cluster": customization}
        ).resources

    deploy()
    pulumi_mocks.assert_res(
        DB_NAME,
        R.DOCDB_CLUSTER,
        {"vpcSecurityGroupIds": expected},
        partial=True,
    )
    _assert_app_sg_ingress(pulumi_mocks, DB_NAME)
    pulumi_mocks.assert_res_counts(_counts(VPC_AZ2_COUNTS, APP_SG_COUNTS, DOCDB_COUNTS))


@mark.parametrize(
    ("customize", "cluster_inputs", "instance_inputs", "absent"),
    [
        param(
            {
                "cluster": {"cluster_identifier": "custom-cluster"},
                "instance": {"identifier": "custom-instance"},
            },
            {"clusterIdentifier": "custom-cluster"},
            {"identifier": "custom-instance"},
            ("clusterIdentifierPrefix", "identifierPrefix"),
            id="identifiers",
        ),
        param(
            {
                "cluster": {"cluster_identifier_prefix": "custom-cluster-"},
                "instance": {"identifier_prefix": "custom-instance-"},
            },
            {"clusterIdentifierPrefix": "custom-cluster-"},
            {"identifierPrefix": "custom-instance-"},
            ("clusterIdentifier", "identifier"),
            id="prefixes",
        ),
    ],
)
def test_document_db_custom_identifiers(
    pulumi_mocks,
    customize,
    cluster_inputs,
    instance_inputs,
    absent,
):
    @pulumi.runtime.test
    def deploy():
        return DocumentDb(DB_NAME, vpc=Vpc(VPC_NAME), customize=customize).resources

    deploy()
    cluster = pulumi_mocks.assert_res(DB_NAME, R.DOCDB_CLUSTER, cluster_inputs, partial=True)
    instance = pulumi_mocks.assert_res(
        f"{DB_NAME}-1", R.DOCDB_INSTANCE, instance_inputs, partial=True
    )
    assert absent[0] not in cluster.inputs
    assert absent[1] not in instance.inputs
    pulumi_mocks.assert_res_counts(_counts(VPC_AZ2_COUNTS, APP_SG_COUNTS, DOCDB_COUNTS))


@mark.parametrize(
    ("parameters", "expected", "query"),
    [
        param(
            lambda: [{"name": "ttl_monitor", "value": "enabled"}],
            [{"name": "tls", "value": "enabled"}, {"name": "ttl_monitor", "value": "enabled"}],
            "tls=true&tlsCAFile=stlv_docdb_ca.pem&replicaSet=rs0&retryWrites=false",
            id="add-tls",
        ),
        param(
            lambda: [{"name": "tls", "value": "disabled"}],
            [{"name": "tls", "value": "disabled"}],
            "replicaSet=rs0&retryWrites=false",
            id="disable-tls",
        ),
        param(
            lambda: pulumi.Output.from_input([{"name": "audit_logs", "value": "disabled"}]),
            [{"name": "tls", "value": "enabled"}, {"name": "audit_logs", "value": "disabled"}],
            "tls=true&tlsCAFile=stlv_docdb_ca.pem&replicaSet=rs0&retryWrites=false",
            id="deferred-list",
        ),
    ],
)
def test_document_db_tls_parameters_and_connection_uri(
    pulumi_mocks,
    project_cwd,
    parameters,
    expected,
    query,
):
    @pulumi.runtime.test
    def deploy():
        vpc = Vpc(VPC_NAME)
        db = DocumentDb(
            DB_NAME, vpc=vpc, customize={"parameter_group": {"parameters": parameters()}}
        )
        return Function("client", handler=SIMPLE_HANDLER, vpc=vpc, links=[db]).resources

    deploy()
    pulumi_mocks.assert_res(
        f"{DB_NAME}-parameter-group",
        R.DOCDB_PARAMETER_GROUP,
        {"parameters": expected},
        partial=True,
    )
    assert _db_env_vars(pulumi_mocks, "client", DB_NAME)["STLV_TODOS_CONNECTION_URI"] == (
        f"mongodb://{DOCDB_HOST}:27017/?{query}"
    )
    pulumi_mocks.assert_res_counts(
        _counts(VPC_AZ2_COUNTS, APP_SG_COUNTS, DOCDB_COUNTS, FUNCTION_VPC_LINKED_COUNTS)
    )


def test_document_db_customize_secret_rotation(pulumi_mocks):
    @pulumi.runtime.test
    def deploy():
        return DocumentDb(
            DB_NAME,
            vpc=Vpc(VPC_NAME),
            secret_rotation=30,
            customize={"secret_rotation": {"rotate_immediately": True}},
        ).resources

    deploy()

    pulumi_mocks.assert_res(
        f"{DB_NAME}-secret-rotation",
        R.SECRET_ROTATION,
        {
            "secretId": DOCDB_SECRET_ARN,
            "rotationEnabled": True,
            "rotationRules": {"automaticallyAfterDays": 30},
            "rotateImmediately": True,
        },
    )
    pulumi_mocks.assert_res_counts(_counts(VPC_AZ2_COUNTS, APP_SG_COUNTS, DOCDB_COUNTS))


def test_document_db_resources_and_parenting(pulumi_mocks):
    @pulumi.runtime.test
    def deploy():
        hidden = {}

        def capture(args):
            if (
                args.type_ in (R.SECURITY_GROUP_INGRESS_RULE, R.SECRET_ROTATION)
                or args.name == TP + APP_SG_NAME
            ):
                hidden[args.name] = args.resource

        # Public transformations expose options/URNs of resources absent from .resources.
        pulumi.runtime.register_stack_transformation(capture)
        r = DocumentDb(DB_NAME, vpc=Vpc(VPC_NAME), instances=2).resources
        children = [
            r.cluster,
            *r.instances,
            r.subnet_group,
            r.parameter_group,
            r.security_group,
            hidden[TP + f"{DB_NAME}-ingress"],
            hidden[TP + f"{DB_NAME}-secret-rotation"],
        ]
        expected = [
            (R.DOCDB_CLUSTER, DB_NAME),
            (R.DOCDB_INSTANCE, f"{DB_NAME}-1"),
            (R.DOCDB_INSTANCE, f"{DB_NAME}-2"),
            (R.DOCDB_SUBNET_GROUP, f"{DB_NAME}-subnet-group"),
            (R.DOCDB_PARAMETER_GROUP, f"{DB_NAME}-parameter-group"),
            (R.SECURITY_GROUP, CLUSTER_SG_NAME),
            (R.SECURITY_GROUP_INGRESS_RULE, f"{DB_NAME}-ingress"),
            (R.SECRET_ROTATION, f"{DB_NAME}-secret-rotation"),
        ]

        def check(urns):
            assert urns == [
                f"urn:pulumi:stack::project::stelvio:aws:DocumentDb${typ}::{TP}{name}"
                for typ, name in expected
            ] + [
                f"urn:pulumi:stack::project::stelvio:aws:Vpc${R.SECURITY_GROUP}::{TP}{APP_SG_NAME}"
            ]

        return pulumi.Output.all(
            *[res.urn for res in children], hidden[TP + APP_SG_NAME].urn
        ).apply(check)

    deploy()
    pulumi_mocks.assert_res_counts(
        _counts(VPC_AZ2_COUNTS, APP_SG_COUNTS, DOCDB_COUNTS | {R.DOCDB_INSTANCE: 2})
    )


def test_document_db_config_dict_matches_dataclass():
    assert_config_dict_matches_dataclass(DocumentDbConfig, DocumentDbConfigDict)


def _link_env_vars(db_name: str) -> dict[str, str]:
    """The STLV_ env vars a DocumentDb link injects, from the mocked cluster outputs."""
    cluster_id = tid(TP + db_name)
    prefix = f"STLV_{db_name.replace('-', '_').upper()}_"
    host = f"{cluster_id}.cluster-{DEFAULT_REGION}.docdb.amazonaws.com"
    return {
        f"{prefix}HOST": host,
        f"{prefix}READER_HOST": f"{cluster_id}.cluster-ro-{DEFAULT_REGION}.docdb.amazonaws.com",
        f"{prefix}PORT": "27017",
        f"{prefix}USERNAME": "stelvio",
        f"{prefix}SECRET_ARN": (
            f"arn:aws:secretsmanager:{DEFAULT_REGION}:{ACCOUNT_ID}:secret:{cluster_id}"
        ),
        f"{prefix}REPLICA_SET": "rs0",
        f"{prefix}CA_FILE": DOCDB_CA_ZIP_PATH,
        f"{prefix}CONNECTION_URI": (
            f"mongodb://{host}:27017/?tls=true&tlsCAFile=stlv_docdb_ca.pem"
            "&replicaSet=rs0&retryWrites=false"
        ),
    }


def _db_env_vars(pulumi_mocks, fn_name: str, db_name: str) -> dict[str, str]:
    """Every STLV_{DB}_ env var on the Function, so extras fail the comparison."""
    variables = pulumi_mocks.assert_res(fn_name, R.FUNCTION).inputs["environment"]["variables"]
    prefix = f"STLV_{db_name.replace('-', '_').upper()}_"
    return {k: v for k, v in variables.items() if k.startswith(prefix)}


def _assert_function_document_db_link(pulumi_mocks, fn_name: str, *db_names: str) -> None:
    for name in db_names:
        assert _db_env_vars(pulumi_mocks, fn_name, name) == _link_env_vars(name)
    policy = pulumi_mocks.assert_res(f"{fn_name}-p", R.POLICY)
    assert json.loads(policy.inputs["policy"]) == [
        {
            "actions": ["secretsmanager:GetSecretValue"],
            "resources": [
                _link_env_vars(name)[f"STLV_{name.replace('-', '_').upper()}_SECRET_ARN"]
            ],
        }
        for name in db_names
    ]


def _assert_ca_packaged(
    pulumi_mocks, project_root: Path, fn_name: str, content: bytes = FAKE_DOCDB_CA_PEM
) -> None:
    ca = pulumi_mocks.assert_res(fn_name, R.FUNCTION).inputs["code"].assets[DOCDB_CA_ZIP_PATH]
    assert isinstance(ca, FileAsset)
    path = Path(ca.path)
    assert path.resolve() == (project_root / CA_CACHE_RELATIVE_PATH).resolve()
    assert path.read_bytes() == content


def _assert_app_sg_ingress(pulumi_mocks, db_name: str) -> None:
    pulumi_mocks.assert_res(
        f"{db_name}-ingress",
        R.SECURITY_GROUP_INGRESS_RULE,
        {
            "securityGroupId": tid(TP + f"{db_name}-sg"),
            "referencedSecurityGroupId": APP_SG_ID,
            "ipProtocol": "tcp",
            "fromPort": 27017,
            "toPort": 27017,
        },
        partial=True,
    )


def _overridden_link(db: DocumentDb):
    return db.link().with_permissions(
        AwsPermission(actions=["secretsmanager:GetSecretValue"], resources=["*"])
    )


def test_document_db_link_dev_mode_uses_absolute_ca_path(pulumi_mocks, project_cwd):
    ctx = replace(_ContextStore.get(), dev_mode=True)
    _ContextStore.clear()
    _ContextStore.set(ctx)
    ca_path = str(project_cwd.resolve() / CA_CACHE_RELATIVE_PATH)

    @pulumi.runtime.test
    def deploy():
        link = DocumentDb(DB_NAME, vpc=Vpc(VPC_NAME)).link()

        def check(values):
            assert values == [
                ca_path,
                f"mongodb://{DOCDB_HOST}:27017/?tls=true&tlsCAFile={quote_plus(ca_path)}"
                "&replicaSet=rs0&retryWrites=false",
            ]

        return pulumi.Output.all(
            link.properties["ca_file"], link.properties["connection_uri"]
        ).apply(check)

    deploy()
    pulumi_mocks.assert_res_counts(_counts(VPC_AZ2_COUNTS, APP_SG_COUNTS, DOCDB_COUNTS))


@mark.parametrize(
    ("other_vpc", "message"),
    [
        param(False, "but has no vpc=.", id="missing-vpc"),
        param(True, "in Vpc 'main_vpc' but is attached to Vpc 'other'.", id="wrong-vpc"),
    ],
)
def test_document_db_link_rejects_wrong_vpc(pulumi_mocks, project_cwd, other_vpc, message):
    @pulumi.runtime.test
    def deploy():
        db = DocumentDb(DB_NAME, vpc=Vpc(VPC_NAME))
        error = (
            f"Function 'client' links DocumentDb '{DB_NAME}' {message} "
            f"Set vpc= to Vpc '{VPC_NAME}'; linking is not networking."
        )
        with raises(ValueError, match=re.escape(error)):
            Function(
                "client",
                handler=SIMPLE_HANDLER,
                vpc=Vpc("other") if other_vpc else None,
                links=[db],
            )
        return db.resources

    deploy()
    pulumi_mocks.assert_res_counts(_counts(VPC_AZ2_COUNTS, APP_SG_COUNTS, DOCDB_COUNTS))


@mark.parametrize(
    ("as_link", "secret_arn"),
    [
        param(lambda db: db, DOCDB_SECRET_ARN, id="default"),
        param(_overridden_link, "*", id="permission-override"),
    ],
)
def test_document_db_function_link(
    pulumi_mocks,
    project_cwd,
    mock_docdb_ca_urlopen,
    as_link,
    secret_arn,
):
    @pulumi.runtime.test
    def deploy():
        vpc = Vpc(VPC_NAME)
        db = DocumentDb(DB_NAME, vpc=vpc)
        return Function("client", handler=SIMPLE_HANDLER, vpc=vpc, links=[as_link(db)]).resources

    deploy()

    assert _db_env_vars(pulumi_mocks, "client", DB_NAME) == _link_env_vars(DB_NAME)
    policy = pulumi_mocks.assert_res("client-p", R.POLICY)
    assert json.loads(policy.inputs["policy"]) == [
        {"actions": ["secretsmanager:GetSecretValue"], "resources": [secret_arn]}
    ]
    pulumi_mocks.assert_res(
        "client",
        R.FUNCTION,
        {"vpcConfig": {"subnetIds": PRIVATE_SUBNET_IDS, "securityGroupIds": [APP_SG_ID]}},
        partial=True,
    )
    _assert_app_sg_ingress(pulumi_mocks, DB_NAME)
    if secret_arn == DOCDB_SECRET_ARN:
        _assert_ca_packaged(pulumi_mocks, project_cwd, "client")
        assert mock_docdb_ca_urlopen == [CA_BUNDLE_URL]
        assert sorted(p.name for p in (project_cwd / CA_CACHE_RELATIVE_PATH).parent.iterdir()) == [
            "global-bundle.pem"
        ]
    pulumi_mocks.assert_res_counts(
        _counts(VPC_AZ2_COUNTS, APP_SG_COUNTS, DOCDB_COUNTS, FUNCTION_VPC_LINKED_COUNTS)
    )


def test_function_without_document_db_link_does_not_package_ca(
    pulumi_mocks, project_cwd, mock_docdb_ca_urlopen
):
    @pulumi.runtime.test
    def deploy():
        vpc = Vpc(VPC_NAME)
        db = DocumentDb(DB_NAME, vpc=vpc)
        fn = Function("client", handler=SIMPLE_HANDLER, vpc=vpc)
        return db.resources, fn.resources

    deploy()

    assert (
        DOCDB_CA_ZIP_PATH
        not in pulumi_mocks.assert_res("client", R.FUNCTION).inputs["code"].assets
    )
    assert mock_docdb_ca_urlopen == []
    assert not (project_cwd / CA_CACHE_RELATIVE_PATH).exists()
    pulumi_mocks.assert_res_counts(
        _counts(
            VPC_AZ2_COUNTS,
            APP_SG_COUNTS,
            DOCDB_COUNTS,
            {R.FUNCTION: 1, R.ROLE: 1, R.ROLE_POLICY_ATTACHMENT: 2},
        )
    )


def _deploy_linked_function() -> None:
    @pulumi.runtime.test
    def deploy():
        vpc = Vpc(VPC_NAME)
        db = DocumentDb(DB_NAME, vpc=vpc)
        fn = Function("client", handler=SIMPLE_HANDLER, vpc=vpc, links=[db])
        return db.resources, fn.resources

    deploy()


def _write_ca_cache(project_root: Path, content: bytes, *, age_seconds: float = 0) -> Path:
    cache = project_root / CA_CACHE_RELATIVE_PATH
    cache.parent.mkdir(parents=True)
    cache.write_bytes(content)
    if age_seconds:
        mtime = time.time() - age_seconds
        os.utime(cache, (mtime, mtime))
    return cache


def _fail_download(monkeypatch, exc: Exception) -> None:
    def raise_exc(_url: str, **_kwargs: object) -> object:
        raise exc

    monkeypatch.setattr("stelvio.aws.document_db.urlopen", raise_exc)


@mark.parametrize(
    ("exc", "detail", "stale_cache"),
    [
        param(URLError("network down"), "<urlopen error network down>", False, id="url-error"),
        param(URLError("network down"), "<urlopen error network down>", True, id="stale-cache"),
    ],
)
def test_document_db_ca_download_failure(  # noqa: PLR0913 — parametrized inputs and expectations
    pulumi_mocks,
    project_cwd,
    monkeypatch,
    exc,
    detail,
    stale_cache,
):
    cache = project_cwd / CA_CACHE_RELATIVE_PATH
    if stale_cache:
        _write_ca_cache(project_cwd, _OLD_CA_PEM, age_seconds=CA_CACHE_TTL_SECONDS + 60)
    _fail_download(monkeypatch, exc)

    with raises(
        RuntimeError,
        match=re.escape(f"Failed to download DocumentDB CA bundle from {CA_BUNDLE_URL}: {detail}"),
    ):
        _deploy_linked_function()
    if stale_cache:
        assert cache.read_bytes() == _OLD_CA_PEM
    else:
        assert not cache.exists()


def test_document_db_ca_rejects_invalid_download(pulumi_mocks, project_cwd, monkeypatch):
    monkeypatch.setattr(
        "stelvio.aws.document_db.urlopen",
        lambda _url, **_kwargs: FakeUrlopenResponse(b"not a certificate"),
    )

    with raises(
        RuntimeError,
        match=re.escape(f"DocumentDB CA bundle from {CA_BUNDLE_URL} is empty or not a PEM file."),
    ):
        _deploy_linked_function()
    assert not (project_cwd / CA_CACHE_RELATIVE_PATH).exists()


@mark.parametrize(
    ("content", "age", "downloads", "packaged"),
    [
        param(_OLD_CA_PEM, CA_CACHE_TTL_SECONDS - 60, [], _OLD_CA_PEM, id="fresh"),
        param(
            _OLD_CA_PEM, CA_CACHE_TTL_SECONDS + 60, [CA_BUNDLE_URL], FAKE_DOCDB_CA_PEM, id="stale"
        ),
        param(b"not a certificate", 0, [CA_BUNDLE_URL], FAKE_DOCDB_CA_PEM, id="corrupt"),
    ],
)
def test_document_db_ca_cache(  # noqa: PLR0913 — parametrized inputs and expectations
    pulumi_mocks,
    project_cwd,
    mock_docdb_ca_urlopen,
    content,
    age,
    downloads,
    packaged,
):
    _write_ca_cache(project_cwd, content, age_seconds=age)
    _deploy_linked_function()

    assert mock_docdb_ca_urlopen == downloads
    _assert_ca_packaged(pulumi_mocks, project_cwd, "client", content=packaged)
    pulumi_mocks.assert_res_counts(
        _counts(VPC_AZ2_COUNTS, APP_SG_COUNTS, DOCDB_COUNTS, FUNCTION_VPC_LINKED_COUNTS)
    )


def test_document_db_ca_failed_cache_write_keeps_stale_cache(
    pulumi_mocks, project_cwd, monkeypatch
):
    cache = _write_ca_cache(project_cwd, _OLD_CA_PEM, age_seconds=CA_CACHE_TTL_SECONDS + 60)
    real_replace = os.replace

    def replace(src: str, dst: str) -> None:
        if Path(dst).resolve() == cache.resolve():
            raise OSError("disk full")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", replace)

    with raises(
        RuntimeError,
        match=re.escape(
            f"Failed to write DocumentDB CA bundle cache "
            f"{project_cwd.resolve() / CA_CACHE_RELATIVE_PATH}: disk full"
        ),
    ):
        _deploy_linked_function()
    assert cache.read_bytes() == _OLD_CA_PEM
    assert list(cache.parent.glob("*.tmp")) == []


def test_function_construction_does_not_download_ca(project_cwd, mock_docdb_ca_urlopen):
    vpc = Vpc(VPC_NAME)

    Function("client", handler=SIMPLE_HANDLER, vpc=vpc, links=[DocumentDb(DB_NAME, vpc=vpc)])

    assert mock_docdb_ca_urlopen == []
    assert not (project_cwd / CA_CACHE_RELATIVE_PATH).exists()


def test_two_functions_linked_to_one_document_db(pulumi_mocks, project_cwd, mock_docdb_ca_urlopen):
    @pulumi.runtime.test
    def deploy():
        vpc = Vpc(VPC_NAME)
        db = DocumentDb(DB_NAME, vpc=vpc)
        reader = Function("reader", handler=SIMPLE_HANDLER, vpc=vpc, links=[db])
        writer = Function("writer", handler=SIMPLE_HANDLER, vpc=vpc, links=[db])
        first = reader.resources
        # Prove process memoization even when the on-disk cache expires between links.
        stale = time.time() - CA_CACHE_TTL_SECONDS - 60
        os.utime(project_cwd / CA_CACHE_RELATIVE_PATH, (stale, stale))
        return first, writer.resources

    deploy()

    assert mock_docdb_ca_urlopen == [CA_BUNDLE_URL]
    for fn_name in ("reader", "writer"):
        _assert_function_document_db_link(pulumi_mocks, fn_name, DB_NAME)
        fn_res = pulumi_mocks.assert_res(fn_name, R.FUNCTION)
        assert fn_res.inputs["vpcConfig"]["securityGroupIds"] == [APP_SG_ID]
        _assert_ca_packaged(pulumi_mocks, project_cwd, fn_name)
    _assert_app_sg_ingress(pulumi_mocks, DB_NAME)
    pulumi_mocks.assert_res_counts(
        _counts(
            VPC_AZ2_COUNTS,
            APP_SG_COUNTS,
            DOCDB_COUNTS,
            {
                R.FUNCTION: 2,
                R.ROLE: 2,
                R.POLICY: 2,
                R.ROLE_POLICY_ATTACHMENT: 6,
            },
        )
    )


def test_function_linked_to_two_document_dbs(pulumi_mocks, project_cwd):
    @pulumi.runtime.test
    def deploy():
        vpc = Vpc(VPC_NAME)
        todos = DocumentDb(DB_NAME, vpc=vpc)
        orders = DocumentDb("orders", vpc=vpc)
        fn = Function("client", handler=SIMPLE_HANDLER, vpc=vpc, links=[todos, orders])
        return todos.resources, orders.resources, fn.resources

    deploy()

    _assert_function_document_db_link(pulumi_mocks, "client", DB_NAME, "orders")
    pulumi_mocks.assert_res(
        "client",
        R.FUNCTION,
        {"vpcConfig": {"subnetIds": PRIVATE_SUBNET_IDS, "securityGroupIds": [APP_SG_ID]}},
        partial=True,
    )
    _assert_app_sg_ingress(pulumi_mocks, DB_NAME)
    _assert_app_sg_ingress(pulumi_mocks, "orders")
    pulumi_mocks.assert_res_counts(
        _counts(
            VPC_AZ2_COUNTS,
            APP_SG_COUNTS,
            {
                R.DOCDB_SUBNET_GROUP: 2,
                R.DOCDB_PARAMETER_GROUP: 2,
                R.SECURITY_GROUP: 2,
                R.SECURITY_GROUP_INGRESS_RULE: 2,
                R.DOCDB_CLUSTER: 2,
                R.DOCDB_INSTANCE: 2,
                R.SECRET_ROTATION: 2,
            },
            FUNCTION_VPC_LINKED_COUNTS,
        )
    )


@mark.parametrize(
    ("customization", "overrides"),
    [
        (
            {"master_username": "admin"},
            {
                "STLV_TODOS_USERNAME": "admin",
            },
        ),
        (
            {"port": 27018},
            {
                "STLV_TODOS_PORT": "27018",
                "STLV_TODOS_CONNECTION_URI": (
                    f"mongodb://{DOCDB_HOST}:27018/?tls=true&tlsCAFile=stlv_docdb_ca.pem"
                    "&replicaSet=rs0&retryWrites=false"
                ),
            },
        ),
    ],
)
def test_document_db_link_uses_customized_connection_properties(
    pulumi_mocks, project_cwd, customization, overrides
):
    @pulumi.runtime.test
    def deploy():
        vpc = Vpc(VPC_NAME)
        db = DocumentDb(DB_NAME, vpc=vpc, customize={"cluster": customization})
        return Function("client", handler=SIMPLE_HANDLER, vpc=vpc, links=[db]).resources

    deploy()

    expected = _link_env_vars(DB_NAME) | overrides
    assert _db_env_vars(pulumi_mocks, "client", DB_NAME) == expected
    port = int(expected["STLV_TODOS_PORT"])
    pulumi_mocks.assert_res(DB_NAME, R.DOCDB_CLUSTER, {"port": port}, partial=True)
    pulumi_mocks.assert_res(
        f"{DB_NAME}-ingress",
        R.SECURITY_GROUP_INGRESS_RULE,
        {"fromPort": port, "toPort": port, "ipProtocol": "tcp"},
        partial=True,
    )
    pulumi_mocks.assert_res_counts(
        _counts(VPC_AZ2_COUNTS, APP_SG_COUNTS, DOCDB_COUNTS, FUNCTION_VPC_LINKED_COUNTS)
    )


_UNMANAGED_PASSWORD = {"manage_master_user_password": False, "master_password": "not-a-secret"}


def test_document_db_without_managed_master_password_skips_rotation(pulumi_mocks):
    @pulumi.runtime.test
    def deploy():
        return DocumentDb(
            DB_NAME, vpc=Vpc(VPC_NAME), customize={"cluster": _UNMANAGED_PASSWORD}
        ).resources

    deploy()

    pulumi_mocks.assert_res(
        DB_NAME, R.DOCDB_CLUSTER, {"manageMasterUserPassword": False}, partial=True
    )
    without_rotation = {k: v for k, v in DOCDB_COUNTS.items() if k != R.SECRET_ROTATION}
    pulumi_mocks.assert_res_counts(_counts(VPC_AZ2_COUNTS, APP_SG_COUNTS, without_rotation))


def test_document_db_link_raises_without_managed_master_password(pulumi_mocks, project_cwd):
    @pulumi.runtime.test
    def deploy():
        db = DocumentDb(DB_NAME, vpc=Vpc(VPC_NAME), customize={"cluster": _UNMANAGED_PASSWORD})
        return db.link().properties["secret_arn"]

    with raises(
        ValueError,
        match=re.escape(
            "Cannot link DocumentDb 'todos': the cluster has no AWS-managed "
            "master-user secret. Linking requires 'manage_master_user_password' to stay "
            "enabled (the default) - it was likely disabled via customize."
        ),
    ):
        deploy()


def test_document_db_rejects_output_manage_master_user_password(pulumi_mocks):
    @pulumi.runtime.test
    def deploy():
        return DocumentDb(
            DB_NAME,
            vpc=Vpc(VPC_NAME),
            customize={
                "cluster": {
                    "manage_master_user_password": pulumi.Output.from_input(False),
                    "master_password": "not-a-secret",
                }
            },
        ).resources

    with raises(
        ValueError,
        match=r"'manage_master_user_password' must be a plain bool "
        r"\(Output and other deferred values are not supported\)\.",
    ):
        deploy()


@mark.parametrize(
    ("values", "mode", "expected"),
    [
        param(None, "global", ("db.r6g.large", True, 14), id="omitted"),
        param(
            ("t4g.medium", False, 7),
            "callable",
            ("db.r6g.large", True, 14),
            id="global-callable",
        ),
        param(("t4g.medium", True, 7), "local", ("db.r5.large", False, 1), id="local-overrides"),
    ],
)
def test_document_db_customization_precedence(pulumi_mocks, values, mode, expected):
    global_props = {
        "instance": {"instance_class": "db.r6g.large"},
        "cluster": {"deletion_protection": True, "backup_retention_period": 14},
    }
    if mode == "callable":
        global_props = {
            key: lambda props, extra=extra: props | extra for key, extra in global_props.items()
        }
    _set_app_context(customize={DocumentDb: global_props})
    local = (
        {
            "instance": {"instance_class": "db.r5.large"},
            "cluster": {"deletion_protection": False, "backup_retention_period": 1},
        }
        if mode == "local"
        else None
    )

    @pulumi.runtime.test
    def deploy():
        opts = (
            dict(
                zip(
                    ("instance_class", "deletion_protection", "backup_retention_period"),
                    values,
                    strict=True,
                )
            )
            if values
            else {}
        )
        return DocumentDb(DB_NAME, vpc=Vpc(VPC_NAME), customize=local, **opts).resources

    deploy()
    instance, protection, retention = expected
    verify_document_db(
        pulumi_mocks,
        replace(
            DEFAULT_TC,
            aws_instance_class=instance,
            deletion_protection=protection,
            backup_retention_period=retention,
        ),
    )


def test_document_db_cluster_ignores_availability_zones_changes(pulumi_mocks):
    seen: list[list[str] | None] = []

    @pulumi.runtime.test
    def deploy():
        def capture(args):
            if args.type_ == R.DOCDB_CLUSTER:
                seen.append(args.opts.ignore_changes)

        pulumi.runtime.register_stack_transformation(capture)
        return DocumentDb(DB_NAME, vpc=Vpc(VPC_NAME)).resources

    deploy()
    assert seen == [["availability_zones"]]
    cluster = pulumi_mocks.assert_res(DB_NAME, R.DOCDB_CLUSTER)
    assert "availabilityZones" not in cluster.inputs
    pulumi_mocks.assert_res_counts(_counts(VPC_AZ2_COUNTS, APP_SG_COUNTS, DOCDB_COUNTS))


def test_document_db_secret_rotation_depends_on_instances(pulumi_mocks):
    instances: list[Any] = []
    rotation_depends: list[Any] = []

    @pulumi.runtime.test
    def deploy():
        def capture(args):
            if args.type_ == R.DOCDB_INSTANCE:
                instances.append(args.resource)
            if args.type_ == R.SECRET_ROTATION:
                rotation_depends.append(args.opts.depends_on)

        pulumi.runtime.register_stack_transformation(capture)
        return DocumentDb(DB_NAME, vpc=Vpc(VPC_NAME), instances=2, secret_rotation=False).resources

    deploy()

    assert len(rotation_depends) == 1
    assert rotation_depends[0] is not None
    assert list(rotation_depends[0]) == instances
    assert len(instances) == 2
    pulumi_mocks.assert_res_counts(
        _counts(
            VPC_AZ2_COUNTS,
            APP_SG_COUNTS,
            DOCDB_COUNTS | {R.DOCDB_INSTANCE: 2},
        )
    )
