"""Tests for WebsocketApi route validation and handler configuration."""

import pulumi
from pytest import mark, raises

from stelvio.aws.api_gateway.websocket_api import WebsocketApi
from stelvio.aws.function import Function, FunctionConfig

from ...pulumi_mocks import ACCOUNT_ID, DEFAULT_REGION, R, tid, tn
from .conftest import TP, WEBSOCKET_API_ID, websocket_api_counts, when_websocket_api_ready

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
        # Lambda Function inputs include code/role assets — assert the configured fields.
        pulumi_mocks.assert_res(
            "chat-functions-simple_handler",
            R.FUNCTION,
            {"handler": "simple.handler", "memorySize": 512.0, "timeout": 60.0},
            partial=True,
        )
        pulumi_mocks.assert_res_counts(
            websocket_api_counts(
                function_count=1,
                route_count=1,
                integration_count=1,
                permission_count=1,
            )
        )

    when_websocket_api_ready(api, check)


@pulumi.runtime.test
def test_websocket_api_route_uses_supplied_function(pulumi_mocks):
    function = Function("connect", handler="functions/simple.handler")
    api = WebsocketApi("chat")
    api.route("$connect", function)
    _ = api.resources

    def check(_):
        pulumi_mocks.assert_res("connect", R.FUNCTION)
        pulumi_mocks.assert_res(
            "chat-permission-chat-connect",
            R.LAMBDA_PERMISSION,
            {
                "action": "lambda:InvokeFunction",
                "function": tn(TP + "connect"),
                "principal": "apigateway.amazonaws.com",
                "sourceArn": (
                    f"arn:aws:execute-api:{DEFAULT_REGION}:{ACCOUNT_ID}:{WEBSOCKET_API_ID}/*/*"
                ),
            },
        )
        pulumi_mocks.assert_res(
            "chat-route-sys-connect",
            R.HTTP_API_ROUTE,
            {
                "apiId": WEBSOCKET_API_ID,
                "routeKey": "$connect",
                "target": f"integrations/{tid(TP + 'chat-integration-chat-connect')}",
            },
        )
        pulumi_mocks.assert_res_counts(
            websocket_api_counts(
                function_count=1,
                route_count=1,
                integration_count=1,
                permission_count=1,
            )
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


@pulumi.runtime.test
def test_websocket_api_folder_handlers_get_distinct_lambdas(pulumi_mocks):
    """folder/:: configs that share a handler suffix must not collide."""
    api = WebsocketApi("chat")
    api.route("$connect", "functions/folder::handler.fn")
    api.route("$disconnect", "functions/folder2::handler.fn")
    _ = api.resources

    def check(_):
        pulumi_mocks.assert_res_counts(
            websocket_api_counts(
                function_count=2,
                route_count=2,
                integration_count=2,
                permission_count=2,
            )
        )

    when_websocket_api_ready(api, check)
