"""Tests for AppSync data sources."""

import pulumi
import pytest

from stelvio.aws.appsync import AppSync
from stelvio.aws.function import Function, FunctionConfig

# Test prefix
TP = "test-test-"

SAMPLE_SCHEMA = """
type Query {
    getUser(id: ID!): User
}
type User {
    id: ID!
    name: String!
}
"""


# =============================================================================
# Lambda Data Source Tests
# =============================================================================


@pulumi.runtime.test
def test_lambda_data_source_with_handler_string(pulumi_mocks, project_cwd):
    """Test Lambda data source with handler string."""
    api = AppSync("my-api", SAMPLE_SCHEMA)
    ds = api.add_data_source("users", handler="functions/users.handler")

    assert ds.name == "users"

    def check_resources(_):
        data_sources = pulumi_mocks.created_appsync_data_sources()
        assert len(data_sources) == 1
        ds_resource = data_sources[0]
        assert ds_resource.inputs["type"] == "AWS_LAMBDA"
        assert ds_resource.inputs["name"] == "users"

        # Verify Lambda function was created
        functions = pulumi_mocks.created_functions()
        ds_functions = [f for f in functions if "users" in f.name]
        assert len(ds_functions) >= 1

    # Wait for actual data source resource
    api.resources.data_sources["users"].id.apply(check_resources)


@pulumi.runtime.test
def test_lambda_data_source_with_function_config(pulumi_mocks, project_cwd):
    """Test Lambda data source with FunctionConfig."""
    api = AppSync("my-api", SAMPLE_SCHEMA)
    config = FunctionConfig(handler="functions/users.handler", memory=512, timeout=30)
    api.add_data_source("users", handler=config)

    def check_resources(_):
        functions = pulumi_mocks.created_functions()
        ds_functions = [f for f in functions if "users" in f.name]
        assert len(ds_functions) >= 1
        # Verify memory was set
        assert ds_functions[0].inputs["memorySize"] == 512

    api.resources.data_sources["users"].id.apply(check_resources)


@pulumi.runtime.test
def test_lambda_data_source_with_function_instance(pulumi_mocks, project_cwd):
    """Test Lambda data source with existing Function instance."""
    fn = Function("shared-handler", handler="functions/shared.handler")
    api = AppSync("my-api", SAMPLE_SCHEMA)
    api.add_data_source("shared", handler=fn)

    def check_resources(_):
        data_sources = pulumi_mocks.created_appsync_data_sources()
        assert len(data_sources) == 1
        assert data_sources[0].inputs["type"] == "AWS_LAMBDA"

    api.resources.data_sources["shared"].id.apply(check_resources)


@pulumi.runtime.test
def test_lambda_data_source_with_opts(pulumi_mocks, project_cwd):
    """Test Lambda data source with function options."""
    api = AppSync("my-api", SAMPLE_SCHEMA)
    api.add_data_source("users", handler="functions/users.handler", memory=1024, timeout=60)

    def check_resources(_):
        functions = pulumi_mocks.created_functions()
        ds_functions = [f for f in functions if "users" in f.name]
        assert len(ds_functions) >= 1
        assert ds_functions[0].inputs["memorySize"] == 1024
        assert ds_functions[0].inputs["timeout"] == 60

    api.resources.data_sources["users"].id.apply(check_resources)


@pulumi.runtime.test
def test_lambda_data_source_creates_permission(pulumi_mocks, project_cwd):
    """Test Lambda data source creates invoke permission for AppSync."""
    api = AppSync("my-api", SAMPLE_SCHEMA)
    api.add_data_source("users", handler="functions/users.handler")

    def check_resources(_):
        permissions = pulumi_mocks.created_permissions()
        ds_permissions = [p for p in permissions if "users" in p.name and "perm" in p.name]
        assert len(ds_permissions) >= 1
        perm = ds_permissions[0]
        assert perm.inputs["action"] == "lambda:InvokeFunction"
        assert perm.inputs["principal"] == "appsync.amazonaws.com"

    api.resources.data_sources["users"].id.apply(check_resources)


@pulumi.runtime.test
def test_lambda_data_source_creates_iam_role(pulumi_mocks, project_cwd):
    """Test Lambda data source creates IAM role."""
    api = AppSync("my-api", SAMPLE_SCHEMA)
    api.add_data_source("users", handler="functions/users.handler")

    def check_resources(_):
        roles = pulumi_mocks.created_roles()
        ds_roles = [r for r in roles if "users" in r.name and "role" in r.name]
        assert len(ds_roles) >= 1

    api.resources.data_sources["users"].id.apply(check_resources)


# =============================================================================
# DynamoDB Data Source Tests
# =============================================================================


@pulumi.runtime.test
def test_dynamodb_data_source(pulumi_mocks, project_cwd):
    """Test DynamoDB data source creation."""
    api = AppSync("my-api", SAMPLE_SCHEMA)
    ds = api.add_data_source("users-table", dynamodb="users")

    assert ds.name == "users-table"

    def check_resources(_):
        data_sources = pulumi_mocks.created_appsync_data_sources()
        assert len(data_sources) == 1
        ds_resource = data_sources[0]
        assert ds_resource.inputs["type"] == "AMAZON_DYNAMODB"
        assert ds_resource.inputs["name"] == "users-table"
        assert ds_resource.inputs["dynamodbConfig"]["tableName"] == "users"

    api.resources.data_sources["users-table"].id.apply(check_resources)


@pulumi.runtime.test
def test_dynamodb_data_source_with_region(pulumi_mocks, project_cwd):
    """Test DynamoDB data source with custom region."""
    api = AppSync("my-api", SAMPLE_SCHEMA)
    api.add_data_source("users-table", dynamodb="users", dynamodb_region="eu-west-1")

    def check_resources(_):
        data_sources = pulumi_mocks.created_appsync_data_sources()
        assert len(data_sources) == 1
        assert data_sources[0].inputs["dynamodbConfig"]["region"] == "eu-west-1"

    api.resources.data_sources["users-table"].id.apply(check_resources)


@pulumi.runtime.test
def test_dynamodb_data_source_creates_iam_role(pulumi_mocks, project_cwd):
    """Test DynamoDB data source creates IAM role with DynamoDB permissions."""
    api = AppSync("my-api", SAMPLE_SCHEMA)
    api.add_data_source("users-table", dynamodb="users")

    def check_resources(_):
        roles = pulumi_mocks.created_roles()
        ds_roles = [r for r in roles if "users-table" in r.name]
        assert len(ds_roles) >= 1

        # Check for inline policy
        policies = pulumi_mocks.created_role_policies()
        ds_policies = [p for p in policies if "users-table" in p.name]
        assert len(ds_policies) >= 1

    api.resources.data_sources["users-table"].id.apply(check_resources)


# =============================================================================
# HTTP Data Source Tests
# =============================================================================


@pulumi.runtime.test
def test_http_data_source(pulumi_mocks, project_cwd):
    """Test HTTP data source creation."""
    api = AppSync("my-api", SAMPLE_SCHEMA)
    ds = api.add_data_source("rest-api", http="https://api.example.com")

    assert ds.name == "rest-api"

    def check_resources(_):
        data_sources = pulumi_mocks.created_appsync_data_sources()
        assert len(data_sources) == 1
        ds_resource = data_sources[0]
        assert ds_resource.inputs["type"] == "HTTP"
        assert ds_resource.inputs["httpConfig"]["endpoint"] == "https://api.example.com"

    api.resources.data_sources["rest-api"].id.apply(check_resources)


# =============================================================================
# EventBridge Data Source Tests
# =============================================================================


@pulumi.runtime.test
def test_eventbridge_data_source(pulumi_mocks, project_cwd):
    """Test EventBridge data source creation."""
    api = AppSync("my-api", SAMPLE_SCHEMA)
    bus_arn = "arn:aws:events:us-east-1:123456789012:event-bus/default"
    ds = api.add_data_source("events", eventbridge=bus_arn)

    assert ds.name == "events"

    def check_resources(_):
        data_sources = pulumi_mocks.created_appsync_data_sources()
        assert len(data_sources) == 1
        assert data_sources[0].inputs["type"] == "AMAZON_EVENTBRIDGE"

    api.resources.data_sources["events"].id.apply(check_resources)


# =============================================================================
# OpenSearch Data Source Tests
# =============================================================================


@pulumi.runtime.test
def test_opensearch_data_source(pulumi_mocks, project_cwd):
    """Test OpenSearch data source creation."""
    api = AppSync("my-api", SAMPLE_SCHEMA)
    ds = api.add_data_source(
        "search",
        opensearch="https://search-domain.us-east-1.es.amazonaws.com",
    )

    assert ds.name == "search"

    def check_resources(_):
        data_sources = pulumi_mocks.created_appsync_data_sources()
        assert len(data_sources) == 1
        assert data_sources[0].inputs["type"] == "AMAZON_OPENSEARCH_SERVICE"

    api.resources.data_sources["search"].id.apply(check_resources)


# =============================================================================
# RDS Data Source Tests
# =============================================================================


@pulumi.runtime.test
def test_rds_data_source(pulumi_mocks, project_cwd):
    """Test RDS data source creation."""
    api = AppSync("my-api", SAMPLE_SCHEMA)
    ds = api.add_data_source(
        "database",
        rds={
            "cluster_arn": "arn:aws:rds:us-east-1:123456789012:cluster:my-cluster",
            "secret_arn": "arn:aws:secretsmanager:us-east-1:123456789012:secret:my-secret",
            "database_name": "mydb",
        },
    )

    assert ds.name == "database"

    def check_resources(_):
        data_sources = pulumi_mocks.created_appsync_data_sources()
        assert len(data_sources) == 1
        assert data_sources[0].inputs["type"] == "RELATIONAL_DATABASE"

    api.resources.data_sources["database"].id.apply(check_resources)


# =============================================================================
# None Data Source Tests
# =============================================================================


@pulumi.runtime.test
def test_none_data_source(pulumi_mocks, project_cwd):
    """Test NONE data source creation (for local resolvers)."""
    api = AppSync("my-api", SAMPLE_SCHEMA)
    ds = api.add_data_source("local", none=True)

    assert ds.name == "local"

    def check_resources(_):
        data_sources = pulumi_mocks.created_appsync_data_sources()
        assert len(data_sources) == 1
        assert data_sources[0].inputs["type"] == "NONE"

    api.resources.data_sources["local"].id.apply(check_resources)


# =============================================================================
# Multiple Data Sources Tests
# =============================================================================


@pulumi.runtime.test
def test_multiple_data_sources(pulumi_mocks, project_cwd):
    """Test creating multiple data sources."""
    api = AppSync("my-api", SAMPLE_SCHEMA)
    api.add_data_source("users", handler="functions/users.handler")
    api.add_data_source("users-table", dynamodb="users")
    api.add_data_source("local", none=True)

    def check_resources(_):
        data_sources = pulumi_mocks.created_appsync_data_sources()
        assert len(data_sources) == 3

        types = {ds.inputs["name"]: ds.inputs["type"] for ds in data_sources}
        assert types["users"] == "AWS_LAMBDA"
        assert types["users-table"] == "AMAZON_DYNAMODB"
        assert types["local"] == "NONE"

    # Wait for all data sources to be created
    pulumi.Output.all(
        api.resources.data_sources["users"].id,
        api.resources.data_sources["users-table"].id,
        api.resources.data_sources["local"].id,
    ).apply(check_resources)


# =============================================================================
# Validation Tests
# =============================================================================


def test_no_data_source_type_raises_error():
    """Test that not specifying a data source type raises ValueError."""
    api = AppSync("my-api", SAMPLE_SCHEMA)
    with pytest.raises(ValueError, match="Must specify exactly one data source type"):
        api.add_data_source("empty")


def test_multiple_data_source_types_raises_error():
    """Test that specifying multiple data source types raises ValueError."""
    api = AppSync("my-api", SAMPLE_SCHEMA)
    with pytest.raises(ValueError, match="Cannot specify multiple data source types"):
        api.add_data_source("multi", handler="fn.handler", dynamodb="table")


def test_duplicate_data_source_name_raises_error():
    """Test that duplicate data source names raise ValueError."""
    api = AppSync("my-api", SAMPLE_SCHEMA)
    api.add_data_source("users", handler="functions/users.handler")
    with pytest.raises(ValueError, match="Data source 'users' already exists"):
        api.add_data_source("users", dynamodb="users")


def test_function_with_opts_raises_error():
    """Test that passing Function instance with opts raises ValueError."""
    fn = Function("handler", handler="functions/handler.main")
    api = AppSync("my-api", SAMPLE_SCHEMA)
    with pytest.raises(ValueError, match="cannot combine Function instance"):
        api.add_data_source("users", handler=fn, memory=512)
