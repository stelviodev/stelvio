"""Tests for AppSync pipeline functions."""

import pulumi
import pytest

from stelvio.aws.appsync import AppSync

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

JS_REQUEST_CODE = """
export function request(ctx) {
    return { operation: "GetItem", key: { id: ctx.args.id } };
}
export function response(ctx) {
    return ctx.result;
}
"""


# =============================================================================
# Function Creation Tests
# =============================================================================


@pulumi.runtime.test
def test_creates_function_with_js_code(pulumi_mocks, project_cwd):
    """Test creating function with JavaScript code."""
    api = AppSync("my-api", SAMPLE_SCHEMA)
    api.add_data_source("users", handler="functions/users.handler")
    fn = api.add_function("get-user", "users", code=JS_REQUEST_CODE)

    assert fn.name == "get-user"

    def check_resources(_):
        functions = pulumi_mocks.created_appsync_functions()
        assert len(functions) == 1
        fn_resource = functions[0]
        assert fn_resource.inputs["name"] == "get-user"
        assert fn_resource.inputs["code"] == JS_REQUEST_CODE
        assert fn_resource.inputs["runtime"]["name"] == "APPSYNC_JS"

    api.resources.functions["get-user"].id.apply(check_resources)


@pulumi.runtime.test
def test_creates_function_with_vtl_templates(pulumi_mocks, project_cwd):
    """Test creating function with VTL templates."""
    api = AppSync("my-api", SAMPLE_SCHEMA)
    api.add_data_source("users", handler="functions/users.handler")
    fn = api.add_function(
        "get-user",
        "users",
        request_template='{"version": "2018-05-29", "operation": "GetItem"}',
        response_template="$util.toJson($ctx.result)",
    )

    assert fn.name == "get-user"

    def check_resources(_):
        functions = pulumi_mocks.created_appsync_functions()
        assert len(functions) == 1
        fn_resource = functions[0]
        assert fn_resource.inputs["name"] == "get-user"
        assert "requestMappingTemplate" in fn_resource.inputs
        assert "responseMappingTemplate" in fn_resource.inputs

    api.resources.functions["get-user"].id.apply(check_resources)


@pulumi.runtime.test
def test_function_references_data_source_by_name(pulumi_mocks, project_cwd):
    """Test creating function with data source name string."""
    api = AppSync("my-api", SAMPLE_SCHEMA)
    api.add_data_source("users", handler="functions/users.handler")
    api.add_function("get-user", "users", code=JS_REQUEST_CODE)

    def check_resources(_):
        functions = pulumi_mocks.created_appsync_functions()
        assert len(functions) == 1

    api.resources.functions["get-user"].id.apply(check_resources)


@pulumi.runtime.test
def test_function_references_data_source_object(pulumi_mocks, project_cwd):
    """Test creating function with AppSyncDataSource object."""
    api = AppSync("my-api", SAMPLE_SCHEMA)
    ds = api.add_data_source("users", handler="functions/users.handler")
    api.add_function("get-user", ds, code=JS_REQUEST_CODE)

    def check_resources(_):
        functions = pulumi_mocks.created_appsync_functions()
        assert len(functions) == 1

    api.resources.functions["get-user"].id.apply(check_resources)


@pulumi.runtime.test
def test_multiple_functions(pulumi_mocks, project_cwd):
    """Test creating multiple functions."""
    api = AppSync("my-api", SAMPLE_SCHEMA)
    api.add_data_source("users", handler="functions/users.handler")
    api.add_function("get-user", "users", code=JS_REQUEST_CODE)
    api.add_function("list-users", "users", code=JS_REQUEST_CODE)
    api.add_function("validate-input", "users", code=JS_REQUEST_CODE)

    def check_resources(_):
        functions = pulumi_mocks.created_appsync_functions()
        assert len(functions) == 3

        names = {fn.inputs["name"] for fn in functions}
        assert names == {"get-user", "list-users", "validate-input"}

    pulumi.Output.all(
        api.resources.functions["get-user"].id,
        api.resources.functions["list-users"].id,
        api.resources.functions["validate-input"].id,
    ).apply(check_resources)


# =============================================================================
# Validation Tests
# =============================================================================


def test_function_with_nonexistent_data_source_raises_error():
    """Test that function referencing nonexistent data source raises ValueError."""
    api = AppSync("my-api", SAMPLE_SCHEMA)
    with pytest.raises(ValueError, match="Data source 'users' not found"):
        api.add_function("get-user", "users", code=JS_REQUEST_CODE)


def test_duplicate_function_name_raises_error():
    """Test that duplicate function names raise ValueError."""
    api = AppSync("my-api", SAMPLE_SCHEMA)
    api.add_data_source("users", handler="functions/users.handler")
    api.add_function("get-user", "users", code=JS_REQUEST_CODE)
    with pytest.raises(ValueError, match="Function 'get-user' already exists"):
        api.add_function("get-user", "users", code=JS_REQUEST_CODE)


def test_function_with_code_and_vtl_raises_error():
    """Test that specifying both JS code and VTL templates raises ValueError."""
    api = AppSync("my-api", SAMPLE_SCHEMA)
    api.add_data_source("users", handler="functions/users.handler")
    with pytest.raises(ValueError, match="Cannot specify both 'code'"):
        api.add_function(
            "get-user",
            "users",
            code=JS_REQUEST_CODE,
            request_template='{"version": "2018-05-29"}',
        )
