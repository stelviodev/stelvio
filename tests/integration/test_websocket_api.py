import asyncio

import websockets
from pytest import mark

from stelvio.aws.api_gateway import WebsocketApi
from stelvio.aws.function import Function

from .assert_helpers import (
    assert_lambda_function,
    assert_lambda_role_permissions,
    assert_websocket_api,
    assert_websocket_api_authorizers,
    assert_websocket_api_route_auth,
)
from .export_helpers import export_function, export_websocket_api

pytestmark = mark.integration


def test_websocket_api_connect(stelvio_env, project_dir):
    def infra():
        api = WebsocketApi("chat")
        api.route("$connect", "handlers/echo.main")
        export_websocket_api(api)

    outputs = stelvio_env.deploy(infra)
    url = outputs["websocket_api_chat_url"]
    assert url.startswith("wss://")
    assert_websocket_api(outputs["websocket_api_chat_id"], expected_route_keys={"$connect"})

    async def connect() -> None:
        async with websockets.connect(url):
            pass

    asyncio.run(connect())


def test_websocket_api_route_function_can_link_to_same_api(stelvio_env, project_dir):
    def infra():
        api = WebsocketApi("chat")
        function = Function("default", handler="handlers/echo.main", links=[api])
        api.route("$default", function)
        export_function(function)
        export_websocket_api(api)

    outputs = stelvio_env.deploy(infra)
    url = outputs["websocket_api_chat_url"]
    assert_websocket_api(outputs["websocket_api_chat_id"], expected_route_keys={"$default"})
    assert_lambda_function(
        outputs["function_default_arn"],
        environment={
            "STLV_CHAT_API_URL": outputs["websocket_api_chat_url"],
            "STLV_CHAT_API_EXECUTION_ARN": outputs["websocket_api_chat_execution_arn"],
        },
    )
    assert_lambda_role_permissions(
        outputs["function_default_role_name"],
        expected_actions=["execute-api:ManageConnections"],
    )

    async def connect() -> None:
        async with websockets.connect(url):
            pass

    asyncio.run(connect())


def test_websocket_api_lambda_authorizer(stelvio_env, project_dir):
    def infra():
        api = WebsocketApi("authchat")
        auth = api.add_lambda_authorizer(
            "token-auth",
            "handlers/websocket_auth.authorize",
            identity_sources=["route.request.querystring.token"],
        )
        api.route("$connect", "handlers/echo.main", auth=auth)
        export_websocket_api(api)

    outputs = stelvio_env.deploy(infra)
    api_id = outputs["websocket_api_authchat_id"]
    assert_websocket_api_authorizers(api_id, expected_types=["REQUEST"])
    assert_websocket_api_route_auth(api_id, route_key="$connect", auth_type="CUSTOM")

    async def connect() -> None:
        async with websockets.connect(outputs["websocket_api_authchat_url"] + "?token=allow"):
            pass

    asyncio.run(connect())
