"""Tests for HttpApi route validation and handler configuration."""

import pulumi
from pytest import mark, raises

from stelvio.aws.api_gateway.http_api import HttpApi
from stelvio.aws.api_gateway.rest_api.constants import ROUTE_MAX_LENGTH, ROUTE_MAX_PARAMS
from stelvio.aws.function import Function, FunctionConfig

from .conftest import when_http_api_ready

pytestmark = mark.usefixtures("project_cwd")


@mark.parametrize(
    ("method", "path"),
    [
        ("ANY", "/users"),
        ("*", "/users"),
        ("ANY", "$default"),
    ],
)
def test_http_api_route_accepts_valid_method_and_path(method, path):
    api = HttpApi("my-api")

    api.route(method, path, "functions/simple.handler")


@mark.parametrize(
    ("method", "path", "expected_error"),
    [
        ("GET", "users", "start with '/'"),
        ("GET", "/users/{}", "Empty path parameters"),
        ("GET", "/users/{id}{name}", "Adjacent"),
        ("GET", "/users/{id}/orders/{id}", "Duplicate"),
        ("GET", "/files/{proxy+}/other", "end of the path"),
        ("GET", "/files/{other+}", r"Only.*proxy"),
        ("GET", "/" + "x" * ROUTE_MAX_LENGTH, "Path too long"),
        (
            "GET",
            "/" + "/".join(f"{{param{index}}}" for index in range(ROUTE_MAX_PARAMS + 1)),
            "Maximum of 10 path parameters",
        ),
        ([], "/users", "Method list cannot be empty"),
        (["GET", "ANY"], "/users", "ANY"),
        (["GET", "*"], "/users", "ANY"),
        ("TRACE", "/users", "Invalid HTTP method"),
        ("GET", "$default", r"\$default"),
        (["GET", "POST"], "$default", r"\$default"),
    ],
)
def test_http_api_route_rejects_invalid_method_or_path(method, path, expected_error):
    api = HttpApi("my-api")

    with raises(ValueError, match=expected_error):
        api.route(method, path, "functions/simple.handler")


@mark.parametrize("method", [[123], [[str]]])
def test_http_api_route_rejects_invalid_method_type(method):
    api = HttpApi("my-api")

    with raises(TypeError, match="Invalid method type in list"):
        api.route(method, "/users", "functions/simple.handler")


def test_http_api_route_accepts_maximum_path_length_and_parameters():
    path = "/" + "/".join(f"{{param{index}}}" for index in range(ROUTE_MAX_PARAMS))
    path += "x" * (ROUTE_MAX_LENGTH - len(path))

    HttpApi("my-api").route("GET", path, "functions/simple.handler")


@mark.parametrize(
    ("handler", "opts"),
    [
        ("functions/simple.handler", {"memory": 512, "timeout": 60}),
        (FunctionConfig(handler="functions/simple.handler", memory=512, timeout=60), {}),
        ({"handler": "functions/simple.handler", "memory": 512, "timeout": 60}, {}),
    ],
    ids=["string_handler_and_opts", "function_config", "dict"],
)
@pulumi.runtime.test
def test_http_api_route_handler_configuration(pulumi_mocks, handler, opts):
    api = HttpApi("my-api")
    api.route("GET", "/users", handler, **opts)
    _ = api.resources

    def check(_):
        functions = pulumi_mocks.created_functions()

        assert len(functions) == 1
        assert functions[0].typ == "aws:lambda/function:Function"
        assert functions[0].name == "test-test-my-api-functions-simple_handler"
        assert functions[0].inputs["memorySize"] == 512
        assert functions[0].inputs["timeout"] == 60

    when_http_api_ready(api, check)


@pulumi.runtime.test
def test_http_api_route_uses_supplied_function(pulumi_mocks):
    function = Function("users-function", handler="functions/users.handler")
    api = HttpApi("my-api")
    api.route("GET", "/users", function)
    _ = api.resources

    def check(_):
        functions = pulumi_mocks.created_functions()

        assert len(functions) == 1
        assert functions[0].name == "test-test-users-function"

        permissions = pulumi_mocks.created_permissions()
        assert len(permissions) == 1
        assert permissions[0].inputs["function"] == "test-test-users-function-test-name"

    when_http_api_ready(api, check)


@mark.parametrize(
    ("handler", "opts", "expected_error"),
    [
        (
            None,
            {},
            "Missing handler configuration: when handler argument is None, "
            "'handler' option must be provided",
        ),
        (
            "functions/simple.handler",
            {"handler": "functions/users.handler"},
            "Ambiguous handler configuration",
        ),
        (
            {"handler": "functions/simple.handler"},
            {"memory": 256},
            "Invalid configuration: cannot combine complete handler configuration",
        ),
    ],
)
def test_http_api_route_rejects_invalid_handler_configuration(handler, opts, expected_error):
    api = HttpApi("my-api")

    with raises(ValueError, match=expected_error):
        api.route("GET", "/users", handler, **opts)


def test_http_api_route_rejects_function_handler_with_opts():
    api = HttpApi("my-api")
    function = Function("users-function", handler="functions/users.handler")

    with raises(ValueError, match="Cannot combine a Function handler with function options"):
        api.route("GET", "/users", function, memory=512)


def test_http_api_route_rejects_invalid_handler_type():
    api = HttpApi("my-api")

    with raises(TypeError, match="Invalid handler type: int"):
        api.route("GET", "/users", 123)  # type: ignore[arg-type]


@pulumi.runtime.test
def test_http_api_rejects_route_changes_after_resources_are_created(pulumi_mocks):
    api = HttpApi("my-api")
    api.route("GET", "/users", "functions/simple.handler")
    _ = api.resources

    with raises(RuntimeError, match="Cannot modify HttpApi 'my-api'"):
        api.route("POST", "/users", "functions/users.handler")


@pulumi.runtime.test
def test_http_api_rejects_authorizer_changes_after_resources_are_created(pulumi_mocks):
    api = HttpApi("my-api")
    api.route("GET", "/users", "functions/simple.handler")
    _ = api.resources

    with raises(RuntimeError, match="Cannot modify HttpApi 'my-api'"):
        api.add_jwt_authorizer(
            "auth",
            issuer="https://issuer.example.com",
            audiences=["client-id"],
        )


@mark.parametrize(
    ("first", "second"),
    [
        (
            ("GET", "/users", "functions/simple.handler", {"memory": 256}),
            ("POST", "/users", "functions/simple.handler", {"timeout": 30}),
        ),
        (
            ("GET", "/users", FunctionConfig(handler="functions/simple.handler", memory=256), {}),
            ("POST", "/users", "functions/simple.handler", {"timeout": 30}),
        ),
    ],
    ids=["string_handlers", "mixed_function_config"],
)
@pulumi.runtime.test
def test_http_api_conflicting_lambda_configurations_raise(pulumi_mocks, first, second):
    api = HttpApi("my-api")
    api.route(first[0], first[1], first[2], **first[3])
    api.route(second[0], second[1], second[2], **second[3])

    with raises(ValueError, match="Multiple routes try to configure the same Lambda function"):
        _ = api.resources
