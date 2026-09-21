import json
import re
from collections import Counter
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any, Literal
from unittest.mock import Mock
from urllib.parse import quote_plus

import pulumi
from pulumi import FileAsset
from pulumi_aws.docdb import ClusterArgs, ClusterParameterGroupParameterArgs
from pytest import fixture, mark, param, raises

from stelvio.aws.api_gateway import HttpApi
from stelvio.aws.document_db import (
    DocumentDb,
    DocumentDbConfig,
    DocumentDbConfigDict,
    _SecretRotationDisabledProvider,
)
from stelvio.aws.function import Function
from stelvio.aws.permission import AwsPermission
from stelvio.aws.vpc import Vpc, VpcAttachment
from stelvio.config import AwsConfig
from stelvio.context import AppContext, _ContextStore
from tests.aws.pulumi_mocks import (
    ACCOUNT_ID,
    DEFAULT_REGION,
    DOCDB_MOCK_SECRET_PASSWORD,
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
_FAKE_CA_PEM = b"-----BEGIN CERTIFICATE-----\nMIIBfake\n-----END CERTIFICATE-----\n"
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
DOCDB_NO_ROTATION_COUNTS = {
    typ: count for typ, count in DOCDB_COUNTS.items() if typ != R.SECRET_ROTATION
}
DOCDB_DISABLED_ROTATION_COUNTS = DOCDB_NO_ROTATION_COUNTS | {
    R.SECRET_ROTATION_DISABLED: 1
}


@fixture(autouse=True)
def mock_docdb_ca_urlopen(monkeypatch):
    """Keep DocumentDB Function tests off the network; record download URLs."""
    calls: list[str] = []

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def read(self) -> bytes:
            return _FAKE_CA_PEM

    def fake_urlopen(url: str, **_kwargs: object) -> _Resp:
        calls.append(url)
        return _Resp()

    monkeypatch.setattr("stelvio.aws.document_db.urlopen", fake_urlopen)
    return calls


def _counts(*parts: dict[R, int]) -> dict[R, int]:
    total: Counter[R] = Counter()
    for part in parts:
        total.update(part)
    return dict(total)


@mark.parametrize(
    ("opts", "error_type", "error_message"),
    [
        param(
            {"vpc": "nope"},
            TypeError,
            "`vpc` must be a Vpc instance, got str",
            id="vpc-str",
        ),
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
            id="instances-too-high",
        ),
        param(
            {"instances": True},
            TypeError,
            "`instances` must be an int, got bool",
            id="instances-bool",
        ),
        param(
            {"instances": 1.5},
            TypeError,
            "`instances` must be an int, got float",
            id="instances-float",
        ),
        param(
            {"instances": "2"},
            TypeError,
            "`instances` must be an int, got str",
            id="instances-str",
        ),
        param(
            {"instance_class": 123},
            TypeError,
            "`instance_class` must be a str, got int",
            id="instance-class-int",
        ),
        param(
            {"instance_class": False},
            TypeError,
            "`instance_class` must be a str, got bool",
            id="instance-class-bool",
        ),
        param(
            {"instance_class": ""},
            ValueError,
            "`instance_class` must be a non-empty string, got ''",
            id="instance-class-empty",
        ),
        param(
            {"instance_class": "db.t4g"},
            ValueError,
            "`instance_class` must be family.size (e.g. 't4g.medium' or 'db.t4g.medium'), "
            "got 'db.t4g'",
            id="instance-class-db-prefix-incomplete",
        ),
        param(
            {"instance_class": "t4g"},
            ValueError,
            "`instance_class` must be family.size (e.g. 't4g.medium' or 'db.t4g.medium'), "
            "got 't4g'",
            id="instance-class-no-size",
        ),
        param(
            {"instance_class": "t4g.MEDIUM"},
            ValueError,
            "`instance_class` must be family.size (e.g. 't4g.medium' or 'db.t4g.medium'), "
            "got 't4g.MEDIUM'",
            id="instance-class-uppercase",
        ),
        param(
            {"engine": "4.0"},
            ValueError,
            "`engine` must be '5.0' or '8.0', got '4.0'",
            id="engine-4",
        ),
        param(
            {"engine": "5"},
            ValueError,
            "`engine` must be '5.0' or '8.0', got '5'",
            id="engine-5",
        ),
        param(
            {"engine": "5.0.0"},
            ValueError,
            "`engine` must be '5.0' or '8.0', got '5.0.0'",
            id="engine-5-patch",
        ),
        param(
            {"engine": "8.0.0"},
            ValueError,
            "`engine` must be '5.0' or '8.0', got '8.0.0'",
            id="engine-8-patch",
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
            {"secret_rotation": True},
            TypeError,
            "`secret_rotation` must be False or an int, got bool",
            id="secret-rotation-true",
        ),
        param(
            {"secret_rotation": 0},
            ValueError,
            "`secret_rotation` must be between 1 and 1000, or False, got 0",
            id="secret-rotation-zero",
        ),
        param(
            {"secret_rotation": -1},
            ValueError,
            "`secret_rotation` must be between 1 and 1000, or False, got -1",
            id="secret-rotation-negative",
        ),
        param(
            {"secret_rotation": 1001},
            ValueError,
            "`secret_rotation` must be between 1 and 1000, or False, got 1001",
            id="secret-rotation-too-high",
        ),
        param(
            {"secret_rotation": 1.5},
            TypeError,
            "`secret_rotation` must be False or an int, got float",
            id="secret-rotation-float",
        ),
        param(
            {"secret_rotation": "7"},
            TypeError,
            "`secret_rotation` must be False or an int, got str",
            id="secret-rotation-string",
        ),
        param(
            {"secret_rotation": None},
            TypeError,
            "`secret_rotation` must be False or an int, got NoneType",
            id="secret-rotation-none",
        ),
        param(
            {"backup_retention_period": 36},
            ValueError,
            "`backup_retention_period` must be between 1 and 35, got 36",
            id="backup-retention-36",
        ),
        param(
            {"backup_retention_period": True},
            TypeError,
            "`backup_retention_period` must be an int, got bool",
            id="backup-retention-bool",
        ),
        param(
            {"backup_retention_period": "7"},
            TypeError,
            "`backup_retention_period` must be an int, got str",
            id="backup-retention-str",
        ),
    ],
)
def test_document_db_raises_when_kwargs_invalid(opts, error_type, error_message):
    vpc = Vpc(VPC_NAME)
    kwargs = opts if "vpc" in opts else {"vpc": vpc, **opts}
    with raises(error_type, match=f"^{re.escape(error_message)}$"):
        DocumentDb(DB_NAME, **kwargs)


def test_document_db_raises_when_vpc_is_attachment():
    vpc = Vpc(VPC_NAME)
    with raises(TypeError, match=re.escape("`vpc` must be a Vpc instance, got VpcAttachment")):
        DocumentDb(DB_NAME, vpc=VpcAttachment(vpc=vpc))


def test_document_db_raises_when_vpc_is_dict():
    vpc = Vpc(VPC_NAME)
    with raises(TypeError, match=re.escape("`vpc` must be a Vpc instance, got dict")):
        DocumentDb(DB_NAME, vpc={"vpc": vpc})


def test_document_db_raises_when_vpc_is_none():
    with raises(TypeError, match=re.escape("`vpc` must be a Vpc instance, got NoneType")):
        DocumentDb(DB_NAME, vpc=None)


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


def test_document_db_name_beyond_old_prefix_limit_still_deploys(pulumi_mocks):
    name = "a" * 25  # 35 chars before the generated separator; previously rejected at 34

    @pulumi.runtime.test
    def deploy():
        return DocumentDb(name, vpc=Vpc(VPC_NAME)).resources

    deploy()

    cluster = pulumi_mocks.assert_res(name, R.DOCDB_CLUSTER)
    assert cluster.inputs["clusterIdentifierPrefix"] == TP + name + "-"
    assert len(cluster.inputs["clusterIdentifierPrefix"]) == 36
    assert "clusterIdentifier" not in cluster.inputs


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
    _ContextStore.clear()
    _ContextStore.set(
        AppContext(
            name=app,
            env=env,
            aws=AwsConfig(profile="default", region="us-east-1"),
            home="aws",
            customize={},
        )
    )

    @pulumi.runtime.test
    def deploy():
        return DocumentDb(DB_NAME, vpc=Vpc(VPC_NAME)).resources

    deploy()

    pulumi_name = f"{app.lower()}-{env.lower()}-{DB_NAME}"
    cluster = pulumi_mocks.assert_res(pulumi_name, R.DOCDB_CLUSTER, prefixed=False)
    assert cluster.inputs["clusterIdentifierPrefix"] == identifier + "-"
    instance = pulumi_mocks.assert_res(
        f"{pulumi_name}-1", R.DOCDB_INSTANCE, prefixed=False
    )
    assert instance.inputs["identifierPrefix"] == identifier + "-1-"
    pulumi_mocks.assert_res_counts(_counts(VPC_AZ2_COUNTS, APP_SG_COUNTS, DOCDB_COUNTS))


def test_document_db_long_app_env_uses_safe_name(pulumi_mocks):
    app, env, name = "a" * 20, "b" * 20, "c" * 30
    _ContextStore.clear()
    _ContextStore.set(
        AppContext(
            name=app,
            env=env,
            aws=AwsConfig(profile="default", region="us-east-1"),
            home="aws",
            customize={},
        )
    )
    pulumi_name = "aaaaaaaaaaaaaaaaaaaa-bbbbbbbbbbbbbbbbbbbb-" + "c" * 30
    legacy_name = "aaaaaaaaaaaaaaaaaaaa-bbbbbbbbbbbbbbbbbbbb-ccccc-489105d"
    aliases: dict[str, list[str]] = {}

    @pulumi.runtime.test
    def deploy():
        def capture(args):
            if args.type_ in (R.DOCDB_CLUSTER, R.DOCDB_INSTANCE):
                aliases[args.name] = [
                    alias.name for alias in args.opts.aliases or [] if alias.name
                ]

        pulumi.runtime.register_stack_transformation(capture)
        return DocumentDb(name, vpc=Vpc(VPC_NAME)).resources

    deploy()

    cluster = pulumi_mocks.assert_res(pulumi_name, R.DOCDB_CLUSTER, prefixed=False)
    prefix = cluster.inputs["clusterIdentifierPrefix"]
    assert prefix == "aaaaaaaaaaaaaaaaaaaa-bbbbbbb-daf89e4-"
    assert "_" not in prefix
    assert legacy_name in aliases[pulumi_name]
    assert (
        "aaaaaaaaaaaaaaaaaaaa-bbbbbbbbbbbbbbbbbbbb-ccc-489105d-1"
        in aliases[pulumi_name + "-1"]
    )
    pulumi_mocks.assert_res(
        pulumi_name + "-1", R.DOCDB_INSTANCE, prefixed=False, partial=True
    )
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
    assert "--" not in identifier
    pulumi_mocks.assert_res_counts(_counts(VPC_AZ2_COUNTS, APP_SG_COUNTS, DOCDB_COUNTS))


@mark.parametrize(
    "kwargs",
    [param({}, id="kwargs"), param({"config": {}}, id="config-dict")],
)
def test_document_db_raises_when_vpc_missing(kwargs):
    with raises(TypeError, match=re.escape("DocumentDb 'todos' requires vpc=")):
        DocumentDb(DB_NAME, **kwargs)


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
            {"backup_retention_period": 0},
            ValueError,
            "`backup_retention_period` must be between 1 and 35, got 0",
            id="backup-retention-zero",
        ),
    ],
)
def test_document_db_raises_when_config_dict_values_invalid(opts, error_type, error_message):
    vpc = Vpc(VPC_NAME)
    with raises(error_type, match=re.escape(error_message)):
        DocumentDb(DB_NAME, config={"vpc": vpc, **opts})


@mark.parametrize("secret_rotation", [1, 1000, False], ids=["minimum", "maximum", "disabled"])
def test_document_db_accepts_secret_rotation_boundaries(secret_rotation):
    db = DocumentDb(DB_NAME, vpc=Vpc(VPC_NAME), secret_rotation=secret_rotation)
    assert db.config.secret_rotation == secret_rotation


@mark.parametrize("rotation_enabled", [True, False], ids=["enabled", "already-disabled"])
def test_document_db_disabled_rotation_provider_enforces_state(monkeypatch, rotation_enabled):
    # Pulumi mocks record the dynamic resource but cannot observe its AWS-side effect.
    client = Mock()
    client.describe_secret.return_value = {"RotationEnabled": rotation_enabled}
    session = Mock()
    session.client.return_value = client
    session_factory = Mock(return_value=session)
    monkeypatch.setattr("stelvio.aws.document_db.boto3.Session", session_factory)

    provider = _SecretRotationDisabledProvider("us-east-1", "default")
    secret_id = "arn:aws:secretsmanager:us-east-1:1:secret:test"  # noqa: S105 - test ARN
    result = provider.create({"secret_id": secret_id})

    assert result.id == secret_id
    session_factory.assert_called_once_with(region_name="us-east-1", profile_name="default")
    if rotation_enabled:
        client.cancel_rotate_secret.assert_called_once_with(SecretId=secret_id)
    else:
        client.cancel_rotate_secret.assert_not_called()


def test_document_db_disabled_rotation_provider_reads_drift_and_replaces_secret(monkeypatch):
    client = Mock()
    client.describe_secret.return_value = {"RotationEnabled": True}
    session = Mock()
    session.client.return_value = client
    monkeypatch.setattr("stelvio.aws.document_db.boto3.Session", Mock(return_value=session))

    provider = _SecretRotationDisabledProvider("us-east-1", None)
    secret_id = "arn:aws:secretsmanager:us-east-1:1:secret:test"  # noqa: S105 - test ARN
    read = provider.read(secret_id, {"secret_id": secret_id, "rotation_enabled": False})

    assert read.id == secret_id
    assert read.outs == {"secret_id": secret_id, "rotation_enabled": True}
    assert read.inputs == {"secret_id": secret_id, "rotation_enabled": True}
    assert provider.diff(
        secret_id,
        read.inputs or {},
        {"secret_id": secret_id, "rotation_enabled": False},
    ).changes
    assert provider.diff(
        secret_id,
        {"secret_id": secret_id, "rotation_enabled": False},
        {"secret_id": "arn:aws:secretsmanager:us-east-1:1:secret:new", "rotation_enabled": False},
    ).replaces == ["secret_id"]
    client.cancel_rotate_secret.assert_not_called()


def test_document_db_raises_when_config_dict_invalid():
    vpc = Vpc(VPC_NAME)
    with raises(ValueError, match=re.escape("`engine` must be '5.0' or '8.0', got '4.0'")):
        DocumentDb(DB_NAME, config={"vpc": vpc, "engine": "4.0"})


def test_document_db_raises_when_config_and_kwargs_combined():
    vpc = Vpc(VPC_NAME)
    with raises(ValueError, match="cannot combine 'config' parameter with additional options"):
        DocumentDb(DB_NAME, config=DocumentDbConfig(vpc=vpc), instances=2)


def test_document_db_raises_when_config_type_invalid():
    with raises(TypeError, match="expected DocumentDbConfig or DocumentDbConfigDict"):
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

    Expectations are literal values, never computed from inputs — a case is a
    full spec: `verify_document_db` asserts everything it declares plus sealed counts.
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
    expected_instance_count: int = 1


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
    expected_instance_count=2,
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
    if tc.secret_rotation is False:
        pulumi_mocks.assert_no_res(R.SECRET_ROTATION)
        pulumi_mocks.assert_res(
            f"{DB_NAME}-secret-rotation-disabled",
            R.SECRET_ROTATION_DISABLED,
            {"secret_id": DOCDB_SECRET_ARN, "rotation_enabled": False},
            partial=True,
        )
    else:
        pulumi_mocks.assert_res(
            f"{DB_NAME}-secret-rotation",
            R.SECRET_ROTATION,
            {
                "secretId": DOCDB_SECRET_ARN,
                "rotationRules": {"automaticallyAfterDays": tc.secret_rotation},
                "rotateImmediately": False,
            },
            partial=True,
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
            (DOCDB_COUNTS if tc.secret_rotation is not False else DOCDB_DISABLED_ROTATION_COUNTS)
            | {R.DOCDB_INSTANCE: tc.expected_instance_count},
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
        NO_ROTATION_TC,
        CUSTOM_ROTATION_OBJECT_TC,
        NO_ROTATION_DICT_TC,
        CONFIG_OBJECT_TC,
        CONFIG_DICT_TC,
        TAGS_TC,
        *(
            replace(
                DEFAULT_TC,
                test_id=f"retention-{days}-{style}",
                style=style,
                backup_retention_period=days,
            )
            for days in (1, 35)
            for style in ("kwargs", "dict", "object")
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
        return props | {
            "vpc_security_group_ids": [*props["vpc_security_group_ids"], "sg-extra"]
        }

    @pulumi.runtime.test
    def deploy():
        vpc = Vpc(VPC_NAME)
        return DocumentDb(
            DB_NAME, vpc=vpc, customize={"cluster": append_security_group}
        ).resources

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


@mark.parametrize("shape", ["list", "entry", "name", "args", "awaitable"])
@mark.parametrize("explicit_tls", [False, True])
def test_document_db_customize_deferred_parameters(pulumi_mocks, shape, explicit_tls):
    parameter = {"name": "tls" if explicit_tls else "audit_logs", "value": "disabled"}

    @pulumi.runtime.test
    def deploy():
        async def parameters():
            return [parameter]

        match shape:
            case "list":
                inputs = pulumi.Output.from_input([parameter])
            case "entry":
                inputs = [pulumi.Output.from_input(parameter)]
            case "name":
                inputs = [parameter | {"name": pulumi.Output.from_input(parameter["name"])}]
            case "args":
                inputs = [
                    ClusterParameterGroupParameterArgs(
                        name=pulumi.Output.from_input(parameter["name"]), value="disabled"
                    )
                ]
            case "awaitable":
                inputs = parameters()

        return DocumentDb(
            DB_NAME,
            vpc=Vpc(VPC_NAME),
            customize={"parameter_group": {"parameters": inputs}},
        ).resources

    deploy()

    expected = [parameter] if explicit_tls else [{"name": "tls", "value": "enabled"}, parameter]
    pulumi_mocks.assert_res(
        f"{DB_NAME}-parameter-group",
        R.DOCDB_PARAMETER_GROUP,
        {"parameters": expected},
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
            "rotationRules": {"automaticallyAfterDays": 30},
            "rotateImmediately": True,
        },
        partial=True,
    )
    pulumi_mocks.assert_res_counts(_counts(VPC_AZ2_COUNTS, APP_SG_COUNTS, DOCDB_COUNTS))


@pulumi.runtime.test
def test_document_db_resources_exposes_created_resources(pulumi_mocks):
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


@pulumi.runtime.test
def test_document_db_resources_parented_to_document_db_component(pulumi_mocks):
    ingress = []

    def capture_ingress(args):
        if args.type_ == R.SECURITY_GROUP_INGRESS_RULE:
            ingress.append(args.resource)

    # A public transformation observes hidden resources without changing their options.
    pulumi.runtime.register_stack_transformation(capture_ingress)
    vpc = Vpc(VPC_NAME)
    r = DocumentDb(DB_NAME, vpc=vpc).resources
    assert len(ingress) == 1
    children = [r.cluster, *r.instances, r.subnet_group, r.parameter_group, r.security_group]
    app_sg = vpc.app_security_group

    def check(urns):
        assert urns == [
            "urn:pulumi:stack::project::stelvio:aws:DocumentDb$aws:docdb/cluster:Cluster::test-test-todos",
            "urn:pulumi:stack::project::stelvio:aws:DocumentDb$aws:docdb/clusterInstance:ClusterInstance::test-test-todos-1",
            "urn:pulumi:stack::project::stelvio:aws:DocumentDb$aws:docdb/subnetGroup:SubnetGroup::test-test-todos-subnet-group",
            "urn:pulumi:stack::project::stelvio:aws:DocumentDb$aws:docdb/clusterParameterGroup:ClusterParameterGroup::test-test-todos-parameter-group",
            "urn:pulumi:stack::project::stelvio:aws:DocumentDb$aws:ec2/securityGroup:SecurityGroup::test-test-todos-sg",
            "urn:pulumi:stack::project::stelvio:aws:DocumentDb$aws:vpc/securityGroupIngressRule:SecurityGroupIngressRule::test-test-todos-ingress",
            "urn:pulumi:stack::project::stelvio:aws:Vpc$aws:ec2/securityGroup:SecurityGroup::test-test-main_vpc-app-sg",
        ]

    return pulumi.Output.all(*[res.urn for res in [*children, *ingress]], app_sg.urn).apply(check)


def test_document_db_config_dict_matches_dataclass():
    assert_config_dict_matches_dataclass(DocumentDbConfig, DocumentDbConfigDict)


def _connection_string(host: str, *, port: object = 27017, username: str = "stelvio") -> str:
    user = quote_plus(username)
    password = quote_plus(DOCDB_MOCK_SECRET_PASSWORD)
    ca_file = quote_plus(DOCDB_CA_ZIP_PATH)
    return (
        f"mongodb://{user}:{password}@{host}:{port}/"
        f"?tls=true&tlsCAFile={ca_file}&replicaSet=rs0&retryWrites=false"
    )


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
        f"{prefix}CONNECTION_STRING": _connection_string(host),
    }


def _unwrap_pulumi_secret(value: object) -> object:
    """Drop Pulumi's secret envelope so tests can read Lambda env vars."""
    if (
        isinstance(value, dict)
        and "4dabf18193072939515e22adb298388d" in value
        and "value" in value
    ):
        return _unwrap_pulumi_secret(value["value"])
    if isinstance(value, dict):
        return {k: _unwrap_pulumi_secret(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_unwrap_pulumi_secret(v) for v in value]
    return value


def _function_env_vars(pulumi_mocks, fn_name: str) -> dict[str, str]:
    fn_res = pulumi_mocks.assert_res(fn_name, R.FUNCTION)
    env = _unwrap_pulumi_secret(fn_res.inputs["environment"])
    assert isinstance(env, dict)
    variables = env["variables"]
    assert isinstance(variables, dict)
    return variables


def _assert_function_document_db_link(pulumi_mocks, fn_name: str, *db_names: str) -> None:
    env = {}
    for name in db_names:
        env |= _link_env_vars(name)
    variables = _function_env_vars(pulumi_mocks, fn_name)
    assert {k: variables[k] for k in env} == env
    for name in db_names:
        prefix = f"STLV_{name.replace('-', '_').upper()}_"
        assert f"{prefix}PASSWORD" not in variables
        assert variables[f"{prefix}CONNECTION_STRING"].startswith("mongodb://")
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
    path = Path(ca.path)
    assert path.parts[-3:] == ("aws", "documentdb", "global-bundle.pem")
    assert path.read_bytes() == _FAKE_CA_PEM


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


@mark.parametrize("secret_rotation", [7, 30, False], ids=["default", "custom", "disabled"])
@pulumi.runtime.test
def test_document_db_link(pulumi_mocks, secret_rotation):
    db = DocumentDb(DB_NAME, vpc=Vpc(VPC_NAME), secret_rotation=secret_rotation)
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
            "connection_string": _connection_string(DOCDB_HOST),
        }
        assert resources == [DOCDB_SECRET_ARN]

    return pulumi.Output.all(link.properties, permissions[0].resources).apply(check)


@pulumi.runtime.test
def test_document_db_link_escapes_reserved_password_characters(pulumi_mocks, monkeypatch):
    monkeypatch.setattr("tests.aws.pulumi_mocks.DOCDB_MOCK_SECRET_PASSWORD", "p:ass%word")
    db = DocumentDb(DB_NAME, vpc=Vpc(VPC_NAME))

    def check(connection_string):
        assert connection_string == (
            f"mongodb://stelvio:p%3Aass%25word@{DOCDB_HOST}:27017/"
            "?tls=true&tlsCAFile=stlv_docdb_ca.pem&replicaSet=rs0&retryWrites=false"
        )

    return db.link().properties["connection_string"].apply(check)


def test_document_db_connection_string_is_secret(pulumi_mocks):
    @pulumi.runtime.test
    def deploy():
        db = DocumentDb(DB_NAME, vpc=Vpc(VPC_NAME))
        connection_string = db.link().properties["connection_string"]

        def check(values: list[bool]) -> None:
            assert values == [True]

        return pulumi.Output.all(connection_string.is_secret()).apply(check)

    deploy()
    pulumi_mocks.assert_res_counts(_counts(VPC_AZ2_COUNTS, APP_SG_COUNTS, DOCDB_COUNTS))


@mark.parametrize("link_style", ["component", "link", "link-with-permissions"])
@mark.parametrize("db_name", [DB_NAME, "my-db"])
def test_document_db_link_without_vpc_raises(pulumi_mocks, db_name, link_style):
    @pulumi.runtime.test
    def deploy():
        db = DocumentDb(db_name, vpc=Vpc(VPC_NAME))
        if link_style == "component":
            linked = db
        elif link_style == "link":
            linked = db.link()
        else:
            linked = _overridden_link(db)
        return Function("client", handler=SIMPLE_HANDLER, links=[linked]).resources

    with raises(
        ValueError,
        match=re.escape(
            f"Function 'client' links DocumentDb '{db_name}' but has no vpc=. "
            f"Set vpc= to the same Vpc as the cluster ('{VPC_NAME}'); "
            "linking is not networking."
        ),
    ):
        deploy()


@mark.parametrize("link_style", ["component", "link", "link-with-permissions"])
def test_document_db_link_with_different_vpc_raises(pulumi_mocks, link_style):
    @pulumi.runtime.test
    def deploy():
        db = DocumentDb(DB_NAME, vpc=Vpc(VPC_NAME))
        if link_style == "component":
            linked = db
        elif link_style == "link":
            linked = db.link()
        else:
            linked = _overridden_link(db)
        return Function(
            "client", handler=SIMPLE_HANDLER, vpc=Vpc("other"), links=[linked]
        ).resources

    with raises(
        ValueError,
        match=re.escape(
            f"Function 'client' links DocumentDb '{DB_NAME}' in Vpc '{VPC_NAME}' "
            "but is attached to Vpc 'other'. "
            "Set vpc= to the same Vpc as the cluster; linking is not networking."
        ),
    ):
        deploy()


@mark.parametrize("link_style", ["component", "link"])
def test_document_db_link_with_vpc_uses_app_security_group(pulumi_mocks, project_cwd, link_style):
    @pulumi.runtime.test
    def deploy():
        vpc = Vpc(VPC_NAME)
        db = DocumentDb(DB_NAME, vpc=vpc)
        linked = db if link_style == "component" else db.link()
        fn = Function("client", handler=SIMPLE_HANDLER, vpc=vpc, links=[linked])
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
    variables = _function_env_vars(pulumi_mocks, "client")
    expected = _link_env_vars(DB_NAME)
    assert {key: variables[key] for key in expected} == expected
    assert "STLV_TODOS_PASSWORD" not in variables
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


def test_function_linked_to_document_db_with_custom_security_groups(pulumi_mocks, project_cwd):
    @pulumi.runtime.test
    def deploy():
        vpc = Vpc(VPC_NAME)
        db = DocumentDb(DB_NAME, vpc=vpc)
        fn = Function(
            "client",
            handler=SIMPLE_HANDLER,
            vpc=VpcAttachment(vpc=vpc, security_groups=["sg-123"]),
            links=[db],
        )
        return db.resources, fn.resources

    deploy()

    _assert_function_document_db_link(pulumi_mocks, "client", DB_NAME)
    pulumi_mocks.assert_res(
        "client",
        R.FUNCTION,
        {"vpcConfig": {"subnetIds": PRIVATE_SUBNET_IDS, "securityGroupIds": ["sg-123"]}},
        partial=True,
    )
    _assert_app_sg_ingress(pulumi_mocks, DB_NAME)
    _assert_ca_packaged(pulumi_mocks, "client")
    pulumi_mocks.assert_res_counts(
        _counts(VPC_AZ2_COUNTS, APP_SG_COUNTS, DOCDB_COUNTS, FUNCTION_VPC_LINKED_COUNTS)
    )


def test_function_without_document_db_link_does_not_package_ca(
    pulumi_mocks, project_cwd, mock_docdb_ca_urlopen
):
    @pulumi.runtime.test
    def deploy():
        return Function("client", handler=SIMPLE_HANDLER).resources

    deploy()

    assert (
        DOCDB_CA_ZIP_PATH
        not in pulumi_mocks.assert_res("client", R.FUNCTION).inputs["code"].assets
    )
    assert mock_docdb_ca_urlopen == []


def test_document_db_ca_download_failure_raises(pulumi_mocks, project_cwd, monkeypatch):
    def boom(_url: str, **_kwargs: object) -> object:
        raise OSError("network down")

    monkeypatch.setattr("stelvio.aws.document_db.urlopen", boom)

    @pulumi.runtime.test
    def deploy():
        vpc = Vpc(VPC_NAME)
        db = DocumentDb(DB_NAME, vpc=vpc)
        return Function("client", handler=SIMPLE_HANDLER, vpc=vpc, links=[db]).resources

    with raises(RuntimeError, match="Failed to download DocumentDB CA bundle"):
        deploy()


def test_document_db_ca_rejects_empty_download(pulumi_mocks, project_cwd, monkeypatch):
    class _Empty:
        def __enter__(self):
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def read(self) -> bytes:
            return b"not a certificate"

    monkeypatch.setattr("stelvio.aws.document_db.urlopen", lambda _url, **_kwargs: _Empty())

    @pulumi.runtime.test
    def deploy():
        vpc = Vpc(VPC_NAME)
        db = DocumentDb(DB_NAME, vpc=vpc)
        return Function("client", handler=SIMPLE_HANDLER, vpc=vpc, links=[db]).resources

    with raises(RuntimeError, match="empty or not a PEM file"):
        deploy()


def test_document_db_ca_reuses_cached_bundle(pulumi_mocks, project_cwd, mock_docdb_ca_urlopen):
    cache = project_cwd / ".stelvio" / "aws" / "documentdb" / "global-bundle.pem"
    cache.parent.mkdir(parents=True)
    cache.write_bytes(_FAKE_CA_PEM)

    @pulumi.runtime.test
    def deploy():
        vpc = Vpc(VPC_NAME)
        db = DocumentDb(DB_NAME, vpc=vpc)
        return Function("client", handler=SIMPLE_HANDLER, vpc=vpc, links=[db]).resources

    deploy()

    assert mock_docdb_ca_urlopen == []
    _assert_ca_packaged(pulumi_mocks, "client")


def test_document_db_ca_refreshes_stale_cached_bundle(
    pulumi_mocks, project_cwd, mock_docdb_ca_urlopen, monkeypatch
):
    cache = project_cwd / ".stelvio" / "aws" / "documentdb" / "global-bundle.pem"
    cache.parent.mkdir(parents=True)
    cache.write_bytes(b"-----BEGIN CERTIFICATE-----\nold\n")
    monkeypatch.setattr(
        "stelvio.aws.document_db.time.time",
        lambda: cache.stat().st_mtime + 24 * 60 * 60 + 1,
    )

    @pulumi.runtime.test
    def deploy():
        vpc = Vpc(VPC_NAME)
        db = DocumentDb(DB_NAME, vpc=vpc)
        return Function("client", handler=SIMPLE_HANDLER, vpc=vpc, links=[db]).resources

    deploy()

    assert mock_docdb_ca_urlopen == [
        "https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem"
    ]
    assert cache.read_bytes() == _FAKE_CA_PEM
    pulumi_mocks.assert_res_counts(
        _counts(VPC_AZ2_COUNTS, APP_SG_COUNTS, DOCDB_COUNTS, FUNCTION_VPC_LINKED_COUNTS)
    )


def test_document_db_ca_replaces_corrupt_cached_bundle(
    pulumi_mocks, project_cwd, mock_docdb_ca_urlopen
):
    cache = project_cwd / ".stelvio" / "aws" / "documentdb" / "global-bundle.pem"
    cache.parent.mkdir(parents=True)
    cache.write_bytes(b"not a certificate")

    @pulumi.runtime.test
    def deploy():
        vpc = Vpc(VPC_NAME)
        db = DocumentDb(DB_NAME, vpc=vpc)
        return Function("client", handler=SIMPLE_HANDLER, vpc=vpc, links=[db]).resources

    deploy()

    assert mock_docdb_ca_urlopen == [
        "https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem"
    ]
    assert cache.read_bytes() == _FAKE_CA_PEM
    pulumi_mocks.assert_res_counts(
        _counts(VPC_AZ2_COUNTS, APP_SG_COUNTS, DOCDB_COUNTS, FUNCTION_VPC_LINKED_COUNTS)
    )


def test_document_db_ca_refresh_failure_preserves_cached_bundle(
    pulumi_mocks, project_cwd, monkeypatch
):
    cache = project_cwd / ".stelvio" / "aws" / "documentdb" / "global-bundle.pem"
    cache.parent.mkdir(parents=True)
    old_bundle = b"-----BEGIN CERTIFICATE-----\nold\n"
    cache.write_bytes(old_bundle)
    monkeypatch.setattr(
        "stelvio.aws.document_db.time.time",
        lambda: cache.stat().st_mtime + 24 * 60 * 60 + 1,
    )

    def boom(_url: str, **_kwargs: object) -> object:
        raise OSError("network down")

    monkeypatch.setattr("stelvio.aws.document_db.urlopen", boom)

    @pulumi.runtime.test
    def deploy():
        vpc = Vpc(VPC_NAME)
        db = DocumentDb(DB_NAME, vpc=vpc)
        return Function("client", handler=SIMPLE_HANDLER, vpc=vpc, links=[db]).resources

    with raises(RuntimeError, match="Failed to download DocumentDB CA bundle"):
        deploy()
    assert cache.read_bytes() == old_bundle


def test_two_functions_linked_to_one_document_db(pulumi_mocks, project_cwd, mock_docdb_ca_urlopen):
    @pulumi.runtime.test
    def deploy():
        vpc = Vpc(VPC_NAME)
        db = DocumentDb(DB_NAME, vpc=vpc)
        reader = Function("reader", handler=SIMPLE_HANDLER, vpc=vpc, links=[db])
        writer = Function("writer", handler=SIMPLE_HANDLER, vpc=vpc, links=[db])
        return db.resources, reader.resources, writer.resources

    deploy()

    assert mock_docdb_ca_urlopen == [
        "https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem"
    ]
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
                "STLV_TODOS_CONNECTION_STRING": _connection_string(DOCDB_HOST, username="admin"),
            },
        ),
        (
            {"port": 27018},
            {
                "STLV_TODOS_PORT": "27018",
                "STLV_TODOS_CONNECTION_STRING": _connection_string(DOCDB_HOST, port=27018),
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
    variables = _function_env_vars(pulumi_mocks, "client")
    assert {k: variables[k] for k in expected} == expected


def test_document_db_link_raises_without_managed_master_password(pulumi_mocks):
    db = DocumentDb(
        DB_NAME, vpc=Vpc(VPC_NAME), customize={"cluster": {"manage_master_user_password": False}}
    )
    link = db.link()

    @pulumi.runtime.test
    def deploy():
        return link.properties["secret_arn"]

    with raises(
        ValueError,
        match=re.escape(
            "Cannot link DocumentDb 'todos': the cluster has no AWS-managed "
            "master-user secret. Linking requires 'manage_master_user_password' to stay "
            "enabled (the default) - it was likely disabled via customize."
        ),
    ):
        deploy()


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


@mark.parametrize("style", ["kwargs", "dict", "object"])
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
def test_document_db_instance_customization_precedence(pulumi_mocks, style, case):
    instance, global_callable, local, expected = case

    global_props = {"instance_class": "db.r6g.large"}
    customization = (lambda props: props | global_props) if global_callable else global_props
    _ContextStore.clear()
    _ContextStore.set(
        AppContext(
            name="test",
            env="test",
            aws=AwsConfig(profile="default", region="us-east-1"),
            home="aws",
            customize={DocumentDb: {"instance": customization}},
        )
    )

    @pulumi.runtime.test
    def deploy():
        opts = {"vpc": Vpc(VPC_NAME)}
        if instance != "omitted":
            opts["instance_class"] = instance
        customize = {"instance": {"instance_class": local}} if local else None
        if style == "kwargs":
            db = DocumentDb(DB_NAME, customize=customize, **opts)
        elif style == "dict":
            db = DocumentDb(DB_NAME, opts, customize=customize)
        else:
            db = DocumentDb(DB_NAME, DocumentDbConfig(**opts), customize=customize)
        return db.resources

    deploy()
    verify_document_db(pulumi_mocks, replace(DEFAULT_TC, aws_instance_class=expected))


@mark.parametrize("style", ["kwargs", "dict", "object"])
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
def test_document_db_cluster_customization_precedence(pulumi_mocks, style, case):
    opts, global_callable, local, expected_protection, expected_retention = case
    global_props = {"deletion_protection": True, "backup_retention_period": 14}
    customization = (lambda props: props | global_props) if global_callable else global_props
    _ContextStore.clear()
    _ContextStore.set(
        AppContext(
            name="test",
            env="test",
            aws=AwsConfig(profile="default", region="us-east-1"),
            home="aws",
            customize={DocumentDb: {"cluster": customization}},
        )
    )

    @pulumi.runtime.test
    def deploy():
        config = {"vpc": Vpc(VPC_NAME), **opts}
        customize = {"cluster": local} if local is not None else None
        if style == "kwargs":
            db = DocumentDb(DB_NAME, customize=customize, **config)
        elif style == "dict":
            db = DocumentDb(DB_NAME, config, customize=customize)
        else:
            db = DocumentDb(DB_NAME, DocumentDbConfig(**config), customize=customize)
        return db.resources

    deploy()
    verify_document_db(
        pulumi_mocks,
        replace(
            DEFAULT_TC,
            deletion_protection=expected_protection,
            backup_retention_period=expected_retention,
        ),
    )


def test_document_db_local_customize_wins_over_constructor_deletion_protection(pulumi_mocks):
    @pulumi.runtime.test
    def deploy():
        return DocumentDb(
            DB_NAME,
            vpc=Vpc(VPC_NAME),
            deletion_protection=False,
            customize={"cluster": {"deletion_protection": True}},
        ).resources

    deploy()
    verify_document_db(pulumi_mocks, replace(DEFAULT_TC, deletion_protection=True))


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


def test_document_db_rejects_empty_config_with_kwargs():
    with raises(ValueError, match="cannot combine 'config' parameter with additional options"):
        DocumentDb(DB_NAME, {}, vpc=Vpc(VPC_NAME))
