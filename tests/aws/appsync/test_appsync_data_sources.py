"""AppSync data source tests — Lambda, DynamoDB, HTTP, RDS, OpenSearch."""

import json

import pulumi
from pulumi_aws import appsync, iam
from pytest import mark, raises

from stelvio.aws.dynamo_db import DynamoTable
from stelvio.aws.function import Function, FunctionConfig

from ..conftest import assert_urn
from ..pulumi_mocks import ACCOUNT_ID, DEFAULT_REGION, R, tn
from .conftest import (
    API_ID,
    TP,
    add_resolver_for_ds,
    deploy_all,
    make_api,
    make_data_source,
)

APPSYNC_TRUST_POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {"Service": "appsync.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }
    ],
}
POSTS_FN_ARN = (
    f"arn:aws:lambda:{DEFAULT_REGION}:{ACCOUNT_ID}:function:{tn(f'{TP}myapi-ds-posts-fn')}"
)
ITEMS_TABLE_ARN = f"arn:aws:dynamodb:{DEFAULT_REGION}:{ACCOUNT_ID}:table/{tn(f'{TP}items')}"


def service_role_arn(ds_name: str) -> str:
    return f"arn:aws:iam::{ACCOUNT_ID}:role/{tn(f'{TP}myapi-ds-{ds_name}-role')}"


# --- Data source creation (all types) ---


@mark.parametrize(
    ("ds_type", "expected_name", "expected_config"),
    [
        (
            "dynamo",
            "items",
            {
                "type": "AMAZON_DYNAMODB",
                "dynamodbConfig": {"tableName": tn(f"{TP}items"), "region": "us-east-1"},
            },
        ),
        (
            "http",
            "ext",
            {"type": "HTTP", "httpConfig": {"endpoint": "https://api.example.com"}},
        ),
        (
            "rds",
            "db",
            {
                "type": "RELATIONAL_DATABASE",
                "relationalDatabaseConfig": {
                    "httpEndpointConfig": {
                        "dbClusterIdentifier": (
                            "arn:aws:rds:us-east-1:123456789012:cluster:my-cluster"
                        ),
                        "awsSecretStoreArn": (
                            "arn:aws:secretsmanager:us-east-1:123456789012:secret:my-secret"
                        ),
                        "databaseName": "mydb",
                    }
                },
            },
        ),
        (
            "opensearch",
            "search",
            {
                "type": "AMAZON_OPENSEARCH_SERVICE",
                "opensearchserviceConfig": {
                    "endpoint": (
                        "https://search-my-domain-abc123def456ghij.us-east-1.es.amazonaws.com"
                    )
                },
            },
        ),
    ],
    ids=["dynamo", "http", "rds", "opensearch"],
)
def test_data_source_creates_correct_type(
    ds_type, expected_name, expected_config, pulumi_mocks, project_cwd
):
    """Each data source type creates a resource with the correct type, name, and config."""
    api = make_api()
    ds = make_data_source(api, ds_type)
    add_resolver_for_ds(api, ds, ds_type)

    deploy_all()

    pulumi_mocks.assert_res(
        f"myapi-ds-{expected_name}",
        R.APPSYNC_DATA_SOURCE,
        {
            "name": expected_name,
            "apiId": API_ID,
            "serviceRoleArn": service_role_arn(expected_name),
            **expected_config,
        },
    )
    pulumi_mocks.assert_res(f"myapi-ds-{expected_name}-role", R.ROLE)
    assert len(pulumi_mocks.created(R.APPSYNC_DATA_SOURCE)) == 2
    assert len(pulumi_mocks.created(R.ROLE)) == 1
    if ds_type == "http":
        pulumi_mocks.assert_no_res(R.ROLE_POLICY)


def test_dynamo_data_source_region_uses_resolved_region(
    pulumi_mocks, project_cwd, no_region_context
):
    """dynamodbConfig.region carries the table's chain-resolved region when config has none.

    Scenario (the default new-user setup): no `region=` in @app.config, but the AWS
    chain resolves one (here AWS_REGION=eu-central-1 via the fixture). The original
    code sent the raw config value (None); the field means "region of the TABLE",
    so it now reads the table component's provider region.
    """
    api = make_api()
    ds = make_data_source(api, "dynamo")
    add_resolver_for_ds(api, ds, "dynamo")

    deploy_all()

    pulumi_mocks.assert_res(
        "myapi-ds-items",
        R.APPSYNC_DATA_SOURCE,
        {"dynamodbConfig": {"tableName": tn(f"{TP}items"), "region": "eu-central-1"}},
        partial=True,
    )
    assert len(pulumi_mocks.created(R.APPSYNC_DATA_SOURCE)) == 2


def test_lambda_data_source_creates_function_role_and_accessible(pulumi_mocks, project_cwd):
    api = make_api()
    posts = api.data_source_lambda("posts", handler="functions/simple.handler")
    api.query("getPost", posts)

    deploy_all()

    pulumi_mocks.assert_res(
        "myapi-ds-posts",
        R.APPSYNC_DATA_SOURCE,
        {
            "name": "posts",
            "type": "AWS_LAMBDA",
            "lambdaConfig": {"functionArn": POSTS_FN_ARN},
            "serviceRoleArn": service_role_arn("posts"),
            "apiId": API_ID,
        },
    )
    role = pulumi_mocks.assert_res("myapi-ds-posts-role", R.ROLE)
    assert role.inputs.keys() == {"assumeRolePolicy"}
    assert json.loads(role.inputs["assumeRolePolicy"]) == APPSYNC_TRUST_POLICY
    policy = pulumi_mocks.assert_res(
        "myapi-ds-posts-lambda-policy",
        R.ROLE_POLICY,
        {"role": tn(f"{TP}myapi-ds-posts-role")},
        partial=True,
    )
    assert json.loads(policy.inputs["policy"]) == {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow", "Action": ["lambda:InvokeFunction"], "Resource": POSTS_FN_ARN}
        ],
    }
    pulumi_mocks.assert_res(
        "myapi-ds-posts-fn", R.FUNCTION, {"handler": "simple.handler"}, partial=True
    )
    pulumi_mocks.assert_res_counts(
        {
            R.GRAPHQL_API: 1,
            R.APPSYNC_DATA_SOURCE: 2,
            R.APPSYNC_RESOLVER: 1,
            R.ROLE: 2,
            R.ROLE_POLICY: 1,
            R.ROLE_POLICY_ATTACHMENT: 1,
            R.FUNCTION: 1,
        }
    )
    assert isinstance(posts.resources.data_source, appsync.DataSource)
    assert isinstance(posts.resources.service_role, iam.Role)
    assert isinstance(posts.resources.function, Function)


@mark.parametrize(
    ("handler", "extra_kwargs", "expected_fn_inputs"),
    [
        (
            FunctionConfig(handler="functions/simple.handler", memory=512, timeout=30),
            {},
            {"memorySize": 512, "timeout": 30},
        ),
        (
            "functions/simple.handler",
            {"memory": 256},
            {"memorySize": 256},
        ),
        (
            {"handler": "functions/simple.handler", "memory": 384},
            {},
            {"handler": "simple.handler", "memorySize": 384},
        ),
    ],
    ids=["function-config", "fn-opts", "dict"],
)
def test_lambda_data_source_function_options(
    handler, extra_kwargs, expected_fn_inputs, pulumi_mocks, project_cwd
):
    api = make_api()
    posts = api.data_source_lambda("posts", handler, **extra_kwargs)
    add_resolver_for_ds(api, posts, "lambda")

    deploy_all()

    pulumi_mocks.assert_res("myapi-ds-posts-fn", R.FUNCTION, expected_fn_inputs, partial=True)
    pulumi_mocks.assert_res_counts(
        {
            R.GRAPHQL_API: 1,
            R.APPSYNC_DATA_SOURCE: 2,
            R.APPSYNC_RESOLVER: 1,
            R.ROLE: 2,
            R.ROLE_POLICY: 1,
            R.ROLE_POLICY_ATTACHMENT: 1,
            R.FUNCTION: 1,
        }
    )


def test_lambda_data_source_with_function_instance(pulumi_mocks, project_cwd):
    api = make_api()
    fn = Function("my-fn", handler="functions/simple.handler")
    posts = api.data_source_lambda("posts", fn)
    add_resolver_for_ds(api, posts, "lambda")

    deploy_all()

    pulumi_mocks.assert_res(
        "myapi-ds-posts",
        R.APPSYNC_DATA_SOURCE,
        {
            "name": "posts",
            "type": "AWS_LAMBDA",
            "lambdaConfig": {
                "functionArn": f"arn:aws:lambda:{DEFAULT_REGION}:{ACCOUNT_ID}:function:"
                + tn(f"{TP}my-fn")
            },
        },
        partial=True,
    )
    assert len(pulumi_mocks.created(R.APPSYNC_DATA_SOURCE)) == 2


def test_lambda_data_source_with_links_list(pulumi_mocks, project_cwd):
    api = make_api()
    table = DynamoTable("items", fields={"pk": "S"}, partition_key="pk")
    posts = api.data_source_lambda(
        "posts",
        handler="functions/simple.handler",
        links=[table],
    )
    api.query("getPost", posts)

    deploy_all()

    pulumi_mocks.assert_res(
        "myapi-ds-posts-fn",
        R.FUNCTION,
        {
            "environment": {
                "variables": {
                    "STLV_ITEMS_TABLE_ARN": ITEMS_TABLE_ARN,
                    "STLV_ITEMS_TABLE_NAME": tn(f"{TP}items"),
                }
            }
        },
        partial=True,
    )


# --- IAM policies (single-statement data sources) ---


@mark.parametrize(
    ("ds_type", "policy_name", "expected_actions", "expected_resources"),
    [
        (
            "dynamo",
            "myapi-ds-items-dynamo-policy",
            [
                "dynamodb:GetItem",
                "dynamodb:PutItem",
                "dynamodb:UpdateItem",
                "dynamodb:DeleteItem",
                "dynamodb:Query",
                "dynamodb:Scan",
            ],
            [ITEMS_TABLE_ARN, f"{ITEMS_TABLE_ARN}/index/*"],
        ),
        (
            "opensearch",
            "myapi-ds-search-policy",
            ["es:ESHttp*"],
            "arn:aws:es:us-east-1:*:domain/my-domain/*",
        ),
    ],
    ids=["dynamo", "opensearch"],
)
def test_data_source_creates_iam_policy(  # noqa: PLR0913
    ds_type, policy_name, expected_actions, expected_resources, pulumi_mocks, project_cwd
):
    """DynamoDB and OpenSearch data sources create correct IAM policies."""
    api = make_api()
    ds = make_data_source(api, ds_type)
    add_resolver_for_ds(api, ds, ds_type)

    deploy_all()

    policy = pulumi_mocks.assert_res(policy_name, R.ROLE_POLICY)
    [statement] = json.loads(policy.inputs["policy"])["Statement"]
    # IAM ignores the order of actions
    assert {**statement, "Action": set(statement["Action"])} == {
        "Effect": "Allow",
        "Action": set(expected_actions),
        "Resource": expected_resources,
    }
    assert len(pulumi_mocks.created(R.ROLE_POLICY)) == 1


# --- VPC-style OpenSearch endpoint ---


def test_opensearch_vpc_endpoint(pulumi_mocks, project_cwd):
    """VPC-style OpenSearch endpoints produce correct config and IAM ARN."""
    api = make_api()
    vpc_endpoint = "https://vpc-mydomain-abc123.us-east-1.es.amazonaws.com"
    ds = api.data_source_opensearch("search", endpoint=vpc_endpoint)
    api.query("getPost", ds, code="resolvers/getItem.js")

    deploy_all()

    pulumi_mocks.assert_res(
        "myapi-ds-search",
        R.APPSYNC_DATA_SOURCE,
        {
            "type": "AMAZON_OPENSEARCH_SERVICE",
            "name": "search",
            "opensearchserviceConfig": {"endpoint": vpc_endpoint},
        },
        partial=True,
    )
    policy = pulumi_mocks.assert_res("myapi-ds-search-policy", R.ROLE_POLICY)
    assert json.loads(policy.inputs["policy"]) == {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["es:ESHttp*"],
                "Resource": "arn:aws:es:us-east-1:*:domain/mydomain/*",
            }
        ],
    }

    # --- RDS data source ---
    assert len(pulumi_mocks.created(R.APPSYNC_DATA_SOURCE)) == 2
    assert len(pulumi_mocks.created(R.ROLE_POLICY)) == 1


def test_rds_data_source_creates_iam_policy(pulumi_mocks, project_cwd):
    api = make_api()
    db = api.data_source_rds(
        "db",
        cluster_arn="arn:aws:rds:us-east-1:123456789012:cluster:my-cluster",
        secret_arn="arn:aws:secretsmanager:us-east-1:123456789012:secret:my-secret",  # noqa: S106
        database="mydb",
    )
    api.query("getPost", db, code="resolvers/getItem.js")

    deploy_all()

    policy = pulumi_mocks.assert_res("myapi-ds-db-policy", R.ROLE_POLICY)
    document = json.loads(policy.inputs["policy"])
    expected = [
        {
            "Effect": "Allow",
            "Action": [
                "rds-data:ExecuteStatement",
                "rds-data:BatchExecuteStatement",
                "rds-data:BeginTransaction",
                "rds-data:CommitTransaction",
                "rds-data:RollbackTransaction",
            ],
            "Resource": "arn:aws:rds:us-east-1:123456789012:cluster:my-cluster",
        },
        {
            "Effect": "Allow",
            "Action": ["secretsmanager:GetSecretValue"],
            "Resource": "arn:aws:secretsmanager:us-east-1:123456789012:secret:my-secret",
        },
    ]
    assert document["Version"] == "2012-10-17"
    assert sorted(document["Statement"], key=json.dumps) == sorted(expected, key=json.dumps)
    assert len(pulumi_mocks.created(R.ROLE_POLICY)) == 1


# --- Validation ---


@mark.parametrize(
    ("setup", "match"),
    [
        (
            lambda api: api.data_source_lambda("", handler="functions/simple.handler"),
            "Data source name cannot be empty",
        ),
        (
            lambda api: api.data_source_http("ext", url=""),
            "url cannot be empty",
        ),
        (
            lambda api: api.data_source_opensearch("search", endpoint=""),
            "endpoint cannot be empty",
        ),
        (
            lambda api: api.data_source_opensearch(
                "search",
                endpoint="https://not-valid-endpoint.com",
            ),
            "Cannot derive domain ARN",
        ),
        (
            lambda api: api.data_source_lambda(
                "NONE",
                handler="functions/simple.handler",
            ),
            "reserved",
        ),
    ],
    ids=[
        "empty-ds-name",
        "empty-http-url",
        "empty-opensearch-endpoint",
        "invalid-opensearch-format",
        "reserved-none-name",
    ],
)
def test_data_source_validation(setup, match, project_cwd):
    api = make_api()
    with raises(ValueError, match=match):
        setup(api)


def test_duplicate_data_source_name(project_cwd):
    api = make_api()
    api.data_source_lambda("posts", handler="functions/simple.handler")
    with raises(ValueError, match="Duplicate data source name 'posts'"):
        api.data_source_lambda("posts", handler="functions/simple.handler")


def test_lambda_data_source_handler_required(project_cwd):
    api = make_api()
    with raises(TypeError):
        api.data_source_lambda("posts")


def test_dynamo_data_source_requires_dynamo_table_component(project_cwd):
    api = make_api()
    with raises(TypeError, match="table must be a DynamoTable"):
        api.data_source_dynamo("items", table=object())


@mark.parametrize(
    "extra_kwargs",
    [{"memory": 256}, {"links": []}],
    ids=["with-fn-opts", "with-links"],
)
def test_lambda_data_source_function_instance_extra_opts_raises(extra_kwargs, project_cwd):
    """Passing a Function instance with extra fn_opts or links should raise ValueError."""
    api = make_api()
    fn = Function("my-fn", handler="functions/simple.handler")
    with raises(ValueError, match="Cannot combine a Function handler with function options"):
        api.data_source_lambda("posts", fn, **extra_kwargs)


def test_none_data_source_created(pulumi_mocks, project_cwd):
    """The internal shared NONE data source is created when resources are built."""
    api = make_api()
    api.mutation("sendMessage", None)

    deploy_all()

    pulumi_mocks.assert_res(
        "myapi-none-ds", R.APPSYNC_DATA_SOURCE, {"name": "NONE", "type": "NONE"}, partial=True
    )
    pulumi_mocks.assert_res_counts(
        {R.GRAPHQL_API: 1, R.APPSYNC_DATA_SOURCE: 1, R.APPSYNC_RESOLVER: 1}
    )


def test_data_source_customize_applied(pulumi_mocks, project_cwd):
    api = make_api()
    posts = api.data_source_lambda(
        "posts",
        handler="functions/simple.handler",
        customize={
            "data_source": {"name": "custom-posts"},
            "service_role": {"path": "/service-role/"},
        },
    )
    api.query("getPost", posts)

    deploy_all()

    pulumi_mocks.assert_res(
        "myapi-ds-posts",
        R.APPSYNC_DATA_SOURCE,
        {"type": "AWS_LAMBDA", "name": "custom-posts"},
        partial=True,
    )
    pulumi_mocks.assert_res(
        "myapi-ds-posts-role", R.ROLE, {"path": "/service-role/"}, partial=True
    )
    assert len(pulumi_mocks.created(R.APPSYNC_DATA_SOURCE)) == 2
    assert len(pulumi_mocks.created(R.ROLE)) == 2


def test_lambda_data_source_function_customize_applied(pulumi_mocks, project_cwd):
    api = make_api()
    posts = api.data_source_lambda(
        "posts",
        handler="functions/simple.handler",
        customize={"function": {"function": {"memory_size": 1024}}},
    )
    api.query("getPost", posts)

    deploy_all()

    pulumi_mocks.assert_res("myapi-ds-posts-fn", R.FUNCTION, {"memorySize": 1024}, partial=True)


# --- Customize key validation ---


@mark.parametrize(
    ("ds_type", "bad_key"),
    [
        ("lambda", "datasource"),
        ("dynamo", "role"),
        ("http", "ds"),
        ("rds", "source"),
        ("opensearch", "opensearch"),
    ],
    ids=["lambda", "dynamo", "http", "rds", "opensearch"],
)
def test_data_source_invalid_customize_key(ds_type, bad_key, project_cwd):
    api = make_api()
    with raises(ValueError, match="Unknown customization key"):
        make_data_source(api, ds_type, customize={bad_key: {}})


# `parent=self` lives in ResourceOptions (invisible to input mocks), but it surfaces
# in the data source component's URN as the `stelvio:aws:AppSync$` parent segment.


@mark.parametrize(
    ("ds_type", "expected_name"),
    [
        ("lambda", "posts"),
        ("dynamo", "items"),
        ("http", "ext"),
        ("rds", "db"),
        ("opensearch", "search"),
    ],
    ids=["lambda", "dynamo", "http", "rds", "opensearch"],
)
@pulumi.runtime.test
def test_data_source_parented_to_appsync(ds_type, expected_name, pulumi_mocks, project_cwd):
    api = make_api()
    ds = make_data_source(api, ds_type)
    _ = ds.resources

    def check(urn):
        assert_urn(
            urn,
            "stelvio:aws:AppSync",
            "stelvio:aws:AppSyncDataSource",
            f"myapi-ds-{expected_name}",
        )

    return ds.urn.apply(check)
