"""AppSync resolver tests — query, mutation, subscription, nested types, pipeline."""

from pathlib import Path

import pulumi
import pytest
from pulumi_aws import appsync

from ..conftest import assert_urn
from ..pulumi_mocks import R, tid
from .conftest import (
    API_ID,
    TP,
    deploy_all,
    make_api,
    make_data_source,
    make_dynamo_ds,
    make_lambda_ds,
    make_pipeline_steps,
)

JS_RUNTIME = {"name": "APPSYNC_JS", "runtimeVersion": "1.0.0"}
PASSTHROUGH_CODE = """\
export function request(ctx) {
    return { payload: ctx.args };
}

export function response(ctx) {
    return ctx.result;
}
"""


def pipe_function_id(name: str) -> str:
    return f"fn-{tid(f'{TP}myapi-fn-{name}')}"


# --- Unit resolvers (Lambda) ---


@pytest.mark.parametrize(
    ("setup", "expected_type", "expected_field"),
    [
        (lambda api, ds: api.query("getPost", ds), "Query", "getPost"),
        (lambda api, ds: api.mutation("createPost", ds), "Mutation", "createPost"),
        (lambda api, ds: api.subscription("onCreatePost", ds), "Subscription", "onCreatePost"),
        (lambda api, ds: api.resolver("Post", "author", ds), "Post", "author"),
    ],
    ids=["query", "mutation", "subscription", "nested-type"],
)
def test_lambda_resolver_creates_unit_resolver(
    setup, expected_type, expected_field, pulumi_mocks, project_cwd
):
    """Lambda data source resolvers are Direct Lambda Resolvers — no code needed."""
    api = make_api()
    posts = make_lambda_ds(api)
    setup(api, posts)

    deploy_all()

    pulumi_mocks.assert_res(
        f"myapi-{expected_type}-{expected_field}",
        R.APPSYNC_RESOLVER,
        {
            "type": expected_type,
            "field": expected_field,
            "kind": "UNIT",
            "dataSource": "posts",
            "apiId": API_ID,
        },
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


def test_multiple_resolvers_same_data_source(pulumi_mocks, project_cwd):
    api = make_api()
    posts = make_lambda_ds(api)
    api.query("getPost", posts)
    api.query("listPosts", posts)
    api.mutation("createPost", posts)

    deploy_all()

    resolvers = pulumi_mocks.created(R.APPSYNC_RESOLVER)
    assert sorted(r.inputs["field"] for r in resolvers) == ["createPost", "getPost", "listPosts"]


# --- Resolvers with explicit code (require JS runtime) ---


INLINE_DYNAMO_RESOLVER_CODE = """
export function request(ctx) { return { operation: 'GetItem', key: { id: ctx.args.id } }; }
export function response(ctx) { return ctx.result; }
"""

INLINE_LAMBDA_RESOLVER_CODE = """
export function request(ctx) { return { payload: ctx.args }; }
export function response(ctx) { return ctx.result; }
"""


@pytest.mark.parametrize(
    ("ds_setup", "code", "expected_ds"),
    [
        (lambda api: make_dynamo_ds(api)[0], "resolvers/getItem.js", "items"),
        (lambda api: make_dynamo_ds(api)[0], INLINE_DYNAMO_RESOLVER_CODE, "items"),
        (lambda api: make_lambda_ds(api), INLINE_LAMBDA_RESOLVER_CODE, "posts"),
    ],
    ids=["dynamo-file", "dynamo-inline", "lambda-inline"],
)
def test_resolver_with_explicit_code(ds_setup, code, expected_ds, pulumi_mocks, project_cwd):
    """Resolvers with explicit code= have JS runtime and code set."""
    api = make_api()
    ds = ds_setup(api)
    api.query("getItem", ds, code=code)

    deploy_all()

    from_file = code.endswith(".js")
    expected_code = Path(project_cwd, code).read_text() if from_file else code
    pulumi_mocks.assert_res(
        "myapi-Query-getItem",
        R.APPSYNC_RESOLVER,
        {
            "type": "Query",
            "field": "getItem",
            "kind": "UNIT",
            "dataSource": expected_ds,
            "code": expected_code,
            "runtime": JS_RUNTIME,
        },
        partial=True,
    )
    assert len(pulumi_mocks.created(R.APPSYNC_RESOLVER)) == 1


# --- NONE data source resolvers ---


CUSTOM_NONE_RESOLVER_CODE = """
export function request(ctx) {
  return { payload: { ...ctx.args, sentAt: util.time.nowISO8601() } };
}
export function response(ctx) { return ctx.result; }
"""


@pytest.mark.parametrize(
    ("code", "expected_code"),
    [(None, PASSTHROUGH_CODE), (CUSTOM_NONE_RESOLVER_CODE, CUSTOM_NONE_RESOLVER_CODE)],
    ids=["passthrough", "custom"],
)
def test_none_resolver_code(code, expected_code, pulumi_mocks, project_cwd):
    """NONE data source resolvers default to passthrough unless custom code is provided."""
    api = make_api()
    api.mutation("sendMessage", None, code=code)

    deploy_all()

    pulumi_mocks.assert_res(
        "myapi-Mutation-sendMessage",
        R.APPSYNC_RESOLVER,
        {
            "type": "Mutation",
            "field": "sendMessage",
            "kind": "UNIT",
            "dataSource": "NONE",
            "code": expected_code,
        },
        partial=True,
    )
    pulumi_mocks.assert_res_counts(
        {R.GRAPHQL_API: 1, R.APPSYNC_DATA_SOURCE: 1, R.APPSYNC_RESOLVER: 1}
    )


# --- Pipeline resolvers ---


def test_pipeline_resolver(pulumi_mocks, project_cwd):
    api = make_api()
    api.mutation("deletePost", make_pipeline_steps(api, with_ds=False))

    deploy_all()

    pulumi_mocks.assert_res(
        "myapi-Mutation-deletePost",
        R.APPSYNC_RESOLVER,
        {
            "type": "Mutation",
            "field": "deletePost",
            "kind": "PIPELINE",
            "runtime": JS_RUNTIME,
            "pipelineConfig": {"functions": [pipe_function_id("checkAuth")]},
            "code": PASSTHROUGH_CODE,
            "apiId": API_ID,
        },
    )
    pulumi_mocks.assert_res(
        "myapi-fn-checkAuth",
        R.APPSYNC_FUNCTION,
        {
            "name": "checkAuth",
            "code": Path(project_cwd, "resolvers/auth.js").read_text(),
            "runtime": JS_RUNTIME,
            "dataSource": "NONE",
            "apiId": API_ID,
        },
    )
    pulumi_mocks.assert_res_counts(
        {
            R.GRAPHQL_API: 1,
            R.APPSYNC_DATA_SOURCE: 1,
            R.APPSYNC_FUNCTION: 1,
            R.APPSYNC_RESOLVER: 1,
        }
    )


def test_pipeline_resolver_runs_steps_in_order(pulumi_mocks, project_cwd):
    api = make_api()
    api.mutation("deletePost", make_pipeline_steps(api))

    deploy_all()

    pulumi_mocks.assert_res(
        "myapi-Mutation-deletePost",
        R.APPSYNC_RESOLVER,
        {
            "kind": "PIPELINE",
            "pipelineConfig": {
                "functions": [pipe_function_id("checkAuth"), pipe_function_id("doDelete")]
            },
            "code": PASSTHROUGH_CODE,
        },
        partial=True,
    )
    functions = pulumi_mocks.created(R.APPSYNC_FUNCTION)
    assert sorted((f.inputs["name"], f.inputs["dataSource"]) for f in functions) == [
        ("checkAuth", "NONE"),
        ("doDelete", "items"),
    ]
    assert len(pulumi_mocks.created(R.APPSYNC_RESOLVER)) == 1


def test_pipeline_resolver_code(pulumi_mocks, project_cwd):
    """Explicit code= replaces the pipeline's passthrough code."""
    code = """
export function request(ctx) {
    ctx.stash.startTime = util.time.nowISO8601();
    return {};
}
export function response(ctx) { return ctx.prev.result; }
"""
    api = make_api()
    api.mutation("deletePost", make_pipeline_steps(api), code=code)

    deploy_all()

    pulumi_mocks.assert_res(
        "myapi-Mutation-deletePost",
        R.APPSYNC_RESOLVER,
        {"kind": "PIPELINE", "code": code},
        partial=True,
    )
    assert len(pulumi_mocks.created(R.APPSYNC_RESOLVER)) == 1


def test_resources_accessible_after_creation(pulumi_mocks, project_cwd):
    """Resolver and pipe function resources are accessible."""
    api = make_api()
    posts = make_lambda_ds(api)
    resolver = api.query("getPost", posts)
    auth_step = api.pipe_function(
        "checkAuth",
        None,
        code="resolvers/auth.js",
    )
    api.mutation("deletePost", [auth_step])

    deploy_all()

    assert isinstance(resolver.resources.resolver, appsync.Resolver)
    assert isinstance(auth_step.resources.function, appsync.Function)


# --- Validation ---


@pytest.mark.parametrize("ds_type", ["dynamo", "http", "rds", "opensearch"])
def test_non_lambda_resolver_requires_code(ds_type, project_cwd):
    api = make_api()
    ds = make_data_source(api, ds_type)
    with pytest.raises(ValueError, match="code is required"):
        api.query("getItem", ds)


def test_duplicate_type_field_raises(project_cwd):
    api = make_api()
    posts = make_lambda_ds(api)
    api.query("getPost", posts)
    with pytest.raises(ValueError, match=r"Duplicate resolver for Query\.getPost"):
        api.query("getPost", posts)


@pytest.mark.parametrize(
    ("method_call", "match"),
    [
        (lambda api, _: api.query("", _), "field cannot be empty"),
        (lambda api, _: api.resolver("", "getPost", _), "type_name cannot be empty"),
        (
            lambda api, _: api.pipe_function("", None, code="resolvers/auth.js"),
            "Pipe function name cannot be empty",
        ),
        (
            lambda api, _: api.pipe_function("checkAuth", None, code=""),
            "code is required for pipe_function",
        ),
        (lambda api, _: api.mutation("deletePost", []), "Pipeline function list cannot be empty"),
    ],
    ids=["empty-field", "empty-type", "empty-pipe-name", "empty-pipe-code", "empty-pipeline-list"],
)
def test_resolver_validation(method_call, match, project_cwd):
    api = make_api()
    ds = make_lambda_ds(api)
    with pytest.raises(ValueError, match=match):
        method_call(api, ds)


def test_pipeline_list_with_non_pipe_function(project_cwd):
    """Pipeline list containing non-PipeFunction should raise TypeError."""
    api = make_api()
    with pytest.raises(TypeError, match="Pipeline function list must contain PipeFunction"):
        api.mutation("deletePost", ["not-a-pipe-function"])


def test_duplicate_pipe_function_name(project_cwd):
    api = make_api()
    api.pipe_function("checkAuth", None, code="resolvers/auth.js")
    with pytest.raises(ValueError, match="Duplicate pipe function name"):
        api.pipe_function("checkAuth", None, code="resolvers/auth.js")


@pytest.mark.parametrize(
    "setup",
    [
        lambda api: api.query("getItem", make_dynamo_ds(api)[0], code="resolvers/missing.js"),
        lambda api: api.pipe_function("checkAuth", None, code="resolvers/missing.js"),
    ],
    ids=["resolver", "pipe-function"],
)
def test_missing_js_file_raises(setup, pulumi_mocks, project_cwd):
    api = make_api()
    component = setup(api)
    with pytest.raises(FileNotFoundError, match=r"resolvers/missing.js"):
        _ = component.resources


@pytest.mark.parametrize(
    "setup",
    [
        lambda api1, api2: api2.query("getPost", make_lambda_ds(api1)),
        lambda api1, api2: api2.mutation(
            "deletePost",
            [api1.pipe_function("checkAuth", None, code="resolvers/auth.js")],
        ),
    ],
    ids=["data-source", "pipe-function"],
)
def test_cross_api_resource_rejected(setup, project_cwd):
    """Resources from one API cannot be used in another API's resolver."""
    api1 = make_api("api1")
    api2 = make_api("api2")
    with pytest.raises(ValueError, match="belongs to AppSync 'api1'"):
        setup(api1, api2)


def test_resolver_customize_applied(pulumi_mocks, project_cwd):
    api = make_api()
    api.query(
        "getPost",
        make_lambda_ds(api),
        customize={"resolver": {"field": "getPostCustom"}},
    )

    deploy_all()

    pulumi_mocks.assert_res(
        "myapi-Query-getPost",
        R.APPSYNC_RESOLVER,
        {"type": "Query", "field": "getPostCustom"},
        partial=True,
    )
    assert len(pulumi_mocks.created(R.APPSYNC_RESOLVER)) == 1


def test_pipe_function_customize_applied(pulumi_mocks, project_cwd):
    api = make_api()
    step = api.pipe_function(
        "checkAuth",
        None,
        code="resolvers/auth.js",
        customize={"function": {"name": "custom-check-auth"}},
    )
    api.mutation("deletePost", [step])

    deploy_all()

    pulumi_mocks.assert_res(
        "myapi-fn-checkAuth", R.APPSYNC_FUNCTION, {"name": "custom-check-auth"}, partial=True
    )
    assert len(pulumi_mocks.created(R.APPSYNC_FUNCTION)) == 1


# --- Customize key validation ---


@pytest.mark.parametrize(
    ("bad_key", "match_pattern"),
    [
        ("resolvers", r"Unknown customization key.*resolvers"),
        ("fn", r"Unknown customization key.*fn"),
    ],
    ids=["resolver", "pipe-function"],
)
def test_invalid_customize_key_rejected(bad_key, match_pattern, project_cwd):
    api = make_api()
    if bad_key == "resolvers":
        posts = make_lambda_ds(api)
        with pytest.raises(ValueError, match=match_pattern):
            api.query("getPost", posts, customize={bad_key: {}})
    else:
        with pytest.raises(ValueError, match=match_pattern):
            api.pipe_function("auth", None, code="resolvers/auth.js", customize={bad_key: {}})


# --- Resources created without explicit .resources access ---


def test_resources_created_without_explicit_resources_access(pulumi_mocks, project_cwd):
    """Data sources, resolvers and pipe functions are components of their own: the deploy loop
    creates them though the user never reads .resources on them.
    """
    api = make_api()
    posts = make_lambda_ds(api)
    api.query("getPost", posts)
    api.mutation("createPost", posts)

    echo = api.data_source_lambda("echo", handler="functions/simple.handler")
    step1 = api.pipe_function("validate", None, code="resolvers/auth.js")
    step2 = api.pipe_function("fetch", echo, code="resolvers/auth.js")
    api.query("listPosts", [step1, step2])

    deploy_all()

    data_sources = pulumi_mocks.created(R.APPSYNC_DATA_SOURCE)
    assert sorted(d.inputs["name"] for d in data_sources) == ["NONE", "echo", "posts"]
    resolvers = pulumi_mocks.created(R.APPSYNC_RESOLVER)
    assert sorted(r.inputs["field"] for r in resolvers) == ["createPost", "getPost", "listPosts"]
    functions = pulumi_mocks.created(R.APPSYNC_FUNCTION)
    assert sorted(f.inputs["name"] for f in functions) == ["fetch", "validate"]


# `parent=self` lives in ResourceOptions (invisible to input mocks), but it surfaces
# in the child component's URN as the `stelvio:aws:AppSync$` parent segment.


@pulumi.runtime.test
def test_resolver_parented_to_appsync(pulumi_mocks, project_cwd):
    api = make_api()
    posts = make_lambda_ds(api)
    resolver = api.query("getPost", posts)
    _ = resolver.resources

    def check(urn):
        assert_urn(
            urn,
            "stelvio:aws:AppSync",
            "stelvio:aws:AppSyncResolver",
            "myapi-resolver-Query-getPost",
        )

    return resolver.urn.apply(check)


@pulumi.runtime.test
def test_pipe_function_parented_to_appsync(pulumi_mocks, project_cwd):
    api = make_api()
    step = api.pipe_function("checkAuth", None, code="resolvers/auth.js")
    _ = step.resources

    def check(urn):
        assert_urn(urn, "stelvio:aws:AppSync", "stelvio:aws:PipeFunction", "myapi-fn-checkAuth")

    return step.urn.apply(check)
