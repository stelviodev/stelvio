"""AppSync data source tests — Lambda, DynamoDB, HTTP, RDS, OpenSearch."""

import pulumi
import pytest
from pulumi_aws import appsync, iam

from stelvio.aws.appsync.constants import (
    DS_TYPE_DYNAMO,
    DS_TYPE_HTTP,
    DS_TYPE_LAMBDA,
    DS_TYPE_OPENSEARCH,
    DS_TYPE_RDS,
)
from stelvio.aws.dynamo_db import DynamoTable
from stelvio.aws.function import Function, FunctionConfig

from .conftest import (
    TP,
    add_resolver_for_ds,
    assert_data_source_inputs,
    assert_iam_policy,
    assert_role,
    make_api,
    make_data_source,
    when_appsync_ready,
)

# --- Data source creation (all types) ---


@pytest.mark.parametrize(
    ("ds_type", "expected_type", "expected_name"),
    [
        ("lambda", DS_TYPE_LAMBDA, "posts"),
        ("dynamo", DS_TYPE_DYNAMO, "items"),
        ("http", DS_TYPE_HTTP, "ext"),
        ("rds", DS_TYPE_RDS, "db"),
        ("opensearch", DS_TYPE_OPENSEARCH, "search"),
    ],
    ids=["lambda", "dynamo", "http", "rds", "opensearch"],
)
@pulumi.runtime.test
def test_data_source_creates_correct_type(
    ds_type, expected_type, expected_name, pulumi_mocks, project_cwd
):
    """Each data source type creates a resource with the correct type, name, and config."""
    api = make_api()
    ds = make_data_source(api, ds_type)
    add_resolver_for_ds(api, ds, ds_type)

    def check_resources(_):
        ds_inputs = assert_data_source_inputs(
            pulumi_mocks, ds_type=expected_type, name=expected_name
        )

        if ds_type == "dynamo":
            assert "dynamodbConfig" in ds_inputs
        elif ds_type == "http":
            assert ds_inputs["httpConfig"]["endpoint"] == "https://api.example.com"
            assert_role(pulumi_mocks, "ds-ext-role")
            policies = pulumi_mocks.created_role_policies()
            assert not [p for p in policies if "ds-ext" in p.name]
        elif ds_type == "rds":
            rdb_config = ds_inputs["relationalDatabaseConfig"]
            assert rdb_config["httpEndpointConfig"]["databaseName"] == "mydb"
        elif ds_type == "opensearch":
            assert "opensearchserviceConfig" in ds_inputs
            assert (
                ds_inputs["opensearchserviceConfig"]["endpoint"]
                == "https://search-my-domain-abc123def456ghij.us-east-1.es.amazonaws.com"
            )
            assert "elasticsearchConfig" not in ds_inputs

    when_appsync_ready(api, check_resources)


@pulumi.runtime.test
def test_lambda_data_source_creates_function_role_and_accessible(pulumi_mocks, project_cwd):
    api = make_api()
    posts = api.data_source_lambda("posts", handler="functions/simple.handler")
    api.query("getPost", posts)

    def check_resources(_):
        fns = [f for f in pulumi_mocks.created_functions() if "ds-posts" in f.name]
        assert len(fns) == 1
        assert_role(pulumi_mocks, "ds-posts-role")
        assert isinstance(posts.resources.data_source, appsync.DataSource)
        assert isinstance(posts.resources.service_role, iam.Role)
        assert isinstance(posts.resources.function, Function)

    when_appsync_ready(api, check_resources)


@pytest.mark.parametrize(
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
    ],
    ids=["function-config", "fn-opts"],
)
@pulumi.runtime.test
def test_lambda_data_source_function_options(
    handler, extra_kwargs, expected_fn_inputs, pulumi_mocks, project_cwd
):
    api = make_api()
    posts = api.data_source_lambda("posts", handler, **extra_kwargs)
    add_resolver_for_ds(api, posts, "lambda")

    def check_resources(_):
        ds_fns = [f for f in pulumi_mocks.created_functions() if "ds-posts" in f.name]
        assert len(ds_fns) == 1
        for key, value in expected_fn_inputs.items():
            assert ds_fns[0].inputs[key] == value

    when_appsync_ready(api, check_resources)


@pulumi.runtime.test
def test_lambda_data_source_with_function_instance(pulumi_mocks, project_cwd):
    api = make_api()
    fn = Function("my-fn", handler="functions/simple.handler")
    posts = api.data_source_lambda("posts", fn)
    add_resolver_for_ds(api, posts, "lambda")

    def check_resources(_):
        assert_data_source_inputs(pulumi_mocks, ds_type=DS_TYPE_LAMBDA, name="posts")

    when_appsync_ready(api, check_resources)


@pulumi.runtime.test
def test_lambda_data_source_with_links_list(pulumi_mocks, project_cwd):
    api = make_api()
    table = DynamoTable("items", fields={"pk": "S"}, partition_key="pk")
    posts = api.data_source_lambda(
        "posts",
        handler="functions/simple.handler",
        links=[table],
    )
    api.query("getPost", posts)

    def check_resources(_):
        ds_fn = pulumi_mocks.assert_function_created(f"{TP}myapi-ds-posts-fn")
        assert ds_fn.typ == "aws:lambda/function:Function"
        env_vars = ds_fn.inputs["environment"]["variables"]
        assert env_vars["STELVIO_ITEMS_TABLE_ARN"].startswith(
            "arn:aws:dynamodb:us-east-1:123456789012:table/"
        )
        assert env_vars["STELVIO_ITEMS_TABLE_NAME"] == f"{TP}items-test-name"

    when_appsync_ready(api, check_resources)


# --- IAM policies (single-statement data sources) ---


@pytest.mark.parametrize(
    ("ds_type", "policy_name_pattern", "expected_actions", "expected_resources"),
    [
        (
            "lambda",
            "lambda-policy",
            ["lambda:InvokeFunction"],
            "arn:aws:lambda:us-east-1:123456789012:function:test-test-myapi-ds-posts-fn-test-name",
        ),
        (
            "dynamo",
            "dynamo-policy",
            {
                "dynamodb:GetItem",
                "dynamodb:PutItem",
                "dynamodb:UpdateItem",
                "dynamodb:DeleteItem",
                "dynamodb:Query",
                "dynamodb:Scan",
            },
            [
                "arn:aws:dynamodb:us-east-1:123456789012:table/test-test-items-test-name",
                "arn:aws:dynamodb:us-east-1:123456789012:table/test-test-items-test-name/index/*",
            ],
        ),
        (
            "opensearch",
            "ds-search-policy",
            ["es:ESHttp*"],
            "arn:aws:es:us-east-1:*:domain/my-domain/*",
        ),
    ],
    ids=["lambda", "dynamo", "opensearch"],
)
@pulumi.runtime.test
def test_data_source_creates_iam_policy(  # noqa: PLR0913
    ds_type, policy_name_pattern, expected_actions, expected_resources, pulumi_mocks, project_cwd
):
    """Lambda, DynamoDB, and OpenSearch data sources create correct IAM policies."""
    api = make_api()
    ds = make_data_source(api, ds_type)
    add_resolver_for_ds(api, ds, ds_type)

    def check_resources(_):
        assert_iam_policy(pulumi_mocks, policy_name_pattern, expected_actions, expected_resources)

    when_appsync_ready(api, check_resources)


# --- VPC-style OpenSearch endpoint ---


@pulumi.runtime.test
def test_opensearch_vpc_endpoint(pulumi_mocks, project_cwd):
    """VPC-style OpenSearch endpoints produce correct config and IAM ARN."""
    api = make_api()
    vpc_endpoint = "https://vpc-mydomain-abc123.us-east-1.es.amazonaws.com"
    ds = api.data_source_opensearch("search", endpoint=vpc_endpoint)
    api.query("getPost", ds, code="resolvers/getItem.js")

    def check_resources(_):
        ds_inputs = assert_data_source_inputs(
            pulumi_mocks, ds_type=DS_TYPE_OPENSEARCH, name="search"
        )
        assert "opensearchserviceConfig" in ds_inputs
        assert ds_inputs["opensearchserviceConfig"]["endpoint"] == vpc_endpoint
        assert_iam_policy(
            pulumi_mocks,
            "ds-search-policy",
            ["es:ESHttp*"],
            "arn:aws:es:us-east-1:*:domain/mydomain/*",
        )

    when_appsync_ready(api, check_resources)


# --- RDS data source ---


@pulumi.runtime.test
def test_rds_data_source_creates_iam_policy(pulumi_mocks, project_cwd):
    api = make_api()
    db = api.data_source_rds(
        "db",
        cluster_arn="arn:aws:rds:us-east-1:123456789012:cluster:my-cluster",
        secret_arn="arn:aws:secretsmanager:us-east-1:123456789012:secret:my-secret",  # noqa: S106
        database="mydb",
    )
    api.query("getPost", db, code="resolvers/getItem.js")

    def check_resources(_):
        stmts = assert_iam_policy(pulumi_mocks, "ds-db-policy", [], [], statement_count=2)
        actions = [s["Action"] for s in stmts]
        resources = [s["Resource"] for s in stmts]
        for stmt in stmts:
            assert stmt["Effect"] == "Allow"
        assert [
            "rds-data:ExecuteStatement",
            "rds-data:BatchExecuteStatement",
            "rds-data:BeginTransaction",
            "rds-data:CommitTransaction",
            "rds-data:RollbackTransaction",
        ] in actions
        assert ["secretsmanager:GetSecretValue"] in actions
        assert "arn:aws:rds:us-east-1:123456789012:cluster:my-cluster" in resources
        assert "arn:aws:secretsmanager:us-east-1:123456789012:secret:my-secret" in resources

    when_appsync_ready(api, check_resources)


# --- Validation ---


@pytest.mark.parametrize(
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
    with pytest.raises(ValueError, match=match):
        setup(api)


def test_duplicate_data_source_name(project_cwd):
    api = make_api()
    api.data_source_lambda("posts", handler="functions/simple.handler")
    with pytest.raises(ValueError, match="Duplicate data source name 'posts'"):
        api.data_source_lambda("posts", handler="functions/simple.handler")


def test_lambda_data_source_handler_required(project_cwd):
    api = make_api()
    with pytest.raises(TypeError):
        api.data_source_lambda("posts")


def test_dynamo_data_source_requires_dynamo_table_component(project_cwd):
    api = make_api()
    with pytest.raises(TypeError, match="table must be a DynamoTable"):
        api.data_source_dynamo("items", table=object())


@pytest.mark.parametrize(
    "extra_kwargs",
    [{"memory": 256}, {"links": []}],
    ids=["with-fn-opts", "with-links"],
)
def test_lambda_data_source_function_instance_extra_opts_raises(extra_kwargs, project_cwd):
    """Passing a Function instance with extra fn_opts or links should raise ValueError."""
    api = make_api()
    fn = Function("my-fn", handler="functions/simple.handler")
    with pytest.raises(
        ValueError, match="Cannot specify function options when handler is a Function"
    ):
        api.data_source_lambda("posts", fn, **extra_kwargs)


@pulumi.runtime.test
def test_none_data_source_created(pulumi_mocks, project_cwd):
    """The internal shared NONE data source is created when resources are built."""
    api = make_api()
    api.mutation("sendMessage", None)

    def check_resources(_):
        assert_data_source_inputs(pulumi_mocks, ds_type="NONE", name="NONE")

    when_appsync_ready(api, check_resources)


@pulumi.runtime.test
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

    def check_resources(_):
        assert_data_source_inputs(
            pulumi_mocks,
            ds_type=DS_TYPE_LAMBDA,
            name="custom-posts",
        )
        assert_role(pulumi_mocks, "ds-posts-role", path="/service-role/")

    when_appsync_ready(api, check_resources)


# --- Customize key validation ---


@pytest.mark.parametrize(
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
    with pytest.raises(ValueError, match="Unknown customization key"):
        make_data_source(api, ds_type, customize={bad_key: {}})
