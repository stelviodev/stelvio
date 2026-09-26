"""`route()`: what it accepts, what it rejects, and how routes may overlap."""

import re

import pulumi
from pytest import mark, param, raises

from stelvio.aws.api_gateway import HTTPMethod, RestApi
from stelvio.aws.function import Function, FunctionConfig

from ....conftest import TP
from ...pulumi_mocks import R

COMPLETE_HANDLER_WITH_OPTIONS = (
    "Invalid configuration: cannot combine complete handler configuration with additional options"
)


@mark.parametrize(
    ("handler", "opts", "expected_error"),
    [
        param(
            None,
            {},
            "Missing handler configuration: when handler argument is None, 'handler' option must "
            "be provided",
            id="no_handler",
        ),
        param(
            "users.index",
            {"handler": "users.other"},
            "Ambiguous handler configuration: handler is specified both as positional argument "
            "and in options",
            id="handler_twice",
        ),
        param(
            {"handler": "users.index"}, {"memory": 256}, COMPLETE_HANDLER_WITH_OPTIONS, id="dict"
        ),
        param(
            FunctionConfig(handler="users.index"),
            {"memory": 256},
            COMPLETE_HANDLER_WITH_OPTIONS,
            id="config",
        ),
        # Lambda: Function() needs context from fixtures, unavailable at collection
        param(
            lambda: Function("test-1", handler="users.index"),
            {"memory": 256},
            "Cannot combine a Function handler with function options.",
            id="instance",
        ),
    ],
)
def test_api_route_rejects_incomplete_or_ambiguous_handler(handler, opts, expected_error):
    if callable(handler):
        handler = handler()
    api = RestApi("test-api")
    with raises(ValueError, match=expected_error):
        api.route("GET", "/users", handler, **opts)


@mark.parametrize("handler", [param(123, id="int"), param(3.14, id="float"), param([], id="list")])
def test_api_route_rejects_handler_of_wrong_type(handler):
    api = RestApi("test-api")
    with raises(TypeError, match=f"Invalid handler type: {type(handler).__name__}"):
        api.route("GET", "/users", handler)


@mark.parametrize(
    ("path", "expected_error"),
    [
        ("", "Path must start with '/'"),
        ("/" + "x" * 8192, "Path too long"),
        ("/users/{}/orders", "Empty path parameters not allowed"),
        ("/".join(f"/{{{i}}}" for i in range(11)), "Maximum of 10 path parameters allowed"),
        ("/users/{id}{name}", "Adjacent path parameters not allowed"),
        ("/users/{id}/orders/{id}", "Duplicate path parameters not allowed"),
        ("/users/{123-id}", "Invalid parameter name: 123-id"),
        ("/users/{proxy+}/orders", "Greedy parameter must be at the end of the path"),
        ("/users/{path+}", re.escape("Only {proxy+} is supported for greedy paths")),
    ],
)
def test_api_route_rejects_invalid_path(path, expected_error):
    api = RestApi("test-api")
    with raises(ValueError, match=expected_error):
        api.route("GET", path, "users.handler")


@mark.parametrize(
    ("method", "expected_error"),
    [
        ("INVALID", "Invalid HTTP method: INVALID"),
        (["GET", "INVALID"], "Invalid HTTP method: INVALID"),
        (["GET", "ANY"], re.escape("ANY and * not allowed in method list")),
        (["GET", "*"], re.escape("ANY and * not allowed in method list")),
        ([], "Method list cannot be empty"),
    ],
)
def test_api_route_rejects_invalid_methods(method, expected_error):
    api = RestApi("test-api")
    with raises(ValueError, match=expected_error):
        api.route(method, "/users", "users.handler")


@mark.parametrize(
    ("method", "expected_error"),
    [
        ([123], "Invalid method type in list: <class 'int'>"),
        ([[str]], "Invalid method type in list: <class 'list'>"),
        ([3.14], "Invalid method type in list: <class 'float'>"),
    ],
)
def test_api_route_rejects_methods_of_wrong_type(method, expected_error):
    api = RestApi("test-api")
    with raises(TypeError, match=expected_error):
        api.route(method, "/users", "users.handler")


@mark.parametrize(
    ("method", "expected_methods"),
    [
        param("get", ["GET"], id="lowercase"),
        param("*", ["ANY"], id="star"),
        param(HTTPMethod.PATCH, ["PATCH"], id="enum"),
        param(["get", "Post"], ["GET", "POST"], id="mixed_case_list"),
        param([HTTPMethod.GET, "post", HTTPMethod.PUT], ["GET", "POST", "PUT"], id="mixed_list"),
    ],
)
def test_api_route_normalizes_methods(pulumi_mocks, project_cwd, method, expected_methods):
    """Upper-case names and ANY are the plain case, covered with the resource graph."""
    api = RestApi("test-api")
    api.route(method, "/users", "functions/simple.handler")

    @pulumi.runtime.test
    def deploy():
        return api.resources

    deploy()

    assert {m.name for m in pulumi_mocks.created(R.API_METHOD)} == {
        f"{TP}test-api-method-{m} /users" for m in expected_methods
    }


@mark.parametrize(
    ("make_auth", "expected_error"),
    [
        param(
            lambda api: api.add_token_authorizer("token", "functions/authorizers/jwt.handler"),
            "cognito_scopes only works with Cognito authorizers.*token authorizer",
            id="token",
        ),
        param(
            lambda api: api.add_request_authorizer(
                "request", "functions/authorizers/request.handler"
            ),
            "cognito_scopes only works with Cognito authorizers.*request authorizer",
            id="request",
        ),
        param(
            lambda api: "IAM",
            "cognito_scopes only works with Cognito authorizers.*IAM authorization",
            id="iam",
        ),
        param(
            lambda api: False,
            "cognito_scopes only works with Cognito authorizers.*no authorization",
            id="public",
        ),
        param(
            lambda api: None,
            "cognito_scopes only works with Cognito authorizers.*no authorization",
            id="default",
        ),
    ],
)
def test_api_route_rejects_cognito_scopes_without_cognito_authorizer(make_auth, expected_error):
    api = RestApi("test-api")
    with raises(ValueError, match=expected_error):
        api.route("POST", "/users", "users.create", auth=make_auth(api), cognito_scopes=["admin"])


@mark.parametrize(
    ("first_route", "second_route"),
    [
        # Same file, both trying to configure
        (
            ("GET", "/users", {"handler": "users.index", "memory": 256}),
            ("POST", "/users", {"handler": "users.index", "timeout": 30}),
        ),
        # Using FunctionConfig instead of dict
        (
            ("GET", "/users", FunctionConfig(handler="users.index", memory=256)),
            ("POST", "/users", FunctionConfig(handler="users.index", timeout=30)),
        ),
    ],
)
def test_api_route_conflicts(first_route, second_route):
    """Test that only one route can configure a shared function."""
    api = RestApi("test-api")
    api.route(first_route[0], first_route[1], first_route[2])
    api.route(second_route[0], second_route[1], second_route[2])

    with raises(ValueError, match="Multiple routes try to configure the same Lambda function"):
        _ = api.resources


def test_route_conflict_ignores_trailing_slash():
    """`/users/` and `/users` are one AWS resource, so the same verb on both conflicts."""
    api = RestApi("test-api")
    api.route("GET", "/users/", "users.index")
    with raises(ValueError, match="Route conflict"):
        api.route("GET", "/users", "users.index")


@mark.parametrize(
    ("first_method", "second_method", "should_conflict"),
    [
        # Exact same method - should conflict
        ("GET", "GET", True),
        # Different methods - should not conflict
        ("GET", "POST", False),
        # List of methods with overlap - should conflict
        (["GET", "POST"], "GET", True),
        # List of methods without overlap - should not conflict
        (["GET", "POST"], "PUT", False),
        # ANY method should conflict with everything
        ("ANY", "GET", True),
        ("GET", "ANY", True),
        # Wildcard (*) method should conflict with everything
        ("*", "POST", True),
        ("DELETE", "*", True),
    ],
)
def test_route_method_path_conflicts(first_method, second_method, should_conflict):
    """Test that routes with the same path and overlapping methods conflict."""
    api = RestApi("test-api")
    api.route(first_method, "/users", "users.handler")

    if should_conflict:
        with raises(ValueError, match="Route conflict"):
            api.route(second_method, "/users", "users.handler2")
    else:
        api.route(second_method, "/users", "users.handler2")
