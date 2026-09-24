import json
import re
from collections import Counter
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote_plus

import pulumi
from pulumi import FileAsset
from pulumi_aws.docdb import ClusterArgs
from pytest import mark, param, raises

from stelvio.aws.api_gateway import HttpApi
from stelvio.aws.document_db import (
    DocumentDb,
    DocumentDbConfig,
    DocumentDbConfigDict,
)
from stelvio.aws.function import Function
from stelvio.aws.permission import AwsPermission
from stelvio.aws.vpc import Vpc, VpcAttachment
from stelvio.config import AwsConfig
from stelvio.context import AppContext, _ContextStore
from stelvio.link import Link
from tests.aws.pulumi_mocks import (
    ACCOUNT_ID,
    DEFAULT_REGION,
    TP,
    R,
    tid,
    tn,
)
from tests.test_utils import assert_config_dict_matches_dataclass

DB_NAME = "todos"
VPC_NAME = "main_vpc"
APP_SG_NAME = f"{VPC_NAME}-app-sg"
APP_SG_ID = tid(TP + APP_SG_NAME)
CLUSTER_SG_NAME = f"{DB_NAME}-sg"
CLUSTER_SG_ID = tid(TP + CLUSTER_SG_NAME)
CLUSTER_ID = tid(TP + DB_NAME)
DOCDB_HOST = f"{CLUSTER_ID}.cluster-{DEFAULT_REGION}.docdb.amazonaws.com"
DOCDB_READER_HOST = f"{CLUSTER_ID}.cluster-ro-{DEFAULT_REGION}.docdb.amazonaws.com"
DOCDB_SECRET_ARN = f"arn:aws:secretsmanager:{DEFAULT_REGION}:{ACCOUNT_ID}:secret:{CLUSTER_ID}"
PRIVATE_SUBNET_IDS = [tid(TP + f"{VPC_NAME}-private-subnet-{az}") for az in "ab"]
ISOLATED_SUBNET_IDS = [tid(TP + f"{VPC_NAME}-isolated-subnet-{az}") for az in "ab"]
DOCDB_CA_ZIP_PATH = "stlv_docdb_ca.pem"
DOCDB_CA_VENDORED_PATH = (
    Path(__file__).resolve().parents[2] / "stelvio" / "aws" / "documentdb" / "global-bundle.pem"
)
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
VPC_AZ3_COUNTS = {
    R.VPC: 1,
    R.INTERNET_GATEWAY: 1,
    R.DEFAULT_SECURITY_GROUP: 1,
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
    _ContextStore.clear()
    _ContextStore.set(
        AppContext(
            name=app,
            env=env,
            aws=AwsConfig(profile="default", region="us-east-1"),
            home="aws",
            customize=customize or {},
        )
    )


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
            {"instances": 17},
            ValueError,
            "`instances` must be between 1 and 16, got 17",
            id="instances-above-max",
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
            {"backup_retention_period": 36},
            ValueError,
            "`backup_retention_period` must be between 1 and 35, got 36",
            id="backup-retention-above-max",
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
            {"secret_rotation": 1001},
            ValueError,
            "`secret_rotation` must be between 1 and 1000, or False, got 1001",
            id="secret-rotation-above-max",
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


def test_document_db_longest_untruncated_identifier_prefix(pulumi_mocks):
    # 63-char AWS limit minus the provider's 26-char generated suffix leaves 37
    name = "a" * 26

    @pulumi.runtime.test
    def deploy():
        return DocumentDb(name, vpc=Vpc(VPC_NAME)).resources

    deploy()

    cluster = pulumi_mocks.assert_res(name, R.DOCDB_CLUSTER)
    assert cluster.inputs["clusterIdentifierPrefix"] == TP + name + "-"
    assert len(cluster.inputs["clusterIdentifierPrefix"]) == 37
    assert "clusterIdentifier" not in cluster.inputs
    pulumi_mocks.assert_res_counts(_counts(VPC_AZ2_COUNTS, APP_SG_COUNTS, DOCDB_COUNTS))


@mark.parametrize(
    ("app", "env", "identifier"),
    [
        param("my_app", "test", "my-app-test-todos", id="app-underscore"),
        param("test", "dev_1", "test-dev-1-todos", id="env-underscore"),
        param("test", "dev.1", "test-dev-1-todos", id="env-dot"),
        param("123app", "test", "stlv-123app-test-todos", id="app-leading-digit"),
        param("_app", "test", "stlv-app-test-todos", id="app-leading-punctuation"),
    ],
)
def test_document_db_sanitizes_app_env_in_aws_identifier(pulumi_mocks, app, env, identifier):
    _set_app_context(app, env)

    @pulumi.runtime.test
    def deploy():
        return DocumentDb(DB_NAME, vpc=Vpc(VPC_NAME)).resources

    deploy()

    pulumi_name = f"{app.lower()}-{env.lower()}-{DB_NAME}"
    cluster = pulumi_mocks.assert_res(pulumi_name, R.DOCDB_CLUSTER, prefixed=False)
    assert cluster.inputs["clusterIdentifierPrefix"] == identifier + "-"
    instance = pulumi_mocks.assert_res(f"{pulumi_name}-1", R.DOCDB_INSTANCE, prefixed=False)
    assert instance.inputs["identifierPrefix"] == identifier + "-1-"
    parameter_group = pulumi_mocks.assert_res(
        f"{pulumi_name}-parameter-group", R.DOCDB_PARAMETER_GROUP, prefixed=False
    )
    assert parameter_group.inputs["namePrefix"] == identifier + "-parameter-group-"
    pulumi_mocks.assert_res_counts(_counts(VPC_AZ2_COUNTS, APP_SG_COUNTS, DOCDB_COUNTS))


def test_document_db_long_app_env_truncates_identifier_prefix(pulumi_mocks):
    app, env, name = "a" * 20, "b" * 20, "c" * 30
    _set_app_context(app, env)
    pulumi_name = "aaaaaaaaaaaaaaaaaaaa-bbbbbbbbbbbbbbbbbbbb-" + "c" * 30

    @pulumi.runtime.test
    def deploy():
        return DocumentDb(name, vpc=Vpc(VPC_NAME)).resources

    deploy()

    cluster = pulumi_mocks.assert_res(pulumi_name, R.DOCDB_CLUSTER, prefixed=False)
    assert cluster.inputs["clusterIdentifierPrefix"] == "aaaaaaaaaaaaaaaaaaaa-bbbbbbb-daf89e4-"
    instance = pulumi_mocks.assert_res(pulumi_name + "-1", R.DOCDB_INSTANCE, prefixed=False)
    assert instance.inputs["identifierPrefix"] == "aaaaaaaaaaaaaaaaaaaa-bbbbbbb-b30de1a-"
    pulumi_mocks.assert_res_counts(_counts(VPC_AZ2_COUNTS, APP_SG_COUNTS, DOCDB_COUNTS))


def test_document_db_identifier_prefixes_fit_aws_identifier_limit(pulumi_mocks):
    name = "a" * 24  # 34 chars before the generated separator

    @pulumi.runtime.test
    def deploy():
        return DocumentDb(name, vpc=Vpc(VPC_NAME), instances=16).resources

    deploy()

    cluster = pulumi_mocks.assert_res(name, R.DOCDB_CLUSTER)
    instance = pulumi_mocks.assert_res(f"{name}-16", R.DOCDB_INSTANCE)
    assert cluster.inputs["clusterIdentifierPrefix"] == TP + name + "-"
    instance_prefix = instance.inputs["identifierPrefix"]
    assert instance_prefix == "test-test-aaaaaaaaaaaaaaaaaa-e655d24-"
    pulumi_mocks.assert_res_counts(
        _counts(VPC_AZ2_COUNTS, APP_SG_COUNTS, DOCDB_COUNTS | {R.DOCDB_INSTANCE: 16})
    )


def test_document_db_identifier_collapses_hyphen_at_truncation_boundary(pulumi_mocks):
    name = "a" * 44 + "-" + "b" * 20

    @pulumi.runtime.test
    def deploy():
        return DocumentDb(name, vpc=Vpc(VPC_NAME)).resources

    deploy()

    pulumi_name = "test-test-" + "a" * 44 + "-" + "b" * 20
    cluster = pulumi_mocks.assert_res(pulumi_name, R.DOCDB_CLUSTER, prefixed=False)
    identifier = cluster.inputs["clusterIdentifierPrefix"]
    assert identifier == "test-test-aaaaaaaaaaaaaaaaaa-5b4feef-"
    pulumi_mocks.assert_res_counts(_counts(VPC_AZ2_COUNTS, APP_SG_COUNTS, DOCDB_COUNTS))


@mark.parametrize(
    "kwargs",
    [param({}, id="kwargs"), param({"config": {}}, id="config-dict")],
)
def test_document_db_raises_when_vpc_missing(kwargs):
    with raises(TypeError, match=re.escape("DocumentDb 'todos' requires vpc=")):
        DocumentDb(DB_NAME, **kwargs)


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


@mark.parametrize(
    "az",
    [param(1, id="az-int"), param(["us-east-1a"], id="az-list")],
)
def test_document_db_raises_when_vpc_has_fewer_than_two_azs(az):
    with raises(
        ValueError,
        match=re.escape(
            "DocumentDb 'todos' requires a Vpc with at least 2 availability zones, "
            "got 1 from Vpc 'main_vpc'."
        ),
    ):
        DocumentDb(DB_NAME, vpc=Vpc(VPC_NAME, az=az))


def test_document_db_raises_when_customize_key_unknown():
    vpc = Vpc(VPC_NAME)
    error = (
        "Unknown customization key(s) ['ingress'] for DocumentDb 'todos'. "
        "Valid keys are: ['cluster', 'instance', 'parameter_group', 'secret_rotation', "
        "'security_group', 'subnet_group']"
    )
    with raises(ValueError, match=re.escape(error)):
        DocumentDb(DB_NAME, vpc=vpc, customize={"ingress": {}})


def test_document_db_raises_when_customize_key_is_constructor_option():
    vpc = Vpc(VPC_NAME)
    error = (
        "Unknown customization key(s) ['instances'] for DocumentDb 'todos'. "
        "Valid keys are: ['cluster', 'instance', 'parameter_group', 'secret_rotation', "
        "'security_group', 'subnet_group']"
    )
    with raises(ValueError, match=re.escape(error)):
        DocumentDb(DB_NAME, vpc=vpc, customize={"instances": {}})


@dataclass
class DocumentDbTestCase:
    """A DocumentDb config and the complete infrastructure expected from it.

    Values AWS derives (family, engine version, instance class, instance names)
    are literal expectation fields. Passthrough settings (retention, deletion
    protection, rotation) are asserted as given, with their documented defaults
    when None. `verify_document_db` asserts every input plus sealed counts.
    """

    test_id: str
    instances: int = 1
    instance_class: str | None = None
    engine: str = "8.0"
    deletion_protection: bool | None = None
    backup_retention_period: int | None = None
    secret_rotation: int | Literal[False] = 7
    tags: dict[str, str] | None = None
    style: Literal["kwargs", "object", "dict"] = "kwargs"
    family: str = "docdb8.0"
    engine_version: str = "8.0.0"
    aws_instance_class: str = "db.t4g.medium"
    expected_instances: tuple[str, ...] = ("todos-1",)


_CONFIG_FIELDS = (
    "instances",
    "instance_class",
    "engine",
    "deletion_protection",
    "backup_retention_period",
    "secret_rotation",
)

DEFAULT_TC = DocumentDbTestCase(test_id="default")
TWO_INSTANCES_TC = DocumentDbTestCase(
    test_id="two-instances",
    instances=2,
    expected_instances=("todos-1", "todos-2"),
)
ENGINE_5_TC = DocumentDbTestCase(
    test_id="engine-5", engine="5.0", family="docdb5.0", engine_version="5.0.0"
)
INSTANCE_CLASS_TC = DocumentDbTestCase(
    test_id="instance-class", instance_class="t4g.large", aws_instance_class="db.t4g.large"
)
INSTANCE_CLASS_DB_PREFIX_TC = DocumentDbTestCase(
    test_id="instance-class-db-prefix",
    instance_class="db.t4g.large",
    aws_instance_class="db.t4g.large",
)
DELETION_PROTECTION_TC = DocumentDbTestCase(
    test_id="deletion-protection", deletion_protection=True
)
BACKUP_RETENTION_TC = DocumentDbTestCase(test_id="backup-retention", backup_retention_period=14)
CUSTOM_ROTATION_TC = DocumentDbTestCase(test_id="custom-rotation", secret_rotation=30)
MIN_ROTATION_TC = DocumentDbTestCase(test_id="min-rotation", secret_rotation=1)
MAX_ROTATION_TC = DocumentDbTestCase(test_id="max-rotation", secret_rotation=1000)
NO_ROTATION_TC = DocumentDbTestCase(test_id="no-rotation", secret_rotation=False)
CUSTOM_ROTATION_OBJECT_TC = replace(
    CUSTOM_ROTATION_TC, test_id="custom-rotation-object", style="object"
)
NO_ROTATION_DICT_TC = replace(NO_ROTATION_TC, test_id="no-rotation-dict", style="dict")
CONFIG_OBJECT_TC = replace(TWO_INSTANCES_TC, test_id="config-object", style="object")
CONFIG_DICT_TC = replace(ENGINE_5_TC, test_id="config-dict", style="dict")
TAGS_TC = replace(DEFAULT_TC, test_id="tags", tags={"stage": "test", "team": "core"})


def _build_document_db(tc: DocumentDbTestCase) -> DocumentDb:
    vpc = Vpc(VPC_NAME)
    if tc.style == "object":
        return DocumentDb(
            DB_NAME,
            config=DocumentDbConfig(
                vpc=vpc,
                instances=tc.instances,
                instance_class=tc.instance_class,
                engine=tc.engine,
                deletion_protection=tc.deletion_protection,
                backup_retention_period=tc.backup_retention_period,
                secret_rotation=tc.secret_rotation,
            ),
            tags=tc.tags,
        )
    opts: dict[str, Any] = {"vpc": vpc} | {
        f.name: getattr(tc, f.name)
        for f in fields(tc)
        if f.name in _CONFIG_FIELDS and getattr(tc, f.name) != f.default
    }
    if tc.style == "dict":
        config: DocumentDbConfigDict = opts
        return DocumentDb(DB_NAME, config=config, tags=tc.tags)
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
            "backupRetentionPeriod": (
                7 if tc.backup_retention_period is None else tc.backup_retention_period
            ),
            "skipFinalSnapshot": True,
            "deletionProtection": (
                False if tc.deletion_protection is None else tc.deletion_protection
            ),
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
        TWO_INSTANCES_TC,
        ENGINE_5_TC,
        INSTANCE_CLASS_TC,
        INSTANCE_CLASS_DB_PREFIX_TC,
        DELETION_PROTECTION_TC,
        BACKUP_RETENTION_TC,
        CUSTOM_ROTATION_TC,
        MIN_ROTATION_TC,
        MAX_ROTATION_TC,
        NO_ROTATION_TC,
        CUSTOM_ROTATION_OBJECT_TC,
        NO_ROTATION_DICT_TC,
        CONFIG_OBJECT_TC,
        CONFIG_DICT_TC,
        TAGS_TC,
        *(
            replace(
                DEFAULT_TC,
                test_id=f"retention-{days}",
                backup_retention_period=days,
            )
            for days in (1, 35)
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


def test_two_document_dbs_share_the_app_security_group(pulumi_mocks):
    @pulumi.runtime.test
    def deploy():
        vpc = Vpc(VPC_NAME)
        todos = DocumentDb(DB_NAME, vpc=vpc)
        orders = DocumentDb("orders", vpc=vpc)
        return todos.resources, orders.resources

    deploy()

    for name in (DB_NAME, "orders"):
        pulumi_mocks.assert_res(
            f"{name}-ingress",
            R.SECURITY_GROUP_INGRESS_RULE,
            {
                "securityGroupId": tid(TP + f"{name}-sg"),
                "referencedSecurityGroupId": APP_SG_ID,
                "ipProtocol": "tcp",
                "fromPort": 27017,
                "toPort": 27017,
            },
            partial=True,
        )
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
        )
    )


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


def test_document_db_cluster_security_group_callable_can_append(pulumi_mocks):
    def append_security_group(props: dict[str, Any]) -> dict[str, Any]:
        return props | {"vpc_security_group_ids": [*props["vpc_security_group_ids"], "sg-extra"]}

    @pulumi.runtime.test
    def deploy():
        vpc = Vpc(VPC_NAME)
        return DocumentDb(DB_NAME, vpc=vpc, customize={"cluster": append_security_group}).resources

    deploy()

    pulumi_mocks.assert_res(
        DB_NAME,
        R.DOCDB_CLUSTER,
        {"vpcSecurityGroupIds": [CLUSTER_SG_ID, "sg-extra"]},
        partial=True,
    )
    _assert_app_sg_ingress(pulumi_mocks, DB_NAME)
    pulumi_mocks.assert_res_counts(_counts(VPC_AZ2_COUNTS, APP_SG_COUNTS, DOCDB_COUNTS))


def test_document_db_cluster_security_group_replacement_keeps_caller_ownership(pulumi_mocks):
    @pulumi.runtime.test
    def deploy():
        vpc = Vpc(VPC_NAME)
        return DocumentDb(
            DB_NAME,
            vpc=vpc,
            customize={"cluster": {"vpc_security_group_ids": ["sg-external"]}},
        ).resources

    deploy()

    pulumi_mocks.assert_res(
        DB_NAME,
        R.DOCDB_CLUSTER,
        {"vpcSecurityGroupIds": ["sg-external"]},
        partial=True,
    )
    _assert_app_sg_ingress(pulumi_mocks, DB_NAME)
    pulumi_mocks.assert_res_counts(_counts(VPC_AZ2_COUNTS, APP_SG_COUNTS, DOCDB_COUNTS))


def test_document_db_custom_identifiers_override_generated_prefixes(pulumi_mocks):
    @pulumi.runtime.test
    def deploy():
        vpc = Vpc(VPC_NAME)
        return DocumentDb(
            DB_NAME,
            vpc=vpc,
            customize={
                "cluster": {"cluster_identifier": "custom-cluster"},
                "instance": {"identifier": "custom-instance"},
            },
        ).resources

    deploy()

    cluster = pulumi_mocks.assert_res(DB_NAME, R.DOCDB_CLUSTER)
    assert cluster.inputs["clusterIdentifier"] == "custom-cluster"
    assert "clusterIdentifierPrefix" not in cluster.inputs
    instance = pulumi_mocks.assert_res(f"{DB_NAME}-1", R.DOCDB_INSTANCE)
    assert instance.inputs["identifier"] == "custom-instance"
    assert "identifierPrefix" not in instance.inputs
    pulumi_mocks.assert_res_counts(_counts(VPC_AZ2_COUNTS, APP_SG_COUNTS, DOCDB_COUNTS))


def test_document_db_custom_identifier_prefixes_override_generated_prefixes(pulumi_mocks):
    @pulumi.runtime.test
    def deploy():
        vpc = Vpc(VPC_NAME)
        return DocumentDb(
            DB_NAME,
            vpc=vpc,
            customize={
                "cluster": {"cluster_identifier_prefix": "custom-cluster-"},
                "instance": {"identifier_prefix": "custom-instance-"},
            },
        ).resources

    deploy()

    pulumi_mocks.assert_res(
        DB_NAME,
        R.DOCDB_CLUSTER,
        {"clusterIdentifierPrefix": "custom-cluster-"},
        partial=True,
    )
    pulumi_mocks.assert_res(
        f"{DB_NAME}-1",
        R.DOCDB_INSTANCE,
        {"identifierPrefix": "custom-instance-"},
        partial=True,
    )
    pulumi_mocks.assert_res_counts(_counts(VPC_AZ2_COUNTS, APP_SG_COUNTS, DOCDB_COUNTS))


def test_document_db_customize_parameters_keeps_tls(pulumi_mocks):
    @pulumi.runtime.test
    def deploy():
        vpc = Vpc(VPC_NAME)
        return DocumentDb(
            DB_NAME,
            vpc=vpc,
            customize={
                "parameter_group": {"parameters": [{"name": "ttl_monitor", "value": "enabled"}]}
            },
        ).resources

    deploy()

    pulumi_mocks.assert_res(
        f"{DB_NAME}-parameter-group",
        R.DOCDB_PARAMETER_GROUP,
        {
            "parameters": [
                {"name": "tls", "value": "enabled"},
                {"name": "ttl_monitor", "value": "enabled"},
            ]
        },
        partial=True,
    )
    pulumi_mocks.assert_res_counts(_counts(VPC_AZ2_COUNTS, APP_SG_COUNTS, DOCDB_COUNTS))


def test_document_db_customize_parameters_respects_explicit_tls(pulumi_mocks):
    @pulumi.runtime.test
    def deploy():
        vpc = Vpc(VPC_NAME)
        return DocumentDb(
            DB_NAME,
            vpc=vpc,
            customize={"parameter_group": {"parameters": [{"name": "tls", "value": "disabled"}]}},
        ).resources

    deploy()

    pulumi_mocks.assert_res(
        f"{DB_NAME}-parameter-group",
        R.DOCDB_PARAMETER_GROUP,
        {"parameters": [{"name": "tls", "value": "disabled"}]},
        partial=True,
    )
    pulumi_mocks.assert_res_counts(_counts(VPC_AZ2_COUNTS, APP_SG_COUNTS, DOCDB_COUNTS))


@mark.parametrize("shape", ["list", "entry"])
def test_document_db_customize_deferred_parameters(pulumi_mocks, shape):
    parameter = {"name": "audit_logs", "value": "disabled"}

    @pulumi.runtime.test
    def deploy():
        match shape:
            case "list":
                inputs = pulumi.Output.from_input([parameter])
            case "entry":
                inputs = [pulumi.Output.from_input(parameter)]

        return DocumentDb(
            DB_NAME,
            vpc=Vpc(VPC_NAME),
            customize={"parameter_group": {"parameters": inputs}},
        ).resources

    deploy()

    pulumi_mocks.assert_res(
        f"{DB_NAME}-parameter-group",
        R.DOCDB_PARAMETER_GROUP,
        {"parameters": [{"name": "tls", "value": "enabled"}, parameter]},
        partial=True,
    )
    pulumi_mocks.assert_res_counts(_counts(VPC_AZ2_COUNTS, APP_SG_COUNTS, DOCDB_COUNTS))


def test_document_db_customize_cluster_port_updates_ingress(pulumi_mocks):
    @pulumi.runtime.test
    def deploy():
        vpc = Vpc(VPC_NAME)
        return DocumentDb(DB_NAME, vpc=vpc, customize={"cluster": {"port": 27018}}).resources

    deploy()

    pulumi_mocks.assert_res(DB_NAME, R.DOCDB_CLUSTER, {"port": 27018}, partial=True)
    pulumi_mocks.assert_res(
        f"{DB_NAME}-ingress",
        R.SECURITY_GROUP_INGRESS_RULE,
        {"fromPort": 27018, "toPort": 27018, "ipProtocol": "tcp"},
        partial=True,
    )
    pulumi_mocks.assert_res_counts(_counts(VPC_AZ2_COUNTS, APP_SG_COUNTS, DOCDB_COUNTS))


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


def test_document_db_resources_exposes_created_resources(pulumi_mocks):
    @pulumi.runtime.test
    def deploy():
        vpc = Vpc(VPC_NAME)
        r = DocumentDb(DB_NAME, vpc=vpc, instances=2).resources

        assert isinstance(r.instances, list)
        assert {f.name for f in fields(r)} == {
            "cluster",
            "instances",
            "subnet_group",
            "parameter_group",
            "security_group",
        }
        exposed = [r.cluster, *r.instances, r.subnet_group, r.parameter_group, r.security_group]
        expected = [
            DB_NAME,
            f"{DB_NAME}-1",
            f"{DB_NAME}-2",
            f"{DB_NAME}-subnet-group",
            f"{DB_NAME}-parameter-group",
            CLUSTER_SG_NAME,
        ]

        def check(ids):
            assert ids == [tid(TP + name) for name in expected]

        return pulumi.Output.all(*[res.id for res in exposed]).apply(check)

    deploy()
    pulumi_mocks.assert_res_counts(
        _counts(VPC_AZ2_COUNTS, APP_SG_COUNTS, DOCDB_COUNTS | {R.DOCDB_INSTANCE: 2})
    )


def test_document_db_resources_parented_to_document_db_component(pulumi_mocks):
    @pulumi.runtime.test
    def deploy():
        ingress = []
        secret_rotation = []

        def capture(args):
            if args.type_ == R.SECURITY_GROUP_INGRESS_RULE:
                ingress.append(args.resource)
            if args.type_ == R.SECRET_ROTATION:
                secret_rotation.append(args.resource)

        # A public transformation observes hidden resources without changing their options.
        pulumi.runtime.register_stack_transformation(capture)
        vpc = Vpc(VPC_NAME)
        r = DocumentDb(DB_NAME, vpc=vpc).resources
        assert len(ingress) == 1
        assert len(secret_rotation) == 1
        children = [r.cluster, *r.instances, r.subnet_group, r.parameter_group, r.security_group]
        app_sg = vpc._app_security_group

        def check(urns):
            assert urns == [
                "urn:pulumi:stack::project::stelvio:aws:DocumentDb$aws:docdb/cluster:Cluster::test-test-todos",
                "urn:pulumi:stack::project::stelvio:aws:DocumentDb$aws:docdb/clusterInstance:ClusterInstance::test-test-todos-1",
                "urn:pulumi:stack::project::stelvio:aws:DocumentDb$aws:docdb/subnetGroup:SubnetGroup::test-test-todos-subnet-group",
                "urn:pulumi:stack::project::stelvio:aws:DocumentDb$aws:docdb/clusterParameterGroup:ClusterParameterGroup::test-test-todos-parameter-group",
                "urn:pulumi:stack::project::stelvio:aws:DocumentDb$aws:ec2/securityGroup:SecurityGroup::test-test-todos-sg",
                "urn:pulumi:stack::project::stelvio:aws:DocumentDb$aws:vpc/securityGroupIngressRule:SecurityGroupIngressRule::test-test-todos-ingress",
                "urn:pulumi:stack::project::stelvio:aws:DocumentDb$aws:secretsmanager/secretRotation:SecretRotation::test-test-todos-secret-rotation",
                "urn:pulumi:stack::project::stelvio:aws:Vpc$aws:ec2/securityGroup:SecurityGroup::test-test-main_vpc-app-sg",
            ]

        return pulumi.Output.all(
            *[res.urn for res in [*children, *ingress, *secret_rotation]], app_sg.urn
        ).apply(check)

    deploy()
    pulumi_mocks.assert_res_counts(_counts(VPC_AZ2_COUNTS, APP_SG_COUNTS, DOCDB_COUNTS))


def test_document_db_config_dict_matches_dataclass():
    assert_config_dict_matches_dataclass(DocumentDbConfig, DocumentDbConfigDict)


def _connection_uri(host: str, *, port: object = 27017, tls: bool = True) -> str:
    query = "replicaSet=rs0&retryWrites=false"
    if tls:
        ca_file = quote_plus(DOCDB_CA_ZIP_PATH)
        query = f"tls=true&tlsCAFile={ca_file}&{query}"
    return f"mongodb://{host}:{port}/?{query}"


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
        f"{prefix}CONNECTION_URI": _connection_uri(host),
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


def _assert_ca_packaged(pulumi_mocks, fn_name: str) -> None:
    ca = pulumi_mocks.assert_res(fn_name, R.FUNCTION).inputs["code"].assets[DOCDB_CA_ZIP_PATH]
    assert isinstance(ca, FileAsset)
    path = Path(ca.path).resolve()
    assert path == DOCDB_CA_VENDORED_PATH.resolve()
    assert path.read_bytes().startswith(b"-----BEGIN CERTIFICATE-----")
    assert path.stat().st_size > 100_000


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


def test_document_db_link(pulumi_mocks):
    @pulumi.runtime.test
    def deploy():
        db = DocumentDb(DB_NAME, vpc=Vpc(VPC_NAME))
        link = db.link()
        assert link.component is db
        assert _overridden_link(db).component is db
        permissions = list(link.permissions)
        assert len(permissions) == 1
        assert list(permissions[0].actions) == ["secretsmanager:GetSecretValue"]

        def check(args):
            properties, resources = args
            assert properties == {
                "host": DOCDB_HOST,
                "reader_host": DOCDB_READER_HOST,
                "port": "27017",
                "username": "stelvio",
                "secret_arn": DOCDB_SECRET_ARN,
                "replica_set": "rs0",
                "ca_file": DOCDB_CA_ZIP_PATH,
                "connection_uri": _connection_uri(DOCDB_HOST),
            }
            assert resources == [DOCDB_SECRET_ARN]

        return pulumi.Output.all(link.properties, permissions[0].resources).apply(check)

    deploy()
    pulumi_mocks.assert_res_counts(_counts(VPC_AZ2_COUNTS, APP_SG_COUNTS, DOCDB_COUNTS))


_LINK_FORMS = [
    param(lambda db: db, id="component"),
    param(lambda db: Link(DB_NAME, {}, [], component=db), id="link-override"),
]


@mark.parametrize("as_link", _LINK_FORMS)
def test_document_db_link_without_vpc_raises(as_link):
    db = DocumentDb(DB_NAME, vpc=Vpc(VPC_NAME))

    with raises(
        ValueError,
        match=re.escape(
            f"Function 'client' links DocumentDb '{DB_NAME}' but has no vpc=. "
            f"Set vpc= to Vpc '{VPC_NAME}'; linking is not networking."
        ),
    ):
        Function("client", handler=SIMPLE_HANDLER, links=[as_link(db)])


@mark.parametrize("as_link", _LINK_FORMS)
def test_document_db_link_with_different_vpc_raises(as_link):
    db = DocumentDb(DB_NAME, vpc=Vpc(VPC_NAME))

    with raises(
        ValueError,
        match=re.escape(
            f"Function 'client' links DocumentDb '{DB_NAME}' in Vpc '{VPC_NAME}' "
            f"but is attached to Vpc 'other'. Set vpc= to Vpc '{VPC_NAME}'; "
            "linking is not networking."
        ),
    ):
        Function("client", handler=SIMPLE_HANDLER, vpc=Vpc("other"), links=[as_link(db)])


def test_document_db_link_with_vpc_uses_app_security_group(pulumi_mocks, project_cwd):
    @pulumi.runtime.test
    def deploy():
        vpc = Vpc(VPC_NAME)
        db = DocumentDb(DB_NAME, vpc=vpc)
        fn = Function("client", handler=SIMPLE_HANDLER, vpc=vpc, links=[db])
        return db.resources, fn.resources

    deploy()

    _assert_function_document_db_link(pulumi_mocks, "client", DB_NAME)
    pulumi_mocks.assert_res(
        "client",
        R.FUNCTION,
        {"vpcConfig": {"subnetIds": PRIVATE_SUBNET_IDS, "securityGroupIds": [APP_SG_ID]}},
        partial=True,
    )
    _assert_app_sg_ingress(pulumi_mocks, DB_NAME)
    _assert_ca_packaged(pulumi_mocks, "client")
    pulumi_mocks.assert_res_counts(
        _counts(VPC_AZ2_COUNTS, APP_SG_COUNTS, DOCDB_COUNTS, FUNCTION_VPC_LINKED_COUNTS)
    )


def test_document_db_link_with_permissions_still_packages_ca(pulumi_mocks, project_cwd):
    @pulumi.runtime.test
    def deploy():
        vpc = Vpc(VPC_NAME)
        db = DocumentDb(DB_NAME, vpc=vpc)
        fn = Function("client", handler=SIMPLE_HANDLER, vpc=vpc, links=[_overridden_link(db)])
        return db.resources, fn.resources

    deploy()

    pulumi_mocks.assert_res(
        "client",
        R.FUNCTION,
        {"vpcConfig": {"subnetIds": PRIVATE_SUBNET_IDS, "securityGroupIds": [APP_SG_ID]}},
        partial=True,
    )
    _assert_ca_packaged(pulumi_mocks, "client")
    policy = pulumi_mocks.assert_res("client-p", R.POLICY)
    assert json.loads(policy.inputs["policy"]) == [
        {"actions": ["secretsmanager:GetSecretValue"], "resources": ["*"]}
    ]
    assert _db_env_vars(pulumi_mocks, "client", DB_NAME) == _link_env_vars(DB_NAME)
    pulumi_mocks.assert_res_counts(
        _counts(VPC_AZ2_COUNTS, APP_SG_COUNTS, DOCDB_COUNTS, FUNCTION_VPC_LINKED_COUNTS)
    )


def test_function_linked_to_document_db_in_isolated_subnets(pulumi_mocks, project_cwd):
    @pulumi.runtime.test
    def deploy():
        vpc = Vpc(VPC_NAME)
        db = DocumentDb(DB_NAME, vpc=vpc)
        fn = Function(
            "client",
            handler=SIMPLE_HANDLER,
            vpc=VpcAttachment(vpc=vpc, subnets="isolated"),
            links=[db],
        )
        return db.resources, fn.resources

    deploy()

    _assert_function_document_db_link(pulumi_mocks, "client", DB_NAME)
    pulumi_mocks.assert_res(
        "client",
        R.FUNCTION,
        {"vpcConfig": {"subnetIds": ISOLATED_SUBNET_IDS, "securityGroupIds": [APP_SG_ID]}},
        partial=True,
    )
    _assert_ca_packaged(pulumi_mocks, "client")
    pulumi_mocks.assert_res_counts(
        _counts(VPC_AZ2_COUNTS, APP_SG_COUNTS, DOCDB_COUNTS, FUNCTION_VPC_LINKED_COUNTS)
    )


@mark.parametrize(
    "security_groups",
    [param(["sg-123"], id="one-group"), param(["sg-a", "sg-b"], id="two-groups")],
)
def test_function_linked_to_document_db_with_custom_security_groups(
    pulumi_mocks, project_cwd, security_groups
):
    @pulumi.runtime.test
    def deploy():
        vpc = Vpc(VPC_NAME)
        db = DocumentDb(DB_NAME, vpc=vpc)
        fn = Function(
            "client",
            handler=SIMPLE_HANDLER,
            vpc=VpcAttachment(vpc=vpc, security_groups=security_groups),
            links=[db],
        )
        return db.resources, fn.resources

    deploy()

    _assert_function_document_db_link(pulumi_mocks, "client", DB_NAME)
    pulumi_mocks.assert_res(
        "client",
        R.FUNCTION,
        {"vpcConfig": {"subnetIds": PRIVATE_SUBNET_IDS, "securityGroupIds": security_groups}},
        partial=True,
    )
    _assert_app_sg_ingress(pulumi_mocks, DB_NAME)
    _assert_ca_packaged(pulumi_mocks, "client")
    pulumi_mocks.assert_res_counts(
        _counts(VPC_AZ2_COUNTS, APP_SG_COUNTS, DOCDB_COUNTS, FUNCTION_VPC_LINKED_COUNTS)
    )


def test_function_without_document_db_link_does_not_package_ca(pulumi_mocks, project_cwd):
    @pulumi.runtime.test
    def deploy():
        return Function("client", handler=SIMPLE_HANDLER).resources

    deploy()

    assert (
        DOCDB_CA_ZIP_PATH
        not in pulumi_mocks.assert_res("client", R.FUNCTION).inputs["code"].assets
    )
    pulumi_mocks.assert_res_counts({R.FUNCTION: 1, R.ROLE: 1, R.ROLE_POLICY_ATTACHMENT: 1})


def test_two_functions_linked_to_one_document_db(pulumi_mocks, project_cwd):
    @pulumi.runtime.test
    def deploy():
        vpc = Vpc(VPC_NAME)
        db = DocumentDb(DB_NAME, vpc=vpc)
        reader = Function("reader", handler=SIMPLE_HANDLER, vpc=vpc, links=[db])
        writer = Function("writer", handler=SIMPLE_HANDLER, vpc=vpc, links=[db])
        return db.resources, reader.resources, writer.resources

    deploy()

    for fn_name in ("reader", "writer"):
        _assert_function_document_db_link(pulumi_mocks, fn_name, DB_NAME)
        fn_res = pulumi_mocks.assert_res(fn_name, R.FUNCTION)
        assert fn_res.inputs["vpcConfig"]["securityGroupIds"] == [APP_SG_ID]
        _assert_ca_packaged(pulumi_mocks, fn_name)
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
                "STLV_TODOS_CONNECTION_URI": _connection_uri(DOCDB_HOST, port=27018),
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


def test_document_db_link_raises_without_managed_master_password(pulumi_mocks):
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


@mark.parametrize(
    ("tls_value", "tls_in_uri"),
    [
        param(None, True, id="default-tls"),
        param("enabled", True, id="explicit-enabled"),
        param("disabled", False, id="disabled"),
    ],
)
def test_document_db_connection_uri_follows_tls_parameter(
    pulumi_mocks, project_cwd, tls_value, tls_in_uri
):
    @pulumi.runtime.test
    def deploy():
        vpc = Vpc(VPC_NAME)
        customize = None
        if tls_value is not None:
            customize = {"parameter_group": {"parameters": [{"name": "tls", "value": tls_value}]}}
        db = DocumentDb(DB_NAME, vpc=vpc, customize=customize)
        return Function("client", handler=SIMPLE_HANDLER, vpc=vpc, links=[db]).resources

    deploy()

    env = _db_env_vars(pulumi_mocks, "client", DB_NAME)
    assert env["STLV_TODOS_CONNECTION_URI"] == _connection_uri(DOCDB_HOST, tls=tls_in_uri)
    pulumi_mocks.assert_res_counts(
        _counts(VPC_AZ2_COUNTS, APP_SG_COUNTS, DOCDB_COUNTS, FUNCTION_VPC_LINKED_COUNTS)
    )


def test_http_api_route_linked_to_document_db(pulumi_mocks, project_cwd):
    @pulumi.runtime.test
    def deploy():
        vpc = Vpc(VPC_NAME)
        db = DocumentDb(DB_NAME, vpc=vpc)
        api = HttpApi("api")
        api.route("GET", "/todos", SIMPLE_HANDLER, vpc=vpc, links=[db])
        return api.resources, db.resources

    deploy()

    fn_name = "api-functions-simple_handler"
    _assert_function_document_db_link(pulumi_mocks, fn_name, DB_NAME)
    pulumi_mocks.assert_res(
        fn_name,
        R.FUNCTION,
        {"vpcConfig": {"subnetIds": PRIVATE_SUBNET_IDS, "securityGroupIds": [APP_SG_ID]}},
        partial=True,
    )
    _assert_app_sg_ingress(pulumi_mocks, DB_NAME)
    pulumi_mocks.assert_res_counts(
        _counts(
            VPC_AZ2_COUNTS,
            APP_SG_COUNTS,
            DOCDB_COUNTS,
            FUNCTION_VPC_LINKED_COUNTS,
            {
                R.HTTP_API: 1,
                R.API_ACCOUNT: 2,
                R.ROLE: 1,
                R.LOG_GROUP: 1,
                R.HTTP_API_STAGE: 1,
                R.HTTP_API_INTEGRATION: 1,
                R.HTTP_API_ROUTE: 1,
                R.LAMBDA_PERMISSION: 1,
            },
        )
    )


@mark.parametrize(
    "case",
    [
        param(("omitted", False, None, "db.r6g.large"), id="global-default"),
        param((None, False, None, "db.r6g.large"), id="explicit-none"),
        param(("t4g.medium", False, None, "db.t4g.medium"), id="explicit-default"),
        param(("t4g.large", False, None, "db.t4g.large"), id="explicit-value"),
        param(("db.t4g.large", False, None, "db.t4g.large"), id="explicit-db-prefix"),
        param(("t4g.medium", True, None, "db.r6g.large"), id="global-callable"),
        param((None, False, "db.r5.large", "db.r5.large"), id="local-dict"),
        param((None, True, "db.r5.large", "db.r5.large"), id="local-over-global-callable"),
    ],
)
def test_document_db_instance_customization_precedence(pulumi_mocks, case):
    instance, global_callable, local, expected = case

    global_props = {"instance_class": "db.r6g.large"}
    customization = (lambda props: props | global_props) if global_callable else global_props
    _set_app_context(customize={DocumentDb: {"instance": customization}})

    @pulumi.runtime.test
    def deploy():
        opts = {"vpc": Vpc(VPC_NAME)}
        if instance != "omitted":
            opts["instance_class"] = instance
        customize = {"instance": {"instance_class": local}} if local else None
        return DocumentDb(DB_NAME, customize=customize, **opts).resources

    deploy()
    verify_document_db(pulumi_mocks, replace(DEFAULT_TC, aws_instance_class=expected))


@mark.parametrize(
    "case",
    [
        param(({}, False, None, True, 14), id="global-default"),
        param(
            (
                {"deletion_protection": None, "backup_retention_period": None},
                False,
                None,
                True,
                14,
            ),
            id="explicit-none",
        ),
        param(
            ({"deletion_protection": False, "backup_retention_period": 7}, False, None, False, 7),
            id="explicit-defaults",
        ),
        param(
            ({"deletion_protection": True, "backup_retention_period": 35}, False, None, True, 35),
            id="explicit-values",
        ),
        param(
            ({"deletion_protection": False, "backup_retention_period": 7}, True, None, True, 14),
            id="global-callable",
        ),
        param(
            (
                {"deletion_protection": True, "backup_retention_period": 7},
                False,
                {"deletion_protection": False, "backup_retention_period": 1},
                False,
                1,
            ),
            id="local-over-constructor",
        ),
        param(
            (
                {"deletion_protection": False, "backup_retention_period": 7},
                True,
                {"deletion_protection": False, "backup_retention_period": 35},
                False,
                35,
            ),
            id="local-over-global-callable",
        ),
    ],
)
def test_document_db_cluster_customization_precedence(pulumi_mocks, case):
    opts, global_callable, local, expected_protection, expected_retention = case
    global_props = {"deletion_protection": True, "backup_retention_period": 14}
    customization = (lambda props: props | global_props) if global_callable else global_props
    _set_app_context(customize={DocumentDb: {"cluster": customization}})

    @pulumi.runtime.test
    def deploy():
        config = {"vpc": Vpc(VPC_NAME), **opts}
        customize = {"cluster": local} if local is not None else None
        return DocumentDb(DB_NAME, customize=customize, **config).resources

    deploy()
    verify_document_db(
        pulumi_mocks,
        replace(
            DEFAULT_TC,
            deletion_protection=expected_protection,
            backup_retention_period=expected_retention,
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
