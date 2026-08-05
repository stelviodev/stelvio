"""Tests for WebsocketApi authorizers."""

import pulumi
from pytest import mark, raises

from stelvio.aws.api_gateway.websocket_api import WebsocketApi
from stelvio.aws.function import Function, FunctionConfig

from ...pulumi_mocks import ACCOUNT_ID, DEFAULT_REGION, R, tid, tn
from .conftest import (
    LAMBDA_INVOKE_ARN_TEMPLATE,
    TP,
    WEBSOCKET_API_ID,
    websocket_api_counts,
    when_websocket_api_ready,
)

pytestmark = mark.usefixtures("project_cwd")


def assert_lambda_authorizer_graph(mocks) -> None:
    mocks.assert_res(
        "chat-authorizer-jwt-auth",
        R.HTTP_API_AUTHORIZER,
        {
            "apiId": WEBSOCKET_API_ID,
            "authorizerType": "REQUEST",
            "authorizerUri": LAMBDA_INVOKE_ARN_TEMPLATE.format(
                function_name=tn(TP + "chat-auth-jwt-auth")
            ),
            "identitySources": ["route.request.header.Authorization"],
            "name": "jwt-auth",
        },
    )
    mocks.assert_res(
        "chat-auth-permission-jwt-auth",
        R.LAMBDA_PERMISSION,
        {
            "action": "lambda:InvokeFunction",
            "function": tn(TP + "chat-auth-jwt-auth"),
            "principal": "apigateway.amazonaws.com",
            "sourceArn": (
                f"arn:aws:execute-api:{DEFAULT_REGION}:{ACCOUNT_ID}:{WEBSOCKET_API_ID}"
                f"/authorizers/{tid(TP + 'chat-authorizer-jwt-auth')}"
            ),
        },
    )
    mocks.assert_res(
        "chat-route-sys-connect",
        R.HTTP_API_ROUTE,
        {
            "apiId": WEBSOCKET_API_ID,
            "routeKey": "$connect",
            "target": (
                f"integrations/{tid(TP + 'chat-integration-chat-functions-simple_handler')}"
            ),
            "authorizationType": "CUSTOM",
            "authorizerId": tid(TP + "chat-authorizer-jwt-auth"),
        },
    )
    mocks.assert_res_counts(
        websocket_api_counts(
            function_count=2,
            route_count=1,
            integration_count=1,
            permission_count=2,
            authorizer_count=1,
        )
    )


@pulumi.runtime.test
def test_websocket_api_lambda_authorizer_protects_connect(pulumi_mocks):
    api = WebsocketApi("chat")
    auth = api.add_lambda_authorizer(
        "jwt-auth",
        "functions/users.handler",
        identity_sources=["route.request.header.Authorization"],
    )
    api.route("$connect", "functions/simple.handler", auth=auth)
    _ = api.resources

    def check(_):
        assert_lambda_authorizer_graph(pulumi_mocks)

    when_websocket_api_ready(api, check)


@pulumi.runtime.test
def test_websocket_api_iam_authorizer_does_not_create_lambda_authorizer(pulumi_mocks):
    api = WebsocketApi("chat")
    api.route("$connect", "functions/simple.handler", auth="IAM")
    _ = api.resources

    def check(_):
        pulumi_mocks.assert_res(
            "chat-route-sys-connect",
            R.HTTP_API_ROUTE,
            {
                "apiId": WEBSOCKET_API_ID,
                "routeKey": "$connect",
                "target": (
                    f"integrations/{tid(TP + 'chat-integration-chat-functions-simple_handler')}"
                ),
                "authorizationType": "AWS_IAM",
            },
        )
        pulumi_mocks.assert_no_res(R.HTTP_API_AUTHORIZER)
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
    "auth",
    [
        "IAM",
        "lambda",
    ],
    ids=["iam", "lambda"],
)
def test_websocket_api_rejects_auth_on_non_connect_routes(auth):
    api = WebsocketApi("chat")
    if auth == "lambda":
        auth_value = api.add_lambda_authorizer(
            "jwt-auth",
            "functions/users.handler",
            identity_sources=["route.request.querystring.token"],
        )
    else:
        auth_value = "IAM"

    with raises(ValueError, match=r"only be configured on the '\$connect' route"):
        api.route("$default", "functions/simple.handler", auth=auth_value)


def test_websocket_api_rejects_authorizer_from_another_api():
    other_api = WebsocketApi("other")
    auth = other_api.add_lambda_authorizer(
        "jwt-auth",
        "functions/users.handler",
        identity_sources=["route.request.header.Authorization"],
    )
    api = WebsocketApi("chat")

    with raises(ValueError, match="belongs to a different WebsocketApi"):
        api.route("$connect", "functions/simple.handler", auth=auth)


@mark.parametrize(
    ("identity_sources", "expected_error"),
    [
        ([], "non-empty list of identity_sources"),
        (["route.request.header.Authorization", ""], "non-empty strings"),
        ("route.request.header.Authorization", "non-empty list of identity_sources"),
    ],
    ids=["empty_list", "empty_string_entry", "not_a_list"],
)
def test_websocket_api_rejects_invalid_identity_sources(identity_sources, expected_error):
    api = WebsocketApi("chat")

    with raises(ValueError, match=expected_error):
        api.add_lambda_authorizer(
            "jwt-auth",
            "functions/users.handler",
            identity_sources=identity_sources,  # type: ignore[arg-type]
        )


def test_websocket_api_rejects_duplicate_authorizer_name():
    api = WebsocketApi("chat")
    api.add_lambda_authorizer(
        "jwt-auth",
        "functions/users.handler",
        identity_sources=["route.request.header.Authorization"],
    )

    with raises(ValueError, match="Duplicate authorizer name"):
        api.add_lambda_authorizer(
            "jwt-auth",
            "functions/simple.handler",
            identity_sources=["route.request.header.Authorization"],
        )


def test_websocket_api_rejects_unsupported_auth_type():
    api = WebsocketApi("chat")

    with raises(TypeError, match="Unsupported auth type"):
        api.route("$connect", "functions/simple.handler", auth="JWT")  # type: ignore[arg-type]


@pulumi.runtime.test
def test_websocket_api_lambda_authorizer_uses_supplied_function(pulumi_mocks):
    function = Function("auth-fn", handler="functions/users.handler")
    api = WebsocketApi("chat")
    auth = api.add_lambda_authorizer(
        "jwt-auth",
        function,
        identity_sources=["route.request.header.Authorization"],
    )
    api.route("$connect", "functions/simple.handler", auth=auth)
    _ = api.resources

    def check(_):
        pulumi_mocks.assert_res(
            "chat-authorizer-jwt-auth",
            R.HTTP_API_AUTHORIZER,
            {
                "apiId": WEBSOCKET_API_ID,
                "authorizerType": "REQUEST",
                "authorizerUri": LAMBDA_INVOKE_ARN_TEMPLATE.format(
                    function_name=tn(TP + "auth-fn")
                ),
                "identitySources": ["route.request.header.Authorization"],
                "name": "jwt-auth",
            },
        )
        pulumi_mocks.assert_res(
            "chat-auth-permission-jwt-auth",
            R.LAMBDA_PERMISSION,
            {
                "action": "lambda:InvokeFunction",
                "function": tn(TP + "auth-fn"),
                "principal": "apigateway.amazonaws.com",
                "sourceArn": (
                    f"arn:aws:execute-api:{DEFAULT_REGION}:{ACCOUNT_ID}:{WEBSOCKET_API_ID}"
                    f"/authorizers/{tid(TP + 'chat-authorizer-jwt-auth')}"
                ),
            },
        )
        pulumi_mocks.assert_res_counts(
            websocket_api_counts(
                function_count=2,
                route_count=1,
                integration_count=1,
                permission_count=2,
                authorizer_count=1,
            )
        )

    when_websocket_api_ready(api, check)


def test_websocket_api_lambda_authorizer_rejects_function_with_opts():
    function = Function("auth-fn", handler="functions/users.handler")
    api = WebsocketApi("chat")

    with raises(ValueError, match="Cannot combine a Function handler"):
        api.add_lambda_authorizer(
            "jwt-auth",
            function,
            identity_sources=["route.request.header.Authorization"],
            memory=256,
        )


@mark.parametrize(
    ("handler", "opts"),
    [
        ("functions/users.handler", {"memory": 256}),
        (FunctionConfig(handler="functions/users.handler", memory=256), {}),
        ({"handler": "functions/users.handler", "memory": 256}, {}),
    ],
    ids=["string", "function_config", "dict"],
)
@pulumi.runtime.test
def test_websocket_api_lambda_authorizer_handler_forms(pulumi_mocks, handler, opts):
    api = WebsocketApi("chat")
    auth = api.add_lambda_authorizer(
        "jwt-auth",
        handler,
        identity_sources=["route.request.header.Authorization"],
        **opts,
    )
    api.route("$connect", "functions/simple.handler", auth=auth)
    _ = api.resources

    def check(_):
        # Lambda Function inputs include code/role assets — assert the configured fields.
        pulumi_mocks.assert_res(
            "chat-auth-jwt-auth",
            R.FUNCTION,
            {"handler": "users.handler", "memorySize": 256.0},
            partial=True,
        )
        pulumi_mocks.assert_res_counts(
            websocket_api_counts(
                function_count=2,
                route_count=1,
                integration_count=1,
                permission_count=2,
                authorizer_count=1,
            )
        )

    when_websocket_api_ready(api, check)


@pulumi.runtime.test
def test_websocket_api_rejects_authorizer_after_resources_created(pulumi_mocks):
    api = WebsocketApi("chat")
    api.route("$connect", "functions/simple.handler")
    _ = api.resources

    with raises(RuntimeError, match="after resources have been created"):
        api.add_lambda_authorizer(
            "jwt-auth",
            "functions/users.handler",
            identity_sources=["route.request.header.Authorization"],
        )


@pulumi.runtime.test
def test_websocket_api_rejects_unused_authorizers(pulumi_mocks):
    api = WebsocketApi("chat")
    api.add_lambda_authorizer(
        "jwt-auth",
        "functions/users.handler",
        identity_sources=["route.request.header.Authorization"],
    )
    api.route("$connect", "functions/simple.handler")

    with raises(ValueError, match=r"unused authorizer\(s\): 'jwt-auth'"):
        _ = api.resources
