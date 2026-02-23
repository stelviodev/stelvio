"""AppSync resolver tests — query, mutation, subscription, nested types, pipeline."""

import pulumi
import pytest

from stelvio.aws.appsync import AppSync, CognitoAuth
from stelvio.aws.appsync.constants import NONE_PASSTHROUGH_CODE
from stelvio.aws.dynamo_db import DynamoTable

from .conftest import COGNITO_USER_POOL_ID, INLINE_SCHEMA

TP = "test-test-"


def _make_api(name="myapi"):
    return AppSync(name, INLINE_SCHEMA, auth=CognitoAuth(user_pool_id=COGNITO_USER_POOL_ID))


# --- Unit resolvers (Lambda) ---


@pulumi.runtime.test
def test_query_resolver_lambda_no_code(pulumi_mocks, project_cwd):
    """Lambda data source resolvers are Direct Lambda Resolvers — no code needed."""
    api = _make_api()
    posts = api.data_source_lambda("posts", handler="functions/simple.handler")
    api.query("getPost", posts)
    _ = api.resources

    def check_resources(_):
        resolvers = pulumi_mocks.created_appsync_resolvers()
        assert len(resolvers) == 1
        r = resolvers[0]
        assert r.typ == "aws:appsync/resolver:Resolver"
        assert r.inputs["type"] == "Query"
        assert r.inputs["field"] == "getPost"
        assert r.inputs["kind"] == "UNIT"
        # Direct Lambda Resolver should not have runtime set
        assert "runtime" not in r.inputs

    api.resources.completed.apply(check_resources)


@pulumi.runtime.test
def test_mutation_resolver_lambda(pulumi_mocks, project_cwd):
    api = _make_api()
    posts = api.data_source_lambda("posts", handler="functions/simple.handler")
    api.mutation("createPost", posts)
    _ = api.resources

    def check_resources(_):
        resolvers = pulumi_mocks.created_appsync_resolvers()
        assert len(resolvers) == 1
        assert resolvers[0].inputs["type"] == "Mutation"
        assert resolvers[0].inputs["field"] == "createPost"

    api.resources.completed.apply(check_resources)


@pulumi.runtime.test
def test_subscription_resolver(pulumi_mocks, project_cwd):
    api = _make_api()
    posts = api.data_source_lambda("posts", handler="functions/simple.handler")
    api.subscription("onCreatePost", posts)
    _ = api.resources

    def check_resources(_):
        resolvers = pulumi_mocks.created_appsync_resolvers()
        assert len(resolvers) == 1
        assert resolvers[0].inputs["type"] == "Subscription"
        assert resolvers[0].inputs["field"] == "onCreatePost"

    api.resources.completed.apply(check_resources)


@pulumi.runtime.test
def test_resolver_for_nested_type(pulumi_mocks, project_cwd):
    api = _make_api()
    posts = api.data_source_lambda("posts", handler="functions/simple.handler")
    api.resolver("Post", "author", posts)
    _ = api.resources

    def check_resources(_):
        resolvers = pulumi_mocks.created_appsync_resolvers()
        assert len(resolvers) == 1
        assert resolvers[0].inputs["type"] == "Post"
        assert resolvers[0].inputs["field"] == "author"

    api.resources.completed.apply(check_resources)


@pulumi.runtime.test
def test_multiple_resolvers_same_data_source(pulumi_mocks, project_cwd):
    api = _make_api()
    posts = api.data_source_lambda("posts", handler="functions/simple.handler")
    api.query("getPost", posts)
    api.query("listPosts", posts)
    api.mutation("createPost", posts)
    _ = api.resources

    def check_resources(_):
        resolvers = pulumi_mocks.created_appsync_resolvers()
        assert len(resolvers) == 3
        fields = {r.inputs["field"] for r in resolvers}
        assert fields == {"getPost", "listPosts", "createPost"}

    api.resources.completed.apply(check_resources)


# --- DynamoDB resolvers (require code) ---


@pulumi.runtime.test
def test_dynamo_resolver_with_code_file(pulumi_mocks, project_cwd):
    api = _make_api()
    table = DynamoTable("items", fields={"pk": "S"}, partition_key="pk")
    items = api.data_source_dynamo("items", table=table)
    api.query("getItem", items, code="resolvers/getItem.js")
    _ = api.resources

    def check_resources(_):
        resolvers = pulumi_mocks.created_appsync_resolvers()
        assert len(resolvers) == 1
        r = resolvers[0]
        assert r.inputs["kind"] == "UNIT"
        # Code should be read from file
        assert "GetItem" in r.inputs["code"]
        # Should have runtime set (APPSYNC_JS)
        assert r.inputs["runtime"]["name"] == "APPSYNC_JS"

    api.resources.completed.apply(check_resources)


@pulumi.runtime.test
def test_dynamo_resolver_with_inline_code(pulumi_mocks, project_cwd):
    api = _make_api()
    table = DynamoTable("items", fields={"pk": "S"}, partition_key="pk")
    items = api.data_source_dynamo("items", table=table)
    inline_js = """
export function request(ctx) { return { operation: 'GetItem', key: { id: ctx.args.id } }; }
export function response(ctx) { return ctx.result; }
"""
    api.query("getItem", items, code=inline_js)
    _ = api.resources

    def check_resources(_):
        resolvers = pulumi_mocks.created_appsync_resolvers()
        assert len(resolvers) == 1
        assert "GetItem" in resolvers[0].inputs["code"]

    api.resources.completed.apply(check_resources)


# --- NONE data source resolvers ---


@pulumi.runtime.test
def test_none_resolver_auto_passthrough(pulumi_mocks, project_cwd):
    """Resolver with None data source and no code gets auto-generated passthrough."""
    api = _make_api()
    api.mutation("sendMessage", None)
    _ = api.resources

    def check_resources(_):
        resolvers = pulumi_mocks.created_appsync_resolvers()
        assert len(resolvers) == 1
        r = resolvers[0]
        assert r.inputs["kind"] == "UNIT"
        assert r.inputs["code"] == NONE_PASSTHROUGH_CODE

    api.resources.completed.apply(check_resources)


@pulumi.runtime.test
def test_none_resolver_with_custom_code(pulumi_mocks, project_cwd):
    """Resolver with None data source and custom code uses provided code."""
    api = _make_api()
    custom_js = """
export function request(ctx) {
  return { payload: { ...ctx.args, sentAt: util.time.nowISO8601() } };
}
export function response(ctx) { return ctx.result; }
"""
    api.mutation("sendMessage", None, code=custom_js)
    _ = api.resources

    def check_resources(_):
        resolvers = pulumi_mocks.created_appsync_resolvers()
        assert len(resolvers) == 1
        assert "sentAt" in resolvers[0].inputs["code"]

    api.resources.completed.apply(check_resources)


# --- Pipeline resolvers ---


@pulumi.runtime.test
def test_pipeline_resolver(pulumi_mocks, project_cwd):
    api = _make_api()
    table = DynamoTable("items", fields={"pk": "S"}, partition_key="pk")
    items = api.data_source_dynamo("items", table=table)

    auth_step = api.pipe_function("checkAuth", None, code="resolvers/auth.js")
    delete_step = api.pipe_function("doDelete", items, code="resolvers/delete.js")
    api.mutation("deletePost", [auth_step, delete_step])
    _ = api.resources

    def check_resources(_):
        # Pipeline resolver created
        resolvers = pulumi_mocks.created_appsync_resolvers()
        assert len(resolvers) == 1
        r = resolvers[0]
        assert r.inputs["kind"] == "PIPELINE"
        assert "pipelineConfig" in r.inputs

        # AppSync Functions created
        appsync_fns = pulumi_mocks.created_appsync_functions()
        assert len(appsync_fns) == 2
        fn_names = {f.inputs["name"] for f in appsync_fns}
        assert fn_names == {"checkAuth", "doDelete"}

    api.resources.completed.apply(check_resources)


@pulumi.runtime.test
def test_pipeline_resolver_with_none_data_source_step(pulumi_mocks, project_cwd):
    """Pipe function with None data source uses the internal NONE data source."""
    api = _make_api()
    auth_step = api.pipe_function("checkAuth", None, code="resolvers/auth.js")
    api.mutation("deletePost", [auth_step])
    _ = api.resources

    def check_resources(_):
        appsync_fns = pulumi_mocks.created_appsync_functions()
        assert len(appsync_fns) == 1
        # The data source should reference NONE
        assert appsync_fns[0].inputs["dataSource"] == "NONE"

    api.resources.completed.apply(check_resources)


@pulumi.runtime.test
def test_pipeline_resolver_default_passthrough_code(pulumi_mocks, project_cwd):
    """Pipeline resolver without explicit code gets passthrough before/after mapping."""
    api = _make_api()
    auth_step = api.pipe_function("checkAuth", None, code="resolvers/auth.js")
    api.mutation("deletePost", [auth_step])
    _ = api.resources

    def check_resources(_):
        resolvers = pulumi_mocks.created_appsync_resolvers()
        assert len(resolvers) == 1
        # Pipeline resolver gets passthrough code (before/after stubs)
        assert resolvers[0].inputs["code"] == NONE_PASSTHROUGH_CODE

    api.resources.completed.apply(check_resources)


@pulumi.runtime.test
def test_pipe_function_resources_accessible(pulumi_mocks, project_cwd):
    api = _make_api()
    auth_step = api.pipe_function("checkAuth", None, code="resolvers/auth.js")
    api.mutation("deletePost", [auth_step])

    def check_resources(_):
        assert auth_step.resources is not None
        assert auth_step.resources.function is not None

    api.resources.completed.apply(check_resources)


@pulumi.runtime.test
def test_resolver_resources_accessible(pulumi_mocks, project_cwd):
    api = _make_api()
    posts = api.data_source_lambda("posts", handler="functions/simple.handler")
    resolver = api.query("getPost", posts)

    def check_resources(_):
        assert resolver.resources is not None
        assert resolver.resources.resolver is not None

    api.resources.completed.apply(check_resources)


# --- Validation ---


def test_dynamo_resolver_requires_code(project_cwd):
    api = _make_api()
    table = DynamoTable("items", fields={"pk": "S"}, partition_key="pk")
    items = api.data_source_dynamo("items", table=table)
    with pytest.raises(ValueError, match="code is required"):
        api.query("getItem", items)


def test_http_resolver_requires_code(project_cwd):
    api = _make_api()
    ext = api.data_source_http("ext", url="https://api.example.com")
    with pytest.raises(ValueError, match="code is required"):
        api.query("getExt", ext)


def test_rds_resolver_requires_code(project_cwd):
    api = _make_api()
    db = api.data_source_rds(
        "db",
        cluster_arn="arn:aws:rds:us-east-1:123456789012:cluster:my-cluster",
        secret_arn="arn:aws:secretsmanager:us-east-1:123456789012:secret:my-secret",
        database="mydb",
    )
    with pytest.raises(ValueError, match="code is required"):
        api.query("getData", db)


def test_opensearch_resolver_requires_code(project_cwd):
    api = _make_api()
    search = api.data_source_opensearch(
        "search", endpoint="https://search-domain.us-east-1.es.amazonaws.com"
    )
    with pytest.raises(ValueError, match="code is required"):
        api.query("search", search)


def test_duplicate_type_field_error(project_cwd):
    api = _make_api()
    posts = api.data_source_lambda("posts", handler="functions/simple.handler")
    api.query("getPost", posts)
    with pytest.raises(ValueError, match=r"Duplicate resolver for Query\.getPost"):
        api.query("getPost", posts)


def test_empty_field_error(project_cwd):
    api = _make_api()
    posts = api.data_source_lambda("posts", handler="functions/simple.handler")
    with pytest.raises(ValueError, match="field cannot be empty"):
        api.query("", posts)


def test_duplicate_pipe_function_name(project_cwd):
    api = _make_api()
    api.pipe_function("checkAuth", None, code="resolvers/auth.js")
    with pytest.raises(ValueError, match="Duplicate pipe function name 'checkAuth'"):
        api.pipe_function("checkAuth", None, code="resolvers/auth.js")


def test_pipe_function_requires_code(project_cwd):
    api = _make_api()
    with pytest.raises(ValueError, match="code is required for pipe_function"):
        api.pipe_function("checkAuth", None, code="")


@pulumi.runtime.test
def test_resolver_customize_applied(pulumi_mocks, project_cwd):
    api = _make_api()
    posts = api.data_source_lambda("posts", handler="functions/simple.handler")
    api.query("getPost", posts, customize={"resolver": {"field": "getPostCustom"}})
    _ = api.resources

    def check_resources(_):
        resolvers = pulumi_mocks.created_appsync_resolvers()
        assert len(resolvers) == 1
        assert resolvers[0].inputs["field"] == "getPostCustom"

    api.resources.completed.apply(check_resources)


@pulumi.runtime.test
def test_pipe_function_customize_applied(pulumi_mocks, project_cwd):
    api = _make_api()
    auth_step = api.pipe_function(
        "checkAuth",
        None,
        code="resolvers/auth.js",
        customize={"function": {"name": "custom-check-auth"}},
    )
    api.mutation("deletePost", [auth_step])
    _ = api.resources

    def check_resources(_):
        appsync_fns = pulumi_mocks.created_appsync_functions()
        assert len(appsync_fns) == 1
        assert appsync_fns[0].inputs["name"] == "custom-check-auth"

    api.resources.completed.apply(check_resources)
