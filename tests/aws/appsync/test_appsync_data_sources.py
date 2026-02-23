"""AppSync data source tests — Lambda, DynamoDB, HTTP, RDS, OpenSearch."""

import json

import pulumi
import pytest

from stelvio.aws.appsync import AppSync, CognitoAuth
from stelvio.aws.appsync.constants import (
    DS_TYPE_DYNAMO,
    DS_TYPE_HTTP,
    DS_TYPE_LAMBDA,
    DS_TYPE_OPENSEARCH,
    DS_TYPE_RDS,
)
from stelvio.aws.dynamo_db import DynamoTable
from stelvio.aws.function import Function, FunctionConfig

from .conftest import COGNITO_USER_POOL_ID, INLINE_SCHEMA

TP = "test-test-"


def _make_api(name="myapi"):
    return AppSync(name, INLINE_SCHEMA, auth=CognitoAuth(user_pool_id=COGNITO_USER_POOL_ID))


# --- Lambda data source ---


@pulumi.runtime.test
def test_lambda_data_source_creates_resources(pulumi_mocks, project_cwd):
    api = _make_api()
    posts = api.data_source_lambda("posts", handler="functions/simple.handler")
    api.query("getPost", posts)
    _ = api.resources

    def check_resources(_):
        # Data source created
        data_sources = pulumi_mocks.created_appsync_data_sources()
        lambda_ds = [ds for ds in data_sources if ds.inputs.get("type") == DS_TYPE_LAMBDA]
        assert len(lambda_ds) == 1
        assert lambda_ds[0].inputs["name"] == "posts"

        # Lambda function created
        fns = pulumi_mocks.created_functions()
        ds_fns = [f for f in fns if "ds-posts" in f.name]
        assert len(ds_fns) == 1

        # IAM role created for data source
        roles = pulumi_mocks.created_roles()
        ds_roles = [r for r in roles if "ds-posts-role" in r.name]
        assert len(ds_roles) == 1

    api.resources.completed.apply(check_resources)


@pulumi.runtime.test
def test_lambda_data_source_with_function_config(pulumi_mocks, project_cwd):
    api = _make_api()
    config = FunctionConfig(handler="functions/simple.handler", memory=512, timeout=30)
    posts = api.data_source_lambda("posts", config)
    api.query("getPost", posts)
    _ = api.resources

    def check_resources(_):
        fns = pulumi_mocks.created_functions()
        ds_fns = [f for f in fns if "ds-posts" in f.name]
        assert len(ds_fns) == 1
        assert ds_fns[0].inputs["memorySize"] == 512
        assert ds_fns[0].inputs["timeout"] == 30

    api.resources.completed.apply(check_resources)


@pulumi.runtime.test
def test_lambda_data_source_with_function_instance(pulumi_mocks, project_cwd):
    api = _make_api()
    fn = Function("my-fn", handler="functions/simple.handler")
    posts = api.data_source_lambda("posts", fn)
    api.query("getPost", posts)
    _ = api.resources

    def check_resources(_):
        data_sources = pulumi_mocks.created_appsync_data_sources()
        lambda_ds = [ds for ds in data_sources if ds.inputs.get("type") == DS_TYPE_LAMBDA]
        assert len(lambda_ds) == 1

    api.resources.completed.apply(check_resources)


@pulumi.runtime.test
def test_lambda_data_source_with_fn_opts(pulumi_mocks, project_cwd):
    api = _make_api()
    posts = api.data_source_lambda("posts", handler="functions/simple.handler", memory=256)
    api.query("getPost", posts)
    _ = api.resources

    def check_resources(_):
        fns = pulumi_mocks.created_functions()
        ds_fns = [f for f in fns if "ds-posts" in f.name]
        assert len(ds_fns) == 1
        assert ds_fns[0].inputs["memorySize"] == 256

    api.resources.completed.apply(check_resources)


@pulumi.runtime.test
def test_lambda_data_source_creates_iam_policy(pulumi_mocks, project_cwd):
    api = _make_api()
    posts = api.data_source_lambda("posts", handler="functions/simple.handler")
    api.query("getPost", posts)
    _ = api.resources

    def check_resources(_):
        policies = pulumi_mocks.created_role_policies()
        lambda_policies = [p for p in policies if "lambda-policy" in p.name]
        assert len(lambda_policies) == 1
        policy_doc = json.loads(lambda_policies[0].inputs["policy"])
        assert policy_doc["Statement"][0]["Action"] == ["lambda:InvokeFunction"]

    api.resources.completed.apply(check_resources)


@pulumi.runtime.test
def test_lambda_data_source_resources_accessible(pulumi_mocks, project_cwd):
    api = _make_api()
    posts = api.data_source_lambda("posts", handler="functions/simple.handler")
    api.query("getPost", posts)

    def check_resources(_):
        assert posts.resources is not None
        assert posts.resources.data_source is not None
        assert posts.resources.service_role is not None
        assert posts.resources.function is not None

    api.resources.completed.apply(check_resources)


# --- DynamoDB data source ---


@pulumi.runtime.test
def test_dynamo_data_source_creates_resources(pulumi_mocks, project_cwd):
    api = _make_api()
    table = DynamoTable("items", fields={"pk": "S"}, partition_key="pk")
    items = api.data_source_dynamo("items", table=table)
    api.query("getItem", items, code="resolvers/getItem.js")
    _ = api.resources

    def check_resources(_):
        data_sources = pulumi_mocks.created_appsync_data_sources()
        dynamo_ds = [ds for ds in data_sources if ds.inputs.get("type") == DS_TYPE_DYNAMO]
        assert len(dynamo_ds) == 1
        assert dynamo_ds[0].inputs["name"] == "items"
        assert "dynamodbConfig" in dynamo_ds[0].inputs

    api.resources.completed.apply(check_resources)


@pulumi.runtime.test
def test_dynamo_data_source_creates_iam_policy(pulumi_mocks, project_cwd):
    api = _make_api()
    table = DynamoTable("items", fields={"pk": "S"}, partition_key="pk")
    items = api.data_source_dynamo("items", table=table)
    api.query("getItem", items, code="resolvers/getItem.js")
    _ = api.resources

    def check_resources(_):
        policies = pulumi_mocks.created_role_policies()
        dynamo_policies = [p for p in policies if "dynamo-policy" in p.name]
        assert len(dynamo_policies) == 1
        policy_doc = json.loads(dynamo_policies[0].inputs["policy"])
        stmt = policy_doc["Statement"][0]
        assert "dynamodb:GetItem" in stmt["Action"]
        assert "dynamodb:PutItem" in stmt["Action"]
        assert "dynamodb:Query" in stmt["Action"]
        assert "dynamodb:Scan" in stmt["Action"]

    api.resources.completed.apply(check_resources)


# --- HTTP data source ---


@pulumi.runtime.test
def test_http_data_source_creates_resources(pulumi_mocks, project_cwd):
    api = _make_api()
    ext = api.data_source_http("ext", url="https://api.example.com")
    api.query("getPost", ext, code="resolvers/getItem.js")
    _ = api.resources

    def check_resources(_):
        data_sources = pulumi_mocks.created_appsync_data_sources()
        http_ds = [ds for ds in data_sources if ds.inputs.get("type") == DS_TYPE_HTTP]
        assert len(http_ds) == 1
        assert http_ds[0].inputs["name"] == "ext"
        assert http_ds[0].inputs["httpConfig"]["endpoint"] == "https://api.example.com"

    api.resources.completed.apply(check_resources)


# --- RDS data source ---


@pulumi.runtime.test
def test_rds_data_source_creates_resources(pulumi_mocks, project_cwd):
    api = _make_api()
    db = api.data_source_rds(
        "db",
        cluster_arn="arn:aws:rds:us-east-1:123456789012:cluster:my-cluster",
        secret_arn="arn:aws:secretsmanager:us-east-1:123456789012:secret:my-secret",
        database="mydb",
    )
    api.query("getPost", db, code="resolvers/getItem.js")
    _ = api.resources

    def check_resources(_):
        data_sources = pulumi_mocks.created_appsync_data_sources()
        rds_ds = [ds for ds in data_sources if ds.inputs.get("type") == DS_TYPE_RDS]
        assert len(rds_ds) == 1
        assert rds_ds[0].inputs["name"] == "db"
        rdb_config = rds_ds[0].inputs["relationalDatabaseConfig"]
        assert rdb_config["httpEndpointConfig"]["databaseName"] == "mydb"

    api.resources.completed.apply(check_resources)


@pulumi.runtime.test
def test_rds_data_source_creates_iam_policy(pulumi_mocks, project_cwd):
    api = _make_api()
    db = api.data_source_rds(
        "db",
        cluster_arn="arn:aws:rds:us-east-1:123456789012:cluster:my-cluster",
        secret_arn="arn:aws:secretsmanager:us-east-1:123456789012:secret:my-secret",
        database="mydb",
    )
    api.query("getPost", db, code="resolvers/getItem.js")
    _ = api.resources

    def check_resources(_):
        policies = pulumi_mocks.created_role_policies()
        # RDS uses static inline policy (not Output-based)
        rds_policies = [p for p in policies if "ds-db-policy" in p.name]
        assert len(rds_policies) == 1
        policy_doc = json.loads(rds_policies[0].inputs["policy"])
        actions = [s["Action"] for s in policy_doc["Statement"]]
        assert [
            "rds-data:ExecuteStatement",
            "rds-data:BatchExecuteStatement",
            "rds-data:BeginTransaction",
            "rds-data:CommitTransaction",
            "rds-data:RollbackTransaction",
        ] in actions
        assert ["secretsmanager:GetSecretValue"] in actions

    api.resources.completed.apply(check_resources)


# --- OpenSearch data source ---


@pulumi.runtime.test
def test_opensearch_data_source_creates_resources(pulumi_mocks, project_cwd):
    api = _make_api()
    search = api.data_source_opensearch(
        "search", endpoint="https://search-domain-abc123def456ghij.us-east-1.es.amazonaws.com"
    )
    api.query("getPost", search, code="resolvers/getItem.js")
    _ = api.resources

    def check_resources(_):
        data_sources = pulumi_mocks.created_appsync_data_sources()
        es_ds = [ds for ds in data_sources if ds.inputs.get("type") == DS_TYPE_OPENSEARCH]
        assert len(es_ds) == 1
        assert es_ds[0].inputs["name"] == "search"

    api.resources.completed.apply(check_resources)


@pulumi.runtime.test
def test_opensearch_data_source_iam_policy_uses_arn(pulumi_mocks, project_cwd):
    api = _make_api()
    search = api.data_source_opensearch(
        "search", endpoint="https://search-mydomain-abc123def456ghij.us-east-1.es.amazonaws.com"
    )
    api.query("getPost", search, code="resolvers/getItem.js")
    _ = api.resources

    def check_resources(_):
        policies = pulumi_mocks.created_role_policies()
        es_policies = [p for p in policies if "ds-search-policy" in p.name]
        assert len(es_policies) == 1
        policy_doc = json.loads(es_policies[0].inputs["policy"])
        stmt = policy_doc["Statement"][0]
        assert stmt["Action"] == ["es:ESHttp*"]
        assert stmt["Resource"] == "arn:aws:es:us-east-1:*:domain/mydomain/*"

    api.resources.completed.apply(check_resources)


# --- Validation ---


def test_duplicate_data_source_name(project_cwd):
    api = _make_api()
    api.data_source_lambda("posts", handler="functions/simple.handler")
    with pytest.raises(ValueError, match="Duplicate data source name 'posts'"):
        api.data_source_lambda("posts", handler="functions/simple.handler")


def test_empty_data_source_name(project_cwd):
    api = _make_api()
    with pytest.raises(ValueError, match="Data source name cannot be empty"):
        api.data_source_lambda("", handler="functions/simple.handler")


def test_http_data_source_empty_url(project_cwd):
    api = _make_api()
    with pytest.raises(ValueError, match="url cannot be empty"):
        api.data_source_http("ext", url="")


def test_rds_data_source_empty_cluster_arn(project_cwd):
    api = _make_api()
    with pytest.raises(ValueError, match="cluster_arn cannot be empty"):
        api.data_source_rds("db", cluster_arn="", secret_arn="x", database="y")


def test_rds_data_source_empty_secret_arn(project_cwd):
    api = _make_api()
    with pytest.raises(ValueError, match="secret_arn cannot be empty"):
        api.data_source_rds("db", cluster_arn="x", secret_arn="", database="y")


def test_rds_data_source_empty_database(project_cwd):
    api = _make_api()
    with pytest.raises(ValueError, match="database cannot be empty"):
        api.data_source_rds("db", cluster_arn="x", secret_arn="y", database="")


def test_opensearch_data_source_empty_endpoint(project_cwd):
    api = _make_api()
    with pytest.raises(ValueError, match="endpoint cannot be empty"):
        api.data_source_opensearch("search", endpoint="")


def test_opensearch_data_source_invalid_endpoint_format(project_cwd):
    api = _make_api()
    with pytest.raises(ValueError, match="Cannot derive domain ARN"):
        api.data_source_opensearch("search", endpoint="https://not-valid-endpoint.com")


def test_lambda_data_source_handler_required(project_cwd):
    api = _make_api()
    with pytest.raises(TypeError):
        api.data_source_lambda("posts")


@pulumi.runtime.test
def test_none_data_source_created(pulumi_mocks, project_cwd):
    """The internal shared NONE data source is created when resources are built."""
    api = _make_api()
    api.mutation("sendMessage", None)
    _ = api.resources

    def check_resources(_):
        data_sources = pulumi_mocks.created_appsync_data_sources()
        none_ds = [ds for ds in data_sources if ds.inputs.get("type") == "NONE"]
        assert len(none_ds) == 1
        assert none_ds[0].inputs["name"] == "NONE"

    api.resources.completed.apply(check_resources)


def test_data_source_resources_before_api_resources(project_cwd):
    """Accessing data source resources before api.resources triggers RuntimeError."""
    api = _make_api()
    ds = api.data_source_lambda("posts", handler="functions/simple.handler")
    with pytest.raises(RuntimeError, match="resources have not been created yet"):
        _ = ds.resources


@pulumi.runtime.test
def test_data_source_customize_applied(pulumi_mocks, project_cwd):
    api = _make_api()
    posts = api.data_source_lambda(
        "posts",
        handler="functions/simple.handler",
        customize={
            "data_source": {"name": "custom-posts"},
            "service_role": {"path": "/service-role/"},
        },
    )
    api.query("getPost", posts)
    _ = api.resources

    def check_resources(_):
        data_sources = pulumi_mocks.created_appsync_data_sources()
        lambda_ds = [ds for ds in data_sources if ds.inputs.get("type") == DS_TYPE_LAMBDA]
        assert len(lambda_ds) == 1
        assert lambda_ds[0].inputs["name"] == "custom-posts"

        roles = pulumi_mocks.created_roles()
        ds_roles = [r for r in roles if "ds-posts-role" in r.name]
        assert len(ds_roles) == 1
        assert ds_roles[0].inputs["path"] == "/service-role/"

    api.resources.completed.apply(check_resources)


# --- Customize key validation ---


def test_lambda_data_source_invalid_customize_key(project_cwd):
    api = _make_api()
    with pytest.raises(ValueError, match=r"Invalid customize key.*datasource"):
        api.data_source_lambda(
            "posts",
            handler="functions/simple.handler",
            customize={"datasource": {"name": "x"}},
        )


def test_dynamo_data_source_invalid_customize_key(project_cwd):
    api = _make_api()
    table = DynamoTable("items", fields={"pk": "S"}, partition_key="pk")
    with pytest.raises(ValueError, match="Invalid customize key"):
        api.data_source_dynamo("items", table=table, customize={"role": {"path": "/x/"}})


def test_http_data_source_invalid_customize_key(project_cwd):
    api = _make_api()
    with pytest.raises(ValueError, match="Invalid customize key"):
        api.data_source_http("ext", url="https://example.com", customize={"ds": {}})


def test_rds_data_source_invalid_customize_key(project_cwd):
    api = _make_api()
    with pytest.raises(ValueError, match="Invalid customize key"):
        api.data_source_rds(
            "db",
            cluster_arn="arn:aws:rds:us-east-1:123456789012:cluster:c",
            secret_arn="arn:aws:secretsmanager:us-east-1:123456789012:secret:s",
            database="mydb",
            customize={"source": {}},
        )


def test_opensearch_data_source_invalid_customize_key(project_cwd):
    api = _make_api()
    with pytest.raises(ValueError, match="Invalid customize key"):
        api.data_source_opensearch(
            "search",
            endpoint="https://search-domain-abc123def456ghij.us-east-1.es.amazonaws.com",
            customize={"opensearch": {}},
        )


# --- OpenSearch config key ---


@pulumi.runtime.test
def test_opensearch_data_source_uses_correct_config_key(pulumi_mocks, project_cwd):
    """OpenSearch data source must use opensearchserviceConfig, not elasticsearchConfig."""
    api = _make_api()
    search = api.data_source_opensearch(
        "search", endpoint="https://search-domain-abc123def456ghij.us-east-1.es.amazonaws.com"
    )
    api.query("getPost", search, code="resolvers/getItem.js")
    _ = api.resources

    def check_resources(_):
        data_sources = pulumi_mocks.created_appsync_data_sources()
        es_ds = [ds for ds in data_sources if ds.inputs.get("type") == DS_TYPE_OPENSEARCH]
        assert len(es_ds) == 1
        assert "opensearchserviceConfig" in es_ds[0].inputs
        assert "elasticsearchConfig" not in es_ds[0].inputs

    api.resources.completed.apply(check_resources)


# --- DynamoDB index policy ---


@pulumi.runtime.test
def test_dynamo_data_source_iam_policy_includes_index_resources(pulumi_mocks, project_cwd):
    """DynamoDB IAM policy must include both table ARN and table ARN/index/*."""
    api = _make_api()
    table = DynamoTable("items", fields={"pk": "S"}, partition_key="pk")
    items = api.data_source_dynamo("items", table=table)
    api.query("getItem", items, code="resolvers/getItem.js")
    _ = api.resources

    def check_resources(_):
        policies = pulumi_mocks.created_role_policies()
        dynamo_policies = [p for p in policies if "dynamo-policy" in p.name]
        assert len(dynamo_policies) == 1
        policy_doc = json.loads(dynamo_policies[0].inputs["policy"])
        resources = policy_doc["Statement"][0]["Resource"]
        assert isinstance(resources, list)
        assert len(resources) == 2
        assert resources[1].endswith("/index/*")

    api.resources.completed.apply(check_resources)
