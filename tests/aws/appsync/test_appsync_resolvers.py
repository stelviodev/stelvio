"""AppSync resolver tests — query, mutation, subscription, nested types, pipeline."""

from pathlib import Path

import pulumi
import pytest

from stelvio.aws.appsync.constants import APPSYNC_JS_RUNTIME, NONE_PASSTHROUGH_CODE
from stelvio.aws.dynamo_db import DynamoTable

from .conftest import make_api, when_appsync_ready

# --- Unit resolvers (Lambda) ---


@pulumi.runtime.test
def test_query_resolver_lambda_no_code(pulumi_mocks, project_cwd):
    """Lambda data source resolvers are Direct Lambda Resolvers — no code needed."""
    api = make_api()
    posts = api.data_source_lambda("posts", handler="functions/simple.handler")
    api.query("getPost", posts)

    def check_resources(_):
        resolvers = pulumi_mocks.created_appsync_resolvers()
        assert len(resolvers) == 1
        r = resolvers[0]
        assert r.typ == "aws:appsync/resolver:Resolver"
        assert r.inputs["type"] == "Query"
        assert r.inputs["field"] == "getPost"
        assert r.inputs["kind"] == "UNIT"
        assert r.inputs["dataSource"] == "posts"
        # Direct Lambda Resolver should not have runtime set
        assert "runtime" not in r.inputs

    when_appsync_ready(api, check_resources)


@pulumi.runtime.test
def test_mutation_resolver_lambda(pulumi_mocks, project_cwd):
    api = make_api()
    posts = api.data_source_lambda("posts", handler="functions/simple.handler")
    api.mutation("createPost", posts)

    def check_resources(_):
        resolvers = pulumi_mocks.created_appsync_resolvers()
        assert len(resolvers) == 1
        r = resolvers[0]
        assert r.typ == "aws:appsync/resolver:Resolver"
        assert r.inputs["type"] == "Mutation"
        assert r.inputs["field"] == "createPost"
        assert r.inputs["dataSource"] == "posts"

    when_appsync_ready(api, check_resources)


@pulumi.runtime.test
def test_subscription_resolver(pulumi_mocks, project_cwd):
    api = make_api()
    posts = api.data_source_lambda("posts", handler="functions/simple.handler")
    api.subscription("onCreatePost", posts)

    def check_resources(_):
        resolvers = pulumi_mocks.created_appsync_resolvers()
        assert len(resolvers) == 1
        r = resolvers[0]
        assert r.typ == "aws:appsync/resolver:Resolver"
        assert r.inputs["type"] == "Subscription"
        assert r.inputs["field"] == "onCreatePost"
        assert r.inputs["dataSource"] == "posts"

    when_appsync_ready(api, check_resources)


@pulumi.runtime.test
def test_resolver_for_nested_type(pulumi_mocks, project_cwd):
    api = make_api()
    posts = api.data_source_lambda("posts", handler="functions/simple.handler")
    api.resolver("Post", "author", posts)

    def check_resources(_):
        resolvers = pulumi_mocks.created_appsync_resolvers()
        assert len(resolvers) == 1
        r = resolvers[0]
        assert r.typ == "aws:appsync/resolver:Resolver"
        assert r.inputs["type"] == "Post"
        assert r.inputs["field"] == "author"
        assert r.inputs["dataSource"] == "posts"

    when_appsync_ready(api, check_resources)


@pulumi.runtime.test
def test_multiple_resolvers_same_data_source(pulumi_mocks, project_cwd):
    api = make_api()
    posts = api.data_source_lambda("posts", handler="functions/simple.handler")
    api.query("getPost", posts)
    api.query("listPosts", posts)
    api.mutation("createPost", posts)

    def check_resources(_):
        resolvers = pulumi_mocks.created_appsync_resolvers()
        assert len(resolvers) == 3
        fields = {r.inputs["field"] for r in resolvers}
        assert fields == {"getPost", "listPosts", "createPost"}

    when_appsync_ready(api, check_resources)


# --- DynamoDB resolvers (require code) ---


@pulumi.runtime.test
def test_dynamo_resolver_with_code_file(pulumi_mocks, project_cwd):
    api = make_api()
    table = DynamoTable("items", fields={"pk": "S"}, partition_key="pk")
    items = api.data_source_dynamo("items", table=table)
    api.query("getItem", items, code="resolvers/getItem.js")

    def check_resources(_):
        resolvers = pulumi_mocks.created_appsync_resolvers()
        assert len(resolvers) == 1
        r = resolvers[0]
        assert r.inputs["kind"] == "UNIT"
        assert r.inputs["dataSource"] == "items"
        assert r.inputs["code"] == Path(project_cwd, "resolvers/getItem.js").read_text()
        # Should have runtime set (APPSYNC_JS)
        assert r.inputs["runtime"]["name"] == "APPSYNC_JS"

    when_appsync_ready(api, check_resources)


@pulumi.runtime.test
def test_dynamo_resolver_with_inline_code(pulumi_mocks, project_cwd):
    api = make_api()
    table = DynamoTable("items", fields={"pk": "S"}, partition_key="pk")
    items = api.data_source_dynamo("items", table=table)
    inline_js = """
export function request(ctx) { return { operation: 'GetItem', key: { id: ctx.args.id } }; }
export function response(ctx) { return ctx.result; }
"""
    api.query("getItem", items, code=inline_js)

    def check_resources(_):
        resolvers = pulumi_mocks.created_appsync_resolvers()
        assert len(resolvers) == 1
        assert resolvers[0].inputs["code"] == inline_js

    when_appsync_ready(api, check_resources)


# --- NONE data source resolvers ---


@pulumi.runtime.test
def test_none_resolver_auto_passthrough(pulumi_mocks, project_cwd):
    """Resolver with None data source and no code gets auto-generated passthrough."""
    api = make_api()
    api.mutation("sendMessage", None)

    def check_resources(_):
        resolvers = pulumi_mocks.created_appsync_resolvers()
        assert len(resolvers) == 1
        r = resolvers[0]
        assert r.inputs["kind"] == "UNIT"
        assert r.inputs["code"] == NONE_PASSTHROUGH_CODE

    when_appsync_ready(api, check_resources)


@pulumi.runtime.test
def test_none_resolver_with_custom_code(pulumi_mocks, project_cwd):
    """Resolver with None data source and custom code uses provided code."""
    api = make_api()
    custom_js = """
export function request(ctx) {
  return { payload: { ...ctx.args, sentAt: util.time.nowISO8601() } };
}
export function response(ctx) { return ctx.result; }
"""
    api.mutation("sendMessage", None, code=custom_js)

    def check_resources(_):
        resolvers = pulumi_mocks.created_appsync_resolvers()
        assert len(resolvers) == 1
        assert resolvers[0].inputs["code"] == custom_js

    when_appsync_ready(api, check_resources)


# --- Pipeline resolvers ---


@pulumi.runtime.test
def test_pipeline_resolver(pulumi_mocks, project_cwd):
    api = make_api()
    table = DynamoTable("items", fields={"pk": "S"}, partition_key="pk")
    items = api.data_source_dynamo("items", table=table)

    auth_step = api.pipe_function("checkAuth", None, code="resolvers/auth.js")
    delete_step = api.pipe_function("doDelete", items, code="resolvers/delete.js")
    api.mutation("deletePost", [auth_step, delete_step])

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
        fn_names = [f.inputs["name"] for f in appsync_fns]
        assert fn_names == ["checkAuth", "doDelete"]

    when_appsync_ready(api, check_resources)


@pulumi.runtime.test
def test_pipeline_resolver_with_none_data_source_step(pulumi_mocks, project_cwd):
    """Pipe function with None data source uses the internal NONE data source."""
    api = make_api()
    auth_step = api.pipe_function("checkAuth", None, code="resolvers/auth.js")
    api.mutation("deletePost", [auth_step])

    def check_resources(_):
        appsync_fns = pulumi_mocks.created_appsync_functions()
        assert len(appsync_fns) == 1
        # The data source should reference NONE
        assert appsync_fns[0].inputs["dataSource"] == "NONE"

    when_appsync_ready(api, check_resources)


@pulumi.runtime.test
def test_pipeline_resolver_default_passthrough_code(pulumi_mocks, project_cwd):
    """Pipeline resolver without explicit code gets passthrough before/after mapping."""
    api = make_api()
    auth_step = api.pipe_function("checkAuth", None, code="resolvers/auth.js")
    api.mutation("deletePost", [auth_step])

    def check_resources(_):
        resolvers = pulumi_mocks.created_appsync_resolvers()
        assert len(resolvers) == 1
        # Pipeline resolver gets passthrough code (before/after stubs)
        assert resolvers[0].inputs["code"] == NONE_PASSTHROUGH_CODE

    when_appsync_ready(api, check_resources)


@pulumi.runtime.test
def test_pipeline_resolver_with_custom_code(pulumi_mocks, project_cwd):
    """Pipeline resolver with explicit code= uses provided code instead of passthrough."""
    api = make_api()
    table = DynamoTable("items", fields={"pk": "S"}, partition_key="pk")
    items = api.data_source_dynamo("items", table=table)

    auth_step = api.pipe_function("checkAuth", None, code="resolvers/auth.js")
    delete_step = api.pipe_function("doDelete", items, code="resolvers/delete.js")

    custom_pipeline_code = """
export function request(ctx) {
    ctx.stash.startTime = util.time.nowISO8601();
    return {};
}
export function response(ctx) {
    return ctx.prev.result;
}
"""
    api.mutation("deletePost", [auth_step, delete_step], code=custom_pipeline_code)

    def check_resources(_):
        resolvers = pulumi_mocks.created_appsync_resolvers()
        assert len(resolvers) == 1
        r = resolvers[0]
        assert r.inputs["kind"] == "PIPELINE"
        assert r.inputs["code"] == custom_pipeline_code

    when_appsync_ready(api, check_resources)


@pulumi.runtime.test
def test_pipe_function_resources_accessible(pulumi_mocks, project_cwd):
    api = make_api()
    auth_step = api.pipe_function("checkAuth", None, code="resolvers/auth.js")
    api.mutation("deletePost", [auth_step])

    def check_resources(_):
        assert auth_step.resources is not None
        assert auth_step.resources.function is not None

    when_appsync_ready(api, check_resources)


@pulumi.runtime.test
def test_resolver_resources_accessible(pulumi_mocks, project_cwd):
    api = make_api()
    posts = api.data_source_lambda("posts", handler="functions/simple.handler")
    resolver = api.query("getPost", posts)

    def check_resources(_):
        assert resolver.resources is not None
        assert resolver.resources.resolver is not None

    when_appsync_ready(api, check_resources)


# --- Validation ---


def test_dynamo_resolver_requires_code(project_cwd):
    api = make_api()
    table = DynamoTable("items", fields={"pk": "S"}, partition_key="pk")
    items = api.data_source_dynamo("items", table=table)
    with pytest.raises(ValueError, match="code is required"):
        api.query("getItem", items)


def test_http_resolver_requires_code(project_cwd):
    api = make_api()
    ext = api.data_source_http("ext", url="https://api.example.com")
    with pytest.raises(ValueError, match="code is required"):
        api.query("getExt", ext)


def test_rds_resolver_requires_code(project_cwd):
    api = make_api()
    db = api.data_source_rds(
        "db",
        cluster_arn="arn:aws:rds:us-east-1:123456789012:cluster:my-cluster",
        secret_arn="arn:aws:secretsmanager:us-east-1:123456789012:secret:my-secret",
        database="mydb",
    )
    with pytest.raises(ValueError, match="code is required"):
        api.query("getData", db)


def test_opensearch_resolver_requires_code(project_cwd):
    api = make_api()
    search = api.data_source_opensearch(
        "search", endpoint="https://search-domain-abc123def456ghij.us-east-1.es.amazonaws.com"
    )
    with pytest.raises(ValueError, match="code is required"):
        api.query("search", search)


def test_duplicate_type_field_raises(project_cwd):
    api = make_api()
    posts = api.data_source_lambda("posts", handler="functions/simple.handler")
    api.query("getPost", posts)
    with pytest.raises(ValueError, match=r"Duplicate resolver for Query\.getPost"):
        api.query("getPost", posts)


def test_empty_field_error(project_cwd):
    api = make_api()
    posts = api.data_source_lambda("posts", handler="functions/simple.handler")
    with pytest.raises(ValueError, match="field cannot be empty"):
        api.query("", posts)


def test_duplicate_pipe_function_name(project_cwd):
    api = make_api()
    api.pipe_function("checkAuth", None, code="resolvers/auth.js")
    with pytest.raises(ValueError, match="Duplicate pipe function name 'checkAuth'"):
        api.pipe_function("checkAuth", None, code="resolvers/auth.js")


def test_pipe_function_requires_code(project_cwd):
    api = make_api()
    with pytest.raises(ValueError, match="code is required for pipe_function"):
        api.pipe_function("checkAuth", None, code="")


def test_resolver_missing_js_file_raises(pulumi_mocks, project_cwd):
    api = make_api()
    table = DynamoTable("items", fields={"pk": "S"}, partition_key="pk")
    items = api.data_source_dynamo("items", table=table)
    resolver = api.query("getItem", items, code="resolvers/missing.js")

    with pytest.raises(FileNotFoundError, match=r"resolvers/missing.js"):
        _ = resolver.resources


def test_pipe_function_missing_js_file_raises_on_resource_creation(pulumi_mocks, project_cwd):
    api = make_api()
    auth_step = api.pipe_function("checkAuth", None, code="resolvers/missing.js")

    with pytest.raises(FileNotFoundError, match=r"resolvers/missing.js"):
        _ = auth_step.resources


def test_empty_type_name_error(project_cwd):
    api = make_api()
    posts = api.data_source_lambda("posts", handler="functions/simple.handler")
    with pytest.raises(ValueError, match="type_name cannot be empty"):
        api.resolver("", "getPost", posts)


def test_empty_pipe_function_name(project_cwd):
    api = make_api()
    with pytest.raises(ValueError, match="Pipe function name cannot be empty"):
        api.pipe_function("", None, code="resolvers/auth.js")


@pulumi.runtime.test
def test_lambda_resolver_with_explicit_code(pulumi_mocks, project_cwd):
    """Lambda data source with explicit code= has runtime and code set."""
    api = make_api()
    posts = api.data_source_lambda("posts", handler="functions/simple.handler")
    inline_js = """
export function request(ctx) { return { payload: ctx.args }; }
export function response(ctx) { return ctx.result; }
"""
    api.query("getPost", posts, code=inline_js)

    def check_resources(_):
        resolvers = pulumi_mocks.created_appsync_resolvers()
        assert len(resolvers) == 1
        r = resolvers[0]
        assert r.inputs["runtime"]["name"] == APPSYNC_JS_RUNTIME
        assert r.inputs["code"] == inline_js

    when_appsync_ready(api, check_resources)


def test_cross_api_data_source_raises(project_cwd):
    """Data source from one API cannot be used in another API's resolver."""
    api1 = make_api("api1")
    api2 = make_api("api2")
    posts = api1.data_source_lambda("posts", handler="functions/simple.handler")
    with pytest.raises(ValueError, match="belongs to AppSync 'api1'"):
        api2.query("getPost", posts)


def test_cross_api_pipe_function_rejected(project_cwd):
    """Pipe function from one API cannot be used in another API's resolver."""
    api1 = make_api("api1")
    api2 = make_api("api2")
    step = api1.pipe_function("checkAuth", None, code="resolvers/auth.js")
    with pytest.raises(ValueError, match="belongs to AppSync 'api1'"):
        api2.mutation("deletePost", [step])


@pulumi.runtime.test
def test_resolver_customize_applied(pulumi_mocks, project_cwd):
    api = make_api()
    posts = api.data_source_lambda("posts", handler="functions/simple.handler")
    api.query("getPost", posts, customize={"resolver": {"field": "getPostCustom"}})

    def check_resources(_):
        resolvers = pulumi_mocks.created_appsync_resolvers()
        assert len(resolvers) == 1
        assert resolvers[0].inputs["field"] == "getPostCustom"

    when_appsync_ready(api, check_resources)


@pulumi.runtime.test
def test_pipe_function_customize_applied(pulumi_mocks, project_cwd):
    api = make_api()
    auth_step = api.pipe_function(
        "checkAuth",
        None,
        code="resolvers/auth.js",
        customize={"function": {"name": "custom-check-auth"}},
    )
    api.mutation("deletePost", [auth_step])

    def check_resources(_):
        appsync_fns = pulumi_mocks.created_appsync_functions()
        assert len(appsync_fns) == 1
        assert appsync_fns[0].inputs["name"] == "custom-check-auth"

    when_appsync_ready(api, check_resources)


# --- Customize key validation ---


def test_resolver_invalid_customize_key(project_cwd):
    api = make_api()
    posts = api.data_source_lambda("posts", handler="functions/simple.handler")
    with pytest.raises(ValueError, match=r"Unknown customization key.*resolvers"):
        api.query("getPost", posts, customize={"resolvers": {}})


def test_pipe_function_invalid_customize_key(project_cwd):
    api = make_api()
    with pytest.raises(ValueError, match=r"Unknown customization key.*fn"):
        api.pipe_function("auth", None, code="resolvers/auth.js", customize={"fn": {}})


def test_empty_pipeline_function_list(project_cwd):
    """Empty list of pipeline functions should be rejected."""
    api = make_api()
    with pytest.raises(ValueError, match="Pipeline function list cannot be empty"):
        api.mutation("deletePost", [])


def test_pipeline_list_with_non_pipe_function(project_cwd):
    """Pipeline list containing non-PipeFunction should raise TypeError."""
    api = make_api()
    with pytest.raises(TypeError, match="Pipeline function list must contain PipeFunction"):
        api.mutation("deletePost", ["not-a-pipe-function"])


# --- Resources created without explicit .resources access ---


@pulumi.runtime.test
def test_data_source_created_without_resources_access(pulumi_mocks, project_cwd):
    """Data source Pulumi resources are created by the parent AppSync._create_resources(),
    even when the user never calls .resources on the AppSyncDataSource instance.
    """
    api = make_api()
    # User creates data source and resolver but never accesses .resources on either
    ds = api.data_source_lambda("posts", handler="functions/simple.handler")
    api.query("getPost", ds)

    def check_resources(_):
        data_sources = pulumi_mocks.created_appsync_data_sources()
        lambda_ds = [d for d in data_sources if d.inputs["name"] == "posts"]
        assert len(lambda_ds) == 1
        assert lambda_ds[0].typ == "aws:appsync/dataSource:DataSource"

    # when_appsync_ready triggers api.resources which runs _create_resources() —
    # the parent creates all child resources internally.
    when_appsync_ready(api, check_resources)


@pulumi.runtime.test
def test_resolvers_created_without_resources_access(pulumi_mocks, project_cwd):
    """Resolver Pulumi resources are created by the parent AppSync._create_resources(),
    even when the user never stores or accesses .resources on AppSyncResolver instances.
    """
    api = make_api()
    posts = api.data_source_lambda("posts", handler="functions/simple.handler")
    # User creates multiple resolvers — return values are not stored
    api.query("getPost", posts)
    api.query("listPosts", posts)
    api.mutation("createPost", posts)

    def check_resources(_):
        resolvers = pulumi_mocks.created_appsync_resolvers()
        assert len(resolvers) == 3
        fields = {r.inputs["field"] for r in resolvers}
        assert fields == {"getPost", "listPosts", "createPost"}
        types = {r.inputs["type"] for r in resolvers}
        assert types == {"Query", "Mutation"}

    when_appsync_ready(api, check_resources)


@pulumi.runtime.test
def test_pipe_functions_created_without_resources_access(pulumi_mocks, project_cwd):
    """Pipe function Pulumi resources are created by the parent AppSync._create_resources(),
    even when the user never calls .resources on PipeFunction instances.
    """
    api = make_api()
    ds = api.data_source_lambda("echo", handler="functions/simple.handler")
    step1 = api.pipe_function("validate", None, code="resolvers/auth.js")
    step2 = api.pipe_function("fetch", ds, code="resolvers/auth.js")
    api.query("getPost", [step1, step2])

    def check_resources(_):
        appsync_fns = pulumi_mocks.created_appsync_functions()
        assert len(appsync_fns) == 2
        fn_names = {f.inputs["name"] for f in appsync_fns}
        assert fn_names == {"validate", "fetch"}

        resolvers = pulumi_mocks.created_appsync_resolvers()
        assert len(resolvers) == 1
        assert resolvers[0].inputs["kind"] == "PIPELINE"

    when_appsync_ready(api, check_resources)
