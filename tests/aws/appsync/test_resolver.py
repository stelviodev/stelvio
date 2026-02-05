"""Tests for AppSync resolvers."""

import pulumi
import pytest

from stelvio.aws.appsync import AppSync

# Test prefix
TP = "test-test-"

SAMPLE_SCHEMA = """
type Query {
    getUser(id: ID!): User
    listUsers: [User]
}
type Mutation {
    createUser(name: String!): User
}
type User {
    id: ID!
    name: String!
}
"""

JS_RESOLVER_CODE = """
export function request(ctx) {
    return { operation: "GetItem", key: { id: ctx.args.id } };
}
export function response(ctx) {
    return ctx.result;
}
"""

JS_PIPELINE_CODE = """
export function request(ctx) {
    return {};
}
export function response(ctx) {
    return ctx.prev.result;
}
"""


# =============================================================================
# Unit Resolver Tests
# =============================================================================


@pulumi.runtime.test
def test_creates_unit_resolver_with_js_code(pulumi_mocks, project_cwd):
    """Test creating unit resolver with JavaScript code."""
    api = AppSync("my-api", SAMPLE_SCHEMA)
    api.add_data_source("users", handler="functions/users.handler")
    resolver = api.add_resolver(
        "Query getUser",
        data_source="users",
        code=JS_RESOLVER_CODE,
    )

    assert resolver.type_name == "Query"
    assert resolver.field_name == "getUser"

    def check_resources(_):
        resolvers = pulumi_mocks.created_appsync_resolvers()
        assert len(resolvers) == 1
        r = resolvers[0]
        assert r.inputs["type"] == "Query"
        assert r.inputs["field"] == "getUser"
        assert r.inputs["kind"] == "UNIT"
        assert r.inputs["code"] == JS_RESOLVER_CODE

    api.resources.resolvers[0].id.apply(check_resources)


@pulumi.runtime.test
def test_creates_unit_resolver_with_vtl(pulumi_mocks, project_cwd):
    """Test creating unit resolver with VTL templates."""
    api = AppSync("my-api", SAMPLE_SCHEMA)
    api.add_data_source("users", handler="functions/users.handler")
    api.add_resolver(
        "Query getUser",
        data_source="users",
        request_template='{"version": "2018-05-29", "operation": "GetItem"}',
        response_template="$util.toJson($ctx.result)",
    )

    def check_resources(_):
        resolvers = pulumi_mocks.created_appsync_resolvers()
        assert len(resolvers) == 1
        r = resolvers[0]
        # Pulumi mocks convert to camelCase: requestTemplate / responseTemplate
        assert "requestTemplate" in r.inputs
        assert "responseTemplate" in r.inputs

    api.resources.resolvers[0].id.apply(check_resources)


@pulumi.runtime.test
def test_unit_resolver_with_data_source_object(pulumi_mocks, project_cwd):
    """Test creating unit resolver with AppSyncDataSource object."""
    api = AppSync("my-api", SAMPLE_SCHEMA)
    ds = api.add_data_source("users", handler="functions/users.handler")
    api.add_resolver(
        "Query getUser",
        data_source=ds,
        code=JS_RESOLVER_CODE,
    )

    def check_resources(_):
        resolvers = pulumi_mocks.created_appsync_resolvers()
        assert len(resolvers) == 1

    api.resources.resolvers[0].id.apply(check_resources)


# =============================================================================
# Pipeline Resolver Tests
# =============================================================================


@pulumi.runtime.test
def test_creates_pipeline_resolver(pulumi_mocks, project_cwd):
    """Test creating pipeline resolver with functions."""
    api = AppSync("my-api", SAMPLE_SCHEMA)
    api.add_data_source("users", handler="functions/users.handler")
    api.add_function("validate", "users", code=JS_RESOLVER_CODE)
    api.add_function("get-user", "users", code=JS_RESOLVER_CODE)
    api.add_resolver(
        "Query getUser",
        kind="pipeline",
        functions=["validate", "get-user"],
        code=JS_PIPELINE_CODE,
    )

    def check_resources(_):
        resolvers = pulumi_mocks.created_appsync_resolvers()
        assert len(resolvers) == 1
        r = resolvers[0]
        assert r.inputs["kind"] == "PIPELINE"
        assert "pipelineConfig" in r.inputs

    api.resources.resolvers[0].id.apply(check_resources)


@pulumi.runtime.test
def test_pipeline_resolver_with_function_objects(pulumi_mocks, project_cwd):
    """Test creating pipeline resolver with AppSyncFunction objects."""
    api = AppSync("my-api", SAMPLE_SCHEMA)
    api.add_data_source("users", handler="functions/users.handler")
    validate_fn = api.add_function("validate", "users", code=JS_RESOLVER_CODE)
    get_user_fn = api.add_function("get-user", "users", code=JS_RESOLVER_CODE)
    api.add_resolver(
        "Query getUser",
        kind="pipeline",
        functions=[validate_fn, get_user_fn],
        code=JS_PIPELINE_CODE,
    )

    def check_resources(_):
        resolvers = pulumi_mocks.created_appsync_resolvers()
        assert len(resolvers) == 1

    api.resources.resolvers[0].id.apply(check_resources)


# =============================================================================
# Operation Parsing Tests
# =============================================================================


@pulumi.runtime.test
def test_parses_query_operation(pulumi_mocks, project_cwd):
    """Test parsing Query operation."""
    api = AppSync("my-api", SAMPLE_SCHEMA)
    api.add_data_source("users", handler="functions/users.handler")
    resolver = api.add_resolver(
        "Query listUsers",
        data_source="users",
        code=JS_RESOLVER_CODE,
    )

    assert resolver.type_name == "Query"
    assert resolver.field_name == "listUsers"

    def check_resources(_):
        resolvers = pulumi_mocks.created_appsync_resolvers()
        assert len(resolvers) == 1
        assert resolvers[0].inputs["type"] == "Query"
        assert resolvers[0].inputs["field"] == "listUsers"

    api.resources.resolvers[0].id.apply(check_resources)


@pulumi.runtime.test
def test_parses_mutation_operation(pulumi_mocks, project_cwd):
    """Test parsing Mutation operation."""
    api = AppSync("my-api", SAMPLE_SCHEMA)
    api.add_data_source("users", handler="functions/users.handler")
    resolver = api.add_resolver(
        "Mutation createUser",
        data_source="users",
        code=JS_RESOLVER_CODE,
    )

    assert resolver.type_name == "Mutation"
    assert resolver.field_name == "createUser"

    def check_resources(_):
        resolvers = pulumi_mocks.created_appsync_resolvers()
        assert len(resolvers) == 1
        assert resolvers[0].inputs["type"] == "Mutation"

    api.resources.resolvers[0].id.apply(check_resources)


@pulumi.runtime.test
def test_parses_custom_type_operation(pulumi_mocks, project_cwd):
    """Test parsing custom type field resolver."""
    api = AppSync("my-api", SAMPLE_SCHEMA)
    api.add_data_source("users", handler="functions/users.handler")
    resolver = api.add_resolver(
        "User name",
        data_source="users",
        code=JS_RESOLVER_CODE,
    )

    assert resolver.type_name == "User"
    assert resolver.field_name == "name"

    def check_resources(_):
        resolvers = pulumi_mocks.created_appsync_resolvers()
        assert len(resolvers) == 1
        assert resolvers[0].inputs["type"] == "User"
        assert resolvers[0].inputs["field"] == "name"

    api.resources.resolvers[0].id.apply(check_resources)


# =============================================================================
# Multiple Resolvers Tests
# =============================================================================


@pulumi.runtime.test
def test_multiple_resolvers(pulumi_mocks, project_cwd):
    """Test creating multiple resolvers."""
    api = AppSync("my-api", SAMPLE_SCHEMA)
    api.add_data_source("users", handler="functions/users.handler")
    api.add_resolver("Query getUser", data_source="users", code=JS_RESOLVER_CODE)
    api.add_resolver("Query listUsers", data_source="users", code=JS_RESOLVER_CODE)
    api.add_resolver("Mutation createUser", data_source="users", code=JS_RESOLVER_CODE)

    def check_resources(_):
        resolvers = pulumi_mocks.created_appsync_resolvers()
        assert len(resolvers) == 3

    pulumi.Output.all(
        api.resources.resolvers[0].id,
        api.resources.resolvers[1].id,
        api.resources.resolvers[2].id,
    ).apply(check_resources)


# =============================================================================
# Validation Tests
# =============================================================================


def test_invalid_operation_format_raises_error():
    """Test that invalid operation format raises ValueError."""
    api = AppSync("my-api", SAMPLE_SCHEMA)
    api.add_data_source("users", handler="functions/users.handler")
    with pytest.raises(ValueError, match="Invalid operation format"):
        api.add_resolver("QuerygetUser", data_source="users", code=JS_RESOLVER_CODE)


def test_single_word_operation_raises_error():
    """Test that single word operation raises ValueError."""
    api = AppSync("my-api", SAMPLE_SCHEMA)
    api.add_data_source("users", handler="functions/users.handler")
    with pytest.raises(ValueError, match="Invalid operation format"):
        api.add_resolver("Query", data_source="users", code=JS_RESOLVER_CODE)


def test_duplicate_resolver_raises_error():
    """Test that duplicate resolver raises ValueError."""
    api = AppSync("my-api", SAMPLE_SCHEMA)
    api.add_data_source("users", handler="functions/users.handler")
    api.add_resolver("Query getUser", data_source="users", code=JS_RESOLVER_CODE)
    with pytest.raises(ValueError, match="Resolver for 'Query getUser' already exists"):
        api.add_resolver("Query getUser", data_source="users", code=JS_RESOLVER_CODE)


def test_unit_resolver_without_data_source_raises_error():
    """Test that unit resolver without data_source raises ValueError."""
    api = AppSync("my-api", SAMPLE_SCHEMA)
    with pytest.raises(ValueError, match="Unit resolver requires 'data_source'"):
        api.add_resolver("Query getUser", code=JS_RESOLVER_CODE)


def test_unit_resolver_with_functions_raises_error():
    """Test that unit resolver with functions raises ValueError."""
    api = AppSync("my-api", SAMPLE_SCHEMA)
    api.add_data_source("users", handler="functions/users.handler")
    api.add_function("fn", "users", code=JS_RESOLVER_CODE)
    with pytest.raises(ValueError, match="Unit resolver cannot have 'functions'"):
        api.add_resolver(
            "Query getUser",
            data_source="users",
            functions=["fn"],
            code=JS_RESOLVER_CODE,
        )


def test_pipeline_resolver_without_functions_raises_error():
    """Test that pipeline resolver without functions raises ValueError."""
    api = AppSync("my-api", SAMPLE_SCHEMA)
    with pytest.raises(ValueError, match="Pipeline resolver requires 'functions'"):
        api.add_resolver("Query getUser", kind="pipeline", code=JS_PIPELINE_CODE)


def test_pipeline_resolver_with_data_source_raises_error():
    """Test that pipeline resolver with data_source raises ValueError."""
    api = AppSync("my-api", SAMPLE_SCHEMA)
    api.add_data_source("users", handler="functions/users.handler")
    api.add_function("fn", "users", code=JS_RESOLVER_CODE)
    with pytest.raises(ValueError, match="Pipeline resolver cannot have 'data_source'"):
        api.add_resolver(
            "Query getUser",
            kind="pipeline",
            data_source="users",
            functions=["fn"],
            code=JS_PIPELINE_CODE,
        )


def test_resolver_with_nonexistent_data_source_raises_error():
    """Test that resolver referencing nonexistent data source raises ValueError."""
    api = AppSync("my-api", SAMPLE_SCHEMA)
    with pytest.raises(ValueError, match="Data source 'users' not found"):
        api.add_resolver("Query getUser", data_source="users", code=JS_RESOLVER_CODE)


def test_resolver_with_nonexistent_function_raises_error():
    """Test that resolver referencing nonexistent function raises ValueError."""
    api = AppSync("my-api", SAMPLE_SCHEMA)
    with pytest.raises(ValueError, match="Function 'validate' not found"):
        api.add_resolver(
            "Query getUser",
            kind="pipeline",
            functions=["validate"],
            code=JS_PIPELINE_CODE,
        )


def test_resolver_with_code_and_vtl_raises_error():
    """Test that specifying both JS code and VTL templates raises ValueError."""
    api = AppSync("my-api", SAMPLE_SCHEMA)
    api.add_data_source("users", handler="functions/users.handler")
    with pytest.raises(ValueError, match="Cannot specify both 'code'"):
        api.add_resolver(
            "Query getUser",
            data_source="users",
            code=JS_RESOLVER_CODE,
            request_template='{"version": "2018-05-29"}',
        )
