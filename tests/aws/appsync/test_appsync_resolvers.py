"""AppSync resolver tests — query, mutation, subscription, nested types, pipeline."""

from pathlib import Path

import pulumi
import pytest

from stelvio.aws.appsync.constants import APPSYNC_JS_RUNTIME, NONE_PASSTHROUGH_CODE

from .conftest import (
    assert_appsync_function_inputs,
    assert_resolver_inputs,
    make_api,
    make_data_source,
    make_dynamo_ds,
    make_lambda_ds,
    make_pipeline_steps,
    when_appsync_ready,
)

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
@pulumi.runtime.test
def test_lambda_resolver_creates_unit_resolver(
    setup, expected_type, expected_field, pulumi_mocks, project_cwd
):
    """Lambda data source resolvers are Direct Lambda Resolvers — no code needed."""
    api = make_api()
    posts = make_lambda_ds(api)
    setup(api, posts)

    def check_resources(_):
        inputs = assert_resolver_inputs(
            pulumi_mocks,
            type=expected_type,
            field=expected_field,
            kind="UNIT",
            dataSource="posts",
        )
        assert "runtime" not in inputs

    when_appsync_ready(api, check_resources)


@pulumi.runtime.test
def test_multiple_resolvers_same_data_source(pulumi_mocks, project_cwd):
    api = make_api()
    posts = make_lambda_ds(api)
    api.query("getPost", posts)
    api.query("listPosts", posts)
    api.mutation("createPost", posts)

    def check_resources(_):
        resolvers = pulumi_mocks.created_appsync_resolvers()
        assert len(resolvers) == 3
        fields = {r.inputs["field"] for r in resolvers}
        assert fields == {"getPost", "listPosts", "createPost"}

    when_appsync_ready(api, check_resources)


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
@pulumi.runtime.test
def test_resolver_with_explicit_code(ds_setup, code, expected_ds, pulumi_mocks, project_cwd):
    """Resolvers with explicit code= have JS runtime and code set."""
    api = make_api()
    ds = ds_setup(api)
    api.query("getItem", ds, code=code)

    def check_resources(_):
        from_file = code.endswith(".js")
        expected_code = Path(project_cwd, code).read_text() if from_file else code
        inputs = assert_resolver_inputs(
            pulumi_mocks,
            type="Query",
            field="getItem",
            kind="UNIT",
            dataSource=expected_ds,
            code=expected_code,
        )
        assert inputs["runtime"]["name"] == APPSYNC_JS_RUNTIME

    when_appsync_ready(api, check_resources)


# --- NONE data source resolvers ---


CUSTOM_NONE_RESOLVER_CODE = """
export function request(ctx) {
  return { payload: { ...ctx.args, sentAt: util.time.nowISO8601() } };
}
export function response(ctx) { return ctx.result; }
"""


@pytest.mark.parametrize(
    ("code", "expected_code"),
    [(None, NONE_PASSTHROUGH_CODE), (CUSTOM_NONE_RESOLVER_CODE, CUSTOM_NONE_RESOLVER_CODE)],
    ids=["passthrough", "custom"],
)
@pulumi.runtime.test
def test_none_resolver_code(code, expected_code, pulumi_mocks, project_cwd):
    """NONE data source resolvers default to passthrough unless custom code is provided."""
    api = make_api()
    api.mutation("sendMessage", None, code=code)

    def check_resources(_):
        assert_resolver_inputs(
            pulumi_mocks,
            type="Mutation",
            field="sendMessage",
            kind="UNIT",
            code=expected_code,
        )

    when_appsync_ready(api, check_resources)


# --- Pipeline resolvers ---


@pytest.mark.parametrize("with_ds", [True, False], ids=["standard", "none-ds"])
@pulumi.runtime.test
def test_pipeline_resolver(with_ds, pulumi_mocks, project_cwd):
    api = make_api()
    steps = make_pipeline_steps(api, with_ds=with_ds)
    api.mutation("deletePost", steps)

    def check_resources(_):
        assert_resolver_inputs(pulumi_mocks, kind="PIPELINE")
        if with_ds:
            fns = pulumi_mocks.created_appsync_functions()
            assert len(fns) == 2
            assert [f.inputs["name"] for f in fns] == ["checkAuth", "doDelete"]
        else:
            assert_appsync_function_inputs(pulumi_mocks, "checkAuth", dataSource="NONE")

    when_appsync_ready(api, check_resources)


@pytest.mark.parametrize(
    "code",
    [
        None,
        """
export function request(ctx) {
    ctx.stash.startTime = util.time.nowISO8601();
    return {};
}
export function response(ctx) { return ctx.prev.result; }
""",
    ],
    ids=["passthrough", "custom"],
)
@pulumi.runtime.test
def test_pipeline_resolver_code(code, pulumi_mocks, project_cwd):
    """Pipeline uses passthrough code by default, or explicit code=."""
    api = make_api()
    steps = make_pipeline_steps(api)
    api.mutation("deletePost", steps, code=code)
    expected = NONE_PASSTHROUGH_CODE if code is None else code

    def check_resources(_):
        assert_resolver_inputs(
            pulumi_mocks,
            kind="PIPELINE",
            code=expected,
        )

    when_appsync_ready(api, check_resources)


@pulumi.runtime.test
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

    def check_resources(_):
        assert resolver.resources.resolver is not None
        assert auth_step.resources.function is not None

    when_appsync_ready(api, check_resources)


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


@pulumi.runtime.test
def test_resolver_customize_applied(pulumi_mocks, project_cwd):
    api = make_api()
    api.query(
        "getPost",
        make_lambda_ds(api),
        customize={"resolver": {"field": "getPostCustom"}},
    )

    def check_resources(_):
        assert_resolver_inputs(
            pulumi_mocks,
            type="Query",
            field="getPostCustom",
        )

    when_appsync_ready(api, check_resources)


@pulumi.runtime.test
def test_pipe_function_customize_applied(pulumi_mocks, project_cwd):
    api = make_api()
    step = api.pipe_function(
        "checkAuth",
        None,
        code="resolvers/auth.js",
        customize={"function": {"name": "custom-check-auth"}},
    )
    api.mutation("deletePost", [step])

    def check_resources(_):
        assert_appsync_function_inputs(
            pulumi_mocks,
            name="custom-check-auth",
        )

    when_appsync_ready(api, check_resources)


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


@pulumi.runtime.test
def test_resources_created_without_explicit_resources_access(pulumi_mocks, project_cwd):
    """Data sources, resolvers, and pipe functions are created by AppSync._create_resources()
    even when the user never calls .resources on child instances.
    """
    api = make_api()
    posts = make_lambda_ds(api)
    api.query("getPost", posts)
    api.mutation("createPost", posts)

    echo = api.data_source_lambda("echo", handler="functions/simple.handler")
    step1 = api.pipe_function("validate", None, code="resolvers/auth.js")
    step2 = api.pipe_function("fetch", echo, code="resolvers/auth.js")
    api.query("listPosts", [step1, step2])

    def check_resources(_):
        ds = pulumi_mocks.created_appsync_data_sources()
        assert any(d.inputs["name"] == "posts" for d in ds)

        resolvers = pulumi_mocks.created_appsync_resolvers()
        assert {r.inputs["field"] for r in resolvers} == {
            "getPost",
            "createPost",
            "listPosts",
        }

        fns = pulumi_mocks.created_appsync_functions()
        assert {f.inputs["name"] for f in fns} == {"validate", "fetch"}

    when_appsync_ready(api, check_resources)
