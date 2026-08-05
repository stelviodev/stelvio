import asyncio
import json
import time

import websockets
from pytest import mark, raises
from websockets.exceptions import InvalidStatus

from stelvio.aws.api_gateway import WebsocketApi
from stelvio.aws.function import Function
from stelvio.component import ComponentRegistry

from .assert_helpers import (
    assert_apigatewayv2_tags,
    assert_lambda_function,
    assert_lambda_tags,
)
from .assert_websocket_api import (
    assert_lambda_role_policy_resources,
    assert_websocket_api,
    assert_websocket_api_authorizers,
    assert_websocket_api_integrations_share_uri,
    assert_websocket_api_route_auth,
)
from .export_helpers import export_function, export_websocket_api

pytestmark = mark.integration

# API Gateway WebSocket stages can briefly reject connections right after deploy.
_WEBSOCKET_API_DEPLOY_WAIT = 3


def _connect(url: str) -> None:
    async def run() -> None:
        async with websockets.connect(url):
            pass

    asyncio.run(run())


def _connect_and_exchange(url: str, message: dict) -> dict:
    async def run() -> dict:
        async with websockets.connect(url) as ws:
            await ws.send(json.dumps(message))
            reply = await asyncio.wait_for(ws.recv(), timeout=10)
            return json.loads(reply)

    return asyncio.run(run())


def test_websocket_api_connect(stelvio_env, project_dir):
    def infra():
        api = WebsocketApi("chat")
        api.route("$connect", "handlers/websocket_connect.main")
        export_websocket_api(api)

    outputs = stelvio_env.deploy(infra)
    url = outputs["websocket_api_chat_url"]
    assert url.startswith("wss://")
    assert_websocket_api(outputs["websocket_api_chat_id"], expected_route_keys={"$connect"})

    time.sleep(_WEBSOCKET_API_DEPLOY_WAIT)
    _connect(url)


def test_websocket_api_custom_stage_name(stelvio_env, project_dir):
    def infra():
        api = WebsocketApi("stagews", stage_name="prod")
        api.route("$connect", "handlers/websocket_connect.main")
        export_websocket_api(api)

    outputs = stelvio_env.deploy(infra)

    assert outputs["websocket_api_stagews_stage_name"] == "prod"
    assert outputs["websocket_api_stagews_url"].endswith("/prod")
    time.sleep(_WEBSOCKET_API_DEPLOY_WAIT)
    _connect(outputs["websocket_api_stagews_url"])


def test_websocket_api_multiple_routes(stelvio_env, project_dir):
    def infra():
        api = WebsocketApi("routes")
        api.route("$connect", "handlers/websocket_connect.main")
        api.route("$default", "handlers/echo.main")
        api.route("ping", "handlers/echo.main")
        export_websocket_api(api)

    outputs = stelvio_env.deploy(infra)
    assert_websocket_api(
        outputs["websocket_api_routes_id"],
        expected_route_keys={"$connect", "$default", "ping"},
        expected_integration_count=2,
    )

    time.sleep(_WEBSOCKET_API_DEPLOY_WAIT)
    _connect(outputs["websocket_api_routes_url"])


def test_websocket_api_shared_handler(stelvio_env, project_dir):
    def infra():
        fn = Function("sharedws", handler="handlers/websocket_connect.main")
        api = WebsocketApi("sharedwsapi")
        api.route("$connect", fn)
        api.route("$default", fn)
        api.route("ping", fn)
        export_function(fn)
        export_websocket_api(api)

    outputs = stelvio_env.deploy(infra)
    api_id = outputs["websocket_api_sharedwsapi_id"]
    function_arn = outputs["function_sharedws_arn"]

    assert_websocket_api(
        api_id,
        expected_route_keys={"$connect", "$default", "ping"},
        expected_integration_count=1,
    )
    assert_websocket_api_integrations_share_uri(
        api_id,
        expected_function_arn=function_arn,
    )


def test_websocket_api_tags_and_generated_function_tags(stelvio_env, project_dir):
    def infra():
        api = WebsocketApi("tagged-ws", tags={"Team": "platform"})
        api.route("$connect", "handlers/websocket_connect.main")
        export_websocket_api(api)
        fn = ComponentRegistry.get_component_by_name("tagged-ws-handlers-websocket_connect_main")
        export_function(fn)

    outputs = stelvio_env.deploy(infra)

    assert_apigatewayv2_tags(outputs["websocket_api_tagged-ws_arn"], {"Team": "platform"})
    assert_lambda_tags(
        outputs["function_tagged-ws-handlers-websocket_connect_main_arn"],
        {"Team": "platform"},
    )


def test_websocket_api_route_function_can_link_to_same_api(stelvio_env, project_dir):
    def infra():
        api = WebsocketApi("chat")
        function = Function(
            "default",
            handler="handlers/websocket_reply.main",
            links=[api],
        )
        api.route("$connect", "handlers/websocket_connect.main")
        api.route("ping", function)
        export_function(function)
        export_websocket_api(api)

    outputs = stelvio_env.deploy(infra)
    url = outputs["websocket_api_chat_url"]
    execution_arn = outputs["websocket_api_chat_execution_arn"]
    assert_websocket_api(
        outputs["websocket_api_chat_id"],
        expected_route_keys={"$connect", "ping"},
        expected_integration_count=2,
    )
    assert_lambda_function(
        outputs["function_default_arn"],
        environment={
            "STLV_CHAT_API_URL": outputs["websocket_api_chat_url"],
            "STLV_CHAT_API_EXECUTION_ARN": execution_arn,
        },
    )
    assert_lambda_role_policy_resources(
        outputs["function_default_role_name"],
        expected_actions=["execute-api:ManageConnections"],
        expected_resources=[f"{execution_arn}/*/@connections/*"],
    )

    time.sleep(_WEBSOCKET_API_DEPLOY_WAIT)
    reply = _connect_and_exchange(url, {"action": "ping", "n": 7})
    assert reply == {
        "routeKey": "ping",
        "body": {"action": "ping", "n": 7},
    }


def test_websocket_api_lambda_authorizer(stelvio_env, project_dir):
    def infra():
        api = WebsocketApi("authchat")
        auth = api.add_lambda_authorizer(
            "token-auth",
            "handlers/websocket_auth.authorize",
            identity_sources=["route.request.querystring.token"],
        )
        api.route("$connect", "handlers/websocket_connect.main", auth=auth)
        export_websocket_api(api)

    outputs = stelvio_env.deploy(infra)
    api_id = outputs["websocket_api_authchat_id"]
    base_url = outputs["websocket_api_authchat_url"]
    assert_websocket_api_authorizers(api_id, expected_types=["REQUEST"])
    assert_websocket_api_route_auth(api_id, route_key="$connect", auth_type="CUSTOM")

    time.sleep(_WEBSOCKET_API_DEPLOY_WAIT)

    with raises(InvalidStatus):
        _connect(base_url)

    with raises(InvalidStatus):
        _connect(f"{base_url}?token=deny")

    _connect(f"{base_url}?token=allow")


def test_websocket_api_iam_auth(stelvio_env, project_dir):
    def infra():
        api = WebsocketApi("iamws")
        api.route("$connect", "handlers/websocket_connect.main", auth="IAM")
        export_websocket_api(api)

    outputs = stelvio_env.deploy(infra)
    api_id = outputs["websocket_api_iamws_id"]
    assert_websocket_api_route_auth(api_id, route_key="$connect", auth_type="AWS_IAM")

    time.sleep(_WEBSOCKET_API_DEPLOY_WAIT)
    with raises(InvalidStatus):
        _connect(outputs["websocket_api_iamws_url"])


def test_websocket_api_custom_action_route_selection(stelvio_env, project_dir):
    """Send a framed message whose action selects a custom route (not $default)."""

    def infra():
        api = WebsocketApi("actions")
        reply = Function("reply", handler="handlers/websocket_reply.main", links=[api])
        api.route("$connect", "handlers/websocket_connect.main")
        api.route("$default", "handlers/echo.main")
        api.route("ping", reply)
        export_function(reply)
        export_websocket_api(api)

    outputs = stelvio_env.deploy(infra)
    assert_websocket_api(
        outputs["websocket_api_actions_id"],
        expected_route_keys={"$connect", "$default", "ping"},
        expected_integration_count=3,
    )

    time.sleep(_WEBSOCKET_API_DEPLOY_WAIT)
    reply = _connect_and_exchange(
        outputs["websocket_api_actions_url"],
        {"action": "ping", "payload": "hi"},
    )
    assert reply == {
        "routeKey": "ping",
        "body": {"action": "ping", "payload": "hi"},
    }
