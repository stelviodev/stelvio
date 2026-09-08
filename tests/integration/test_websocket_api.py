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
    assert_apigatewayv2_authorizers,
    assert_apigatewayv2_integrations_share_uri,
    assert_apigatewayv2_route_auth,
    assert_apigatewayv2_tags,
    assert_lambda_function,
    assert_lambda_role_permissions,
    assert_lambda_tags,
)
from .assert_websocket_api import (
    assert_websocket_api,
    websocket_connect,
    websocket_execute_api_url,
)
from .export_helpers import export_function, export_websocket_api

pytestmark = mark.integration

# API Gateway WebSocket stages can briefly reject connections right after deploy.
_WEBSOCKET_API_DEPLOY_WAIT = 3


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
    api_id = outputs["websocket_api_chat_id"]
    url = outputs["websocket_api_chat_url"]
    assert url == websocket_execute_api_url(
        "wss", api_id=api_id, region=stelvio_env.aws_region, stage="$default"
    )
    assert_websocket_api(api_id, expected_route_keys={"$connect"})

    time.sleep(_WEBSOCKET_API_DEPLOY_WAIT)
    websocket_connect(url)


def test_websocket_api_custom_stage_name(stelvio_env, project_dir):
    def infra():
        api = WebsocketApi("stagews", stage_name="prod")
        api.route("$connect", "handlers/websocket_connect.main")
        export_websocket_api(api)

    outputs = stelvio_env.deploy(infra)
    api_id = outputs["websocket_api_stagews_id"]
    url = outputs["websocket_api_stagews_url"]
    assert url == websocket_execute_api_url(
        "wss", api_id=api_id, region=stelvio_env.aws_region, stage="prod"
    )
    assert_websocket_api(
        api_id,
        expected_route_keys={"$connect"},
        expected_stage_name="prod",
    )

    time.sleep(_WEBSOCKET_API_DEPLOY_WAIT)
    websocket_connect(url)


def test_websocket_api_shared_handler(stelvio_env, project_dir):
    def infra():
        api = WebsocketApi("sharedwsapi")
        fn = Function("sharedws", handler="handlers/websocket_reply.main", links=[api])
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
    assert_apigatewayv2_integrations_share_uri(
        api_id,
        expected_function_arn=function_arn,
    )

    time.sleep(_WEBSOCKET_API_DEPLOY_WAIT)
    url = outputs["websocket_api_sharedwsapi_url"]
    ping = _connect_and_exchange(url, {"action": "ping"})
    assert ping["routeKey"] == "ping"
    other = _connect_and_exchange(url, {"action": "other"})
    assert other["routeKey"] == "$default"


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
    api_id = outputs["websocket_api_chat_id"]
    url = outputs["websocket_api_chat_url"]
    execution_arn = outputs["websocket_api_chat_execution_arn"]
    region = stelvio_env.aws_region
    management_url = websocket_execute_api_url(
        "https", api_id=api_id, region=region, stage="$default"
    )
    assert url == websocket_execute_api_url("wss", api_id=api_id, region=region, stage="$default")
    assert_websocket_api(
        api_id,
        expected_route_keys={"$connect", "ping"},
        expected_integration_count=2,
    )
    assert_lambda_function(
        outputs["function_default_arn"],
        environment={
            "STLV_CHAT_API_URL": url,
            "STLV_CHAT_API_EXECUTION_ARN": execution_arn,
            "STLV_CHAT_API_MANAGEMENT_URL": management_url,
        },
    )
    assert_lambda_role_permissions(
        outputs["function_default_role_name"],
        expected_actions=["execute-api:ManageConnections"],
        expected_resources=[f"{execution_arn}/*/*/@connections/*"],
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
    assert_apigatewayv2_authorizers(api_id, expected_types=["REQUEST"])
    assert_apigatewayv2_route_auth(api_id, route_key="$connect", auth_type="CUSTOM")

    time.sleep(_WEBSOCKET_API_DEPLOY_WAIT)

    with raises(InvalidStatus) as no_token:
        websocket_connect(base_url)
    assert no_token.value.response.status_code == 401

    with raises(InvalidStatus) as denied:
        websocket_connect(f"{base_url}?token=deny")
    assert denied.value.response.status_code == 403

    websocket_connect(f"{base_url}?token=allow")


def test_websocket_api_iam_auth(stelvio_env, project_dir):
    def infra():
        api = WebsocketApi("iamws")
        api.route("$connect", "handlers/websocket_connect.main", auth="IAM")
        export_websocket_api(api)

    outputs = stelvio_env.deploy(infra)
    api_id = outputs["websocket_api_iamws_id"]
    assert_apigatewayv2_route_auth(api_id, route_key="$connect", auth_type="AWS_IAM")

    time.sleep(_WEBSOCKET_API_DEPLOY_WAIT)
    with raises(InvalidStatus) as unsigned:
        websocket_connect(outputs["websocket_api_iamws_url"])
    assert unsigned.value.response.status_code == 403


def test_websocket_api_custom_action_route_selection(stelvio_env, project_dir):
    """Custom route_selection_expression: ping hits ping, unknown hits $default."""

    def infra():
        api = WebsocketApi("actions", route_selection_expression="$request.body.route")
        reply = Function("reply", handler="handlers/websocket_reply.main", links=[api])
        api.route("$connect", "handlers/websocket_connect.main")
        api.route("$default", reply)
        api.route("ping", reply)
        export_function(reply)
        export_websocket_api(api)

    outputs = stelvio_env.deploy(infra)
    assert_websocket_api(
        outputs["websocket_api_actions_id"],
        expected_route_keys={"$connect", "$default", "ping"},
        expected_integration_count=2,
        route_selection_expression="$request.body.route",
    )

    time.sleep(_WEBSOCKET_API_DEPLOY_WAIT)
    url = outputs["websocket_api_actions_url"]
    ping = _connect_and_exchange(url, {"route": "ping", "payload": "hi"})
    assert ping == {
        "routeKey": "ping",
        "body": {"route": "ping", "payload": "hi"},
    }
    other = _connect_and_exchange(url, {"route": "other"})
    assert other == {
        "routeKey": "$default",
        "body": {"route": "other"},
    }
