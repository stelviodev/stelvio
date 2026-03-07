"""AppSync data source tests — Lambda, DynamoDB, HTTP, RDS, OpenSearch."""

import json

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

from .conftest import make_api, make_data_source, make_dynamo_ds, when_appsync_ready

TP = "test-test-"


# --- Lambda data source ---


@pulumi.runtime.test
def test_lambda_data_source_creates_resources(pulumi_mocks, project_cwd):
    api = make_api()
    posts = api.data_source_lambda("posts", handler="functions/simple.handler")
    api.query("getPost", posts)

    def check_resources(_):
        # Data source created
        data_sources = pulumi_mocks.created_appsync_data_sources()
        lambda_ds = [ds for ds in data_sources if ds.inputs["type"] == DS_TYPE_LAMBDA]
        assert len(lambda_ds) == 1
        assert lambda_ds[0].typ == "aws:appsync/dataSource:DataSource"
        assert lambda_ds[0].inputs["name"] == "posts"

        # Lambda function created
        fns = pulumi_mocks.created_functions()
        ds_fns = [f for f in fns if "ds-posts" in f.name]
        assert len(ds_fns) == 1

        # IAM role created for data source
        roles = pulumi_mocks.created_roles()
        ds_roles = [r for r in roles if "ds-posts-role" in r.name]
        assert len(ds_roles) == 1

    when_appsync_ready(api, check_resources)


@pulumi.runtime.test
def test_lambda_data_source_with_function_config(pulumi_mocks, project_cwd):
    api = make_api()
    config = FunctionConfig(handler="functions/simple.handler", memory=512, timeout=30)
    posts = api.data_source_lambda("posts", config)
    api.query("getPost", posts)

    def check_resources(_):
        fns = pulumi_mocks.created_functions()
        ds_fns = [f for f in fns if "ds-posts" in f.name]
        assert len(ds_fns) == 1
        assert ds_fns[0].typ == "aws:lambda/function:Function"
        assert ds_fns[0].inputs["memorySize"] == 512
        assert ds_fns[0].inputs["timeout"] == 30

    when_appsync_ready(api, check_resources)


@pulumi.runtime.test
def test_lambda_data_source_with_function_instance(pulumi_mocks, project_cwd):
    api = make_api()
    fn = Function("my-fn", handler="functions/simple.handler")
    posts = api.data_source_lambda("posts", fn)
    api.query("getPost", posts)

    def check_resources(_):
        data_sources = pulumi_mocks.created_appsync_data_sources()
        lambda_ds = [ds for ds in data_sources if ds.inputs["type"] == DS_TYPE_LAMBDA]
        assert len(lambda_ds) == 1

    when_appsync_ready(api, check_resources)


@pulumi.runtime.test
def test_lambda_data_source_with_fn_opts(pulumi_mocks, project_cwd):
    api = make_api()
    posts = api.data_source_lambda("posts", handler="functions/simple.handler", memory=256)
    api.query("getPost", posts)

    def check_resources(_):
        fns = pulumi_mocks.created_functions()
        ds_fns = [f for f in fns if "ds-posts" in f.name]
        assert len(ds_fns) == 1
        assert ds_fns[0].typ == "aws:lambda/function:Function"
        assert ds_fns[0].inputs["memorySize"] == 256

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
        assert env_vars["STLV_ITEMS_TABLE_ARN"].startswith(
            "arn:aws:dynamodb:us-east-1:123456789012:table/"
        )
        assert env_vars["STLV_ITEMS_TABLE_NAME"] == f"{TP}items-test-name"

    when_appsync_ready(api, check_resources)


@pulumi.runtime.test
def test_lambda_data_source_creates_iam_policy(pulumi_mocks, project_cwd):
    api = make_api()
    posts = api.data_source_lambda("posts", handler="functions/simple.handler")
    api.query("getPost", posts)

    def check_resources(_):
        policies = pulumi_mocks.created_role_policies()
        lambda_policies = [p for p in policies if "lambda-policy" in p.name]
        assert len(lambda_policies) == 1
        assert lambda_policies[0].typ == "aws:iam/rolePolicy:RolePolicy"
        policy_doc = json.loads(lambda_policies[0].inputs["policy"])
        assert len(policy_doc["Statement"]) == 1
        stmt = policy_doc["Statement"][0]
        assert stmt["Effect"] == "Allow"
        assert stmt["Action"] == ["lambda:InvokeFunction"]
        expected_fn_arn = (
            "arn:aws:lambda:us-east-1:123456789012:function:test-test-myapi-ds-posts-fn-test-name"
        )
        assert stmt["Resource"] == expected_fn_arn

    when_appsync_ready(api, check_resources)


@pulumi.runtime.test
def test_lambda_data_source_resources_accessible(pulumi_mocks, project_cwd):
    api = make_api()
    posts = api.data_source_lambda("posts", handler="functions/simple.handler")
    api.query("getPost", posts)

    def check_resources(_):
        assert posts.resources is not None
        assert isinstance(posts.resources.data_source, appsync.DataSource)
        assert isinstance(posts.resources.service_role, iam.Role)
        assert isinstance(posts.resources.function, Function)

    when_appsync_ready(api, check_resources)


# --- DynamoDB data source ---


@pulumi.runtime.test
def test_dynamo_data_source_creates_resources(pulumi_mocks, project_cwd):
    api = make_api()
    items, _ = make_dynamo_ds(api)
    api.query("getItem", items, code="resolvers/getItem.js")

    def check_resources(_):
        data_sources = pulumi_mocks.created_appsync_data_sources()
        dynamo_ds = [ds for ds in data_sources if ds.inputs["type"] == DS_TYPE_DYNAMO]
        assert len(dynamo_ds) == 1
        assert dynamo_ds[0].typ == "aws:appsync/dataSource:DataSource"
        assert dynamo_ds[0].inputs["name"] == "items"
        assert "dynamodbConfig" in dynamo_ds[0].inputs

    when_appsync_ready(api, check_resources)


@pulumi.runtime.test
def test_dynamo_data_source_creates_iam_policy(pulumi_mocks, project_cwd):
    api = make_api()
    items, _ = make_dynamo_ds(api)
    api.query("getItem", items, code="resolvers/getItem.js")

    def check_resources(_):
        policies = pulumi_mocks.created_role_policies()
        dynamo_policies = [p for p in policies if "dynamo-policy" in p.name]
        assert len(dynamo_policies) == 1
        assert dynamo_policies[0].typ == "aws:iam/rolePolicy:RolePolicy"
        policy_doc = json.loads(dynamo_policies[0].inputs["policy"])
        assert len(policy_doc["Statement"]) == 1
        stmt = policy_doc["Statement"][0]
        assert stmt["Effect"] == "Allow"
        assert set(stmt["Action"]) == {
            "dynamodb:GetItem",
            "dynamodb:PutItem",
            "dynamodb:UpdateItem",
            "dynamodb:DeleteItem",
            "dynamodb:Query",
            "dynamodb:Scan",
        }
        assert stmt["Resource"] == [
            "arn:aws:dynamodb:us-east-1:123456789012:table/test-test-items-test-name",
            "arn:aws:dynamodb:us-east-1:123456789012:table/test-test-items-test-name/index/*",
        ]

    when_appsync_ready(api, check_resources)


# --- HTTP data source ---


@pulumi.runtime.test
def test_http_data_source_creates_resources(pulumi_mocks, project_cwd):
    api = make_api()
    ext = api.data_source_http("ext", url="https://api.example.com")
    api.query("getPost", ext, code="resolvers/getItem.js")

    def check_resources(_):
        data_sources = pulumi_mocks.created_appsync_data_sources()
        http_ds = [ds for ds in data_sources if ds.inputs["type"] == DS_TYPE_HTTP]
        assert len(http_ds) == 1
        assert http_ds[0].typ == "aws:appsync/dataSource:DataSource"
        assert http_ds[0].inputs["name"] == "ext"
        assert http_ds[0].inputs["httpConfig"]["endpoint"] == "https://api.example.com"

    when_appsync_ready(api, check_resources)


@pulumi.runtime.test
def test_http_data_source_creates_service_role_without_policy(pulumi_mocks, project_cwd):
    api = make_api()
    ext = api.data_source_http("ext", url="https://api.example.com")
    api.query("getPost", ext, code="resolvers/getItem.js")

    def check_resources(_):
        roles = pulumi_mocks.created_roles()
        ds_roles = [r for r in roles if "ds-ext-role" in r.name]
        assert len(ds_roles) == 1

        policies = pulumi_mocks.created_role_policies()
        ext_policies = [p for p in policies if "ds-ext" in p.name]
        assert len(ext_policies) == 0

    when_appsync_ready(api, check_resources)


# --- RDS data source ---


@pulumi.runtime.test
def test_rds_data_source_creates_resources(pulumi_mocks, project_cwd):
    api = make_api()
    db = api.data_source_rds(
        "db",
        cluster_arn="arn:aws:rds:us-east-1:123456789012:cluster:my-cluster",
        secret_arn="arn:aws:secretsmanager:us-east-1:123456789012:secret:my-secret",  # noqa: S106
        database="mydb",
    )
    api.query("getPost", db, code="resolvers/getItem.js")

    def check_resources(_):
        data_sources = pulumi_mocks.created_appsync_data_sources()
        rds_ds = [ds for ds in data_sources if ds.inputs["type"] == DS_TYPE_RDS]
        assert len(rds_ds) == 1
        assert rds_ds[0].typ == "aws:appsync/dataSource:DataSource"
        assert rds_ds[0].inputs["name"] == "db"
        rdb_config = rds_ds[0].inputs["relationalDatabaseConfig"]
        assert rdb_config["httpEndpointConfig"]["databaseName"] == "mydb"

    when_appsync_ready(api, check_resources)


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
        policies = pulumi_mocks.created_role_policies()
        # RDS uses static inline policy (not Output-based)
        rds_policies = [p for p in policies if "ds-db-policy" in p.name]
        assert len(rds_policies) == 1
        assert rds_policies[0].typ == "aws:iam/rolePolicy:RolePolicy"
        policy_doc = json.loads(rds_policies[0].inputs["policy"])
        assert len(policy_doc["Statement"]) == 2
        actions = [s["Action"] for s in policy_doc["Statement"]]
        resources = [s["Resource"] for s in policy_doc["Statement"]]
        for stmt in policy_doc["Statement"]:
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


# --- OpenSearch data source ---


@pulumi.runtime.test
def test_opensearch_data_source_creates_resources(pulumi_mocks, project_cwd):
    api = make_api()
    search = api.data_source_opensearch(
        "search", endpoint="https://search-domain-abc123def456ghij.us-east-1.es.amazonaws.com"
    )
    api.query("getPost", search, code="resolvers/getItem.js")

    def check_resources(_):
        data_sources = pulumi_mocks.created_appsync_data_sources()
        es_ds = [ds for ds in data_sources if ds.inputs["type"] == DS_TYPE_OPENSEARCH]
        assert len(es_ds) == 1
        assert es_ds[0].typ == "aws:appsync/dataSource:DataSource"
        assert es_ds[0].inputs["name"] == "search"

    when_appsync_ready(api, check_resources)


@pulumi.runtime.test
def test_opensearch_data_source_iam_policy_uses_arn(pulumi_mocks, project_cwd):
    api = make_api()
    search = api.data_source_opensearch(
        "search",
        endpoint="https://search-my-domain-abc123def456ghij.us-east-1.es.amazonaws.com",
    )
    api.query("getPost", search, code="resolvers/getItem.js")

    def check_resources(_):
        policies = pulumi_mocks.created_role_policies()
        es_policies = [p for p in policies if "ds-search-policy" in p.name]
        assert len(es_policies) == 1
        assert es_policies[0].typ == "aws:iam/rolePolicy:RolePolicy"
        policy_doc = json.loads(es_policies[0].inputs["policy"])
        assert len(policy_doc["Statement"]) == 1
        stmt = policy_doc["Statement"][0]
        assert stmt["Effect"] == "Allow"
        assert stmt["Action"] == ["es:ESHttp*"]
        assert stmt["Resource"] == "arn:aws:es:us-east-1:*:domain/my-domain/*"

    when_appsync_ready(api, check_resources)


# --- Validation ---


def test_duplicate_data_source_name(project_cwd):
    api = make_api()
    api.data_source_lambda("posts", handler="functions/simple.handler")
    with pytest.raises(ValueError, match="Duplicate data source name 'posts'"):
        api.data_source_lambda("posts", handler="functions/simple.handler")


def test_empty_data_source_name(project_cwd):
    api = make_api()
    with pytest.raises(ValueError, match="Data source name cannot be empty"):
        api.data_source_lambda("", handler="functions/simple.handler")


def test_http_data_source_empty_url(project_cwd):
    api = make_api()
    with pytest.raises(ValueError, match="url cannot be empty"):
        api.data_source_http("ext", url="")


def test_rds_data_source_empty_cluster_arn(project_cwd):
    api = make_api()
    with pytest.raises(ValueError, match="cluster_arn cannot be empty"):
        api.data_source_rds("db", cluster_arn="", secret_arn="x", database="y")  # noqa: S106


def test_rds_data_source_empty_secret_arn(project_cwd):
    api = make_api()
    with pytest.raises(ValueError, match="secret_arn cannot be empty"):
        api.data_source_rds("db", cluster_arn="x", secret_arn="", database="y")


def test_rds_data_source_empty_database(project_cwd):
    api = make_api()
    with pytest.raises(ValueError, match="database cannot be empty"):
        api.data_source_rds("db", cluster_arn="x", secret_arn="y", database="")  # noqa: S106


# --- OpenSearch ARN extraction ---


@pulumi.runtime.test
def test_opensearch_data_source_vpc_prefix_endpoint(pulumi_mocks, project_cwd):
    """OpenSearch endpoint with vpc- prefix should derive correct domain ARN."""
    api = make_api()
    search = api.data_source_opensearch(
        "search",
        endpoint="https://vpc-mydomain-abc123def456ghij.us-east-1.es.amazonaws.com",
    )
    api.query("getPost", search, code="resolvers/getItem.js")

    def check_resources(_):
        policies = pulumi_mocks.created_role_policies()
        es_policies = [p for p in policies if "ds-search-policy" in p.name]
        assert len(es_policies) == 1
        policy_doc = json.loads(es_policies[0].inputs["policy"])
        stmt = policy_doc["Statement"][0]
        assert stmt["Resource"] == "arn:aws:es:us-east-1:*:domain/mydomain/*"

    when_appsync_ready(api, check_resources)


def test_opensearch_data_source_empty_endpoint(project_cwd):
    api = make_api()
    with pytest.raises(ValueError, match="endpoint cannot be empty"):
        api.data_source_opensearch("search", endpoint="")


def test_opensearch_data_source_invalid_endpoint_format(project_cwd):
    api = make_api()
    with pytest.raises(ValueError, match="Cannot derive domain ARN"):
        api.data_source_opensearch("search", endpoint="https://not-valid-endpoint.com")


def test_lambda_data_source_handler_required(project_cwd):
    api = make_api()
    with pytest.raises(TypeError):
        api.data_source_lambda("posts")


def test_reserved_none_data_source_name(project_cwd):
    """Data source named 'NONE' should be rejected — conflicts with internal NONE DS."""
    api = make_api()
    with pytest.raises(ValueError, match="reserved"):
        api.data_source_lambda("NONE", handler="functions/simple.handler")


def test_dynamo_data_source_requires_dynamo_table_component(project_cwd):
    api = make_api()
    with pytest.raises(TypeError, match="table must be a DynamoTable component"):
        api.data_source_dynamo("items", table=object())


def test_lambda_data_source_function_instance_with_links_raises(project_cwd):
    """Passing a Function instance with links= should raise ValueError."""
    api = make_api()
    fn = Function("my-fn", handler="functions/simple.handler")
    with pytest.raises(
        ValueError, match="Cannot specify function options when handler is a Function"
    ):
        api.data_source_lambda("posts", fn, links=[fn])


def test_lambda_data_source_function_instance_with_fn_opts_raises(project_cwd):
    """Passing a Function instance with extra fn_opts should raise ValueError."""
    api = make_api()
    fn = Function("my-fn", handler="functions/simple.handler")
    with pytest.raises(
        ValueError, match="Cannot specify function options when handler is a Function"
    ):
        api.data_source_lambda("posts", fn, memory=256)


@pulumi.runtime.test
def test_none_data_source_created(pulumi_mocks, project_cwd):
    """The internal shared NONE data source is created when resources are built."""
    api = make_api()
    api.mutation("sendMessage", None)

    def check_resources(_):
        data_sources = pulumi_mocks.created_appsync_data_sources()
        none_ds = [ds for ds in data_sources if ds.inputs["type"] == "NONE"]
        assert len(none_ds) == 1
        assert none_ds[0].inputs["name"] == "NONE"

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
        data_sources = pulumi_mocks.created_appsync_data_sources()
        lambda_ds = [ds for ds in data_sources if ds.inputs["type"] == DS_TYPE_LAMBDA]
        assert len(lambda_ds) == 1
        assert lambda_ds[0].inputs["name"] == "custom-posts"

        roles = pulumi_mocks.created_roles()
        ds_roles = [r for r in roles if "ds-posts-role" in r.name]
        assert len(ds_roles) == 1
        assert ds_roles[0].inputs["path"] == "/service-role/"

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


# --- OpenSearch config key ---


@pulumi.runtime.test
def test_opensearch_data_source_uses_correct_config_key(pulumi_mocks, project_cwd):
    """OpenSearch data source must use opensearchserviceConfig, not elasticsearchConfig."""
    api = make_api()
    search = api.data_source_opensearch(
        "search", endpoint="https://search-domain-abc123def456ghij.us-east-1.es.amazonaws.com"
    )
    api.query("getPost", search, code="resolvers/getItem.js")

    def check_resources(_):
        data_sources = pulumi_mocks.created_appsync_data_sources()
        es_ds = [ds for ds in data_sources if ds.inputs["type"] == DS_TYPE_OPENSEARCH]
        assert len(es_ds) == 1
        assert "opensearchserviceConfig" in es_ds[0].inputs
        assert "elasticsearchConfig" not in es_ds[0].inputs

    when_appsync_ready(api, check_resources)


# --- DynamoDB index policy ---


@pulumi.runtime.test
def test_dynamo_data_source_iam_policy_includes_index_resources(pulumi_mocks, project_cwd):
    """DynamoDB IAM policy must include both table ARN and table ARN/index/*."""
    api = make_api()
    items, _ = make_dynamo_ds(api)
    api.query("getItem", items, code="resolvers/getItem.js")

    def check_resources(_):
        policies = pulumi_mocks.created_role_policies()
        dynamo_policies = [p for p in policies if "dynamo-policy" in p.name]
        assert len(dynamo_policies) == 1
        policy_doc = json.loads(dynamo_policies[0].inputs["policy"])
        resources = policy_doc["Statement"][0]["Resource"]
        assert isinstance(resources, list)
        assert len(resources) == 2
        assert resources[1].endswith("/index/*")

    when_appsync_ready(api, check_resources)
