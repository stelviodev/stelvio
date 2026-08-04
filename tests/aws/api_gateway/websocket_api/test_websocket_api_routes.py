"""Tests for WebsocketApi route validation and handler configuration."""

import pulumi
from pytest import mark, raises

from stelvio.aws.api_gateway.websocket_api import WebsocketApi
from stelvio.aws.function import Function, FunctionConfig

from ...pulumi_mocks import R
from .conftest import when_websocket_api_ready

pytestmark = mark.usefixtures("project_cwd")


@mark.parametrize(
    "route_key",
    ["$connect", "$disconnect", "$default", "sendMessage"],
)
def test_websocket_api_route_accepts_valid_route_keys(route_key):
    api = WebsocketApi("chat")
    api.route(route_key, "functions/simple.handler")


def test_websocket_api_rejects_duplicate_routes():
    api = WebsocketApi("chat")
    api.route("$connect", "functions/simple.handler")

    with raises(ValueError, match=r"Duplicate route key: '\$connect'"):
        api.route("$connect", "functions/disconnect.main")


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
def test_websocket_api_route_handler_configuration(pulumi_mocks, handler, opts):
    api = WebsocketApi("chat")
    api.route("$connect", handler, **opts)
    _ = api.resources

    def check(_):
        functions = pulumi_mocks.created_functions()
        assert len(functions) == 1
        assert functions[0].typ == "aws:lambda/function:Function"
        assert functions[0].name == "test-test-chat-functions-simple_handler"
        assert functions[0].inputs["memorySize"] == 512
        assert functions[0].inputs["timeout"] == 60

    when_websocket_api_ready(api, check)


@pulumi.runtime.test
def test_websocket_api_route_uses_supplied_function(pulumi_mocks):
    function = Function("connect", handler="functions/simple.handler")
    api = WebsocketApi("chat")
    api.route("$connect", function)
    _ = api.resources

    def check(_):
        functions = pulumi_mocks.created_functions()
        assert len(functions) == 1
        assert functions[0].name == "test-test-connect"
        permissions = pulumi_mocks.created_permissions()
        assert len(permissions) == 1
        assert permissions[0].inputs["function"] == "test-test-connect-test-name"
        pulumi_mocks.assert_res(
            "chat-route-sys-connect",
            R.HTTP_API_ROUTE,
            {"routeKey": "$connect"},
            partial=True,
        )

    when_websocket_api_ready(api, check)


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
def test_websocket_api_route_rejects_invalid_handler_configuration(handler, opts, expected_error):
    api = WebsocketApi("chat")

    with raises(ValueError, match=expected_error):
        api.route("$connect", handler, **opts)


def test_websocket_api_route_rejects_function_handler_with_opts():
    function = Function("connect", handler="functions/simple.handler")
    api = WebsocketApi("chat")

    with raises(ValueError, match="Cannot combine a Function handler"):
        api.route("$connect", function, memory=256)


def test_websocket_api_route_rejects_invalid_handler_type():
    api = WebsocketApi("chat")

    with raises(TypeError, match="Invalid handler type: int"):
        api.route("$connect", 123)  # type: ignore[arg-type]


@pulumi.runtime.test
def test_websocket_api_rejects_routes_after_resource_creation(pulumi_mocks):
    api = WebsocketApi("chat")
    api.route("$connect", "functions/simple.handler")
    _ = api.resources

    with raises(RuntimeError, match="after resources have been created"):
        api.route("$default", "functions/simple2.handler")


@mark.parametrize(
    ("first", "second"),
    [
        (
            ("$connect", "functions/simple.handler", {"memory": 256}),
            ("$disconnect", "functions/simple.handler", {"timeout": 30}),
        ),
        (
            ("$connect", FunctionConfig(handler="functions/simple.handler", memory=256), {}),
            ("$disconnect", "functions/simple.handler", {"timeout": 30}),
        ),
    ],
    ids=["string_handlers", "mixed_function_config"],
)
@pulumi.runtime.test
def test_websocket_api_conflicting_lambda_configurations_raise(pulumi_mocks, first, second):
    api = WebsocketApi("chat")
    api.route(first[0], first[1], **first[2])
    api.route(second[0], second[1], **second[2])

    with raises(ValueError, match="Multiple routes trying to configure"):
        _ = api.resources
