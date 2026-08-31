"""Unit tests for WebsocketApi component (API Gateway v2 WebSocket).

Owns resource-graph, link, customize, and config coverage. Route and
authorizer behavior lives in the specialty modules.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import Any, Literal

import pulumi
from pytest import mark, raises

from stelvio.aws.api_gateway import (
    WebsocketApi,
    WebsocketApiConfig,
    WebsocketApiConfigDict,
)
from stelvio.aws.api_gateway.rest_api.constants import (
    API_GATEWAY_LOGS_POLICY,
    API_GATEWAY_ROLE_NAME,
)
from stelvio.aws.api_gateway.websocket_api.websocket_api import _ACCESS_LOG_FORMAT
from stelvio.aws.function import Function
from stelvio.config import AwsConfig
from stelvio.context import AppContext, _ContextStore
from tests.test_utils import assert_config_dict_matches_dataclass

from ...pulumi_mocks import ACCOUNT_ID, DEFAULT_REGION, R, tid, tn
from .conftest import (
    LAMBDA_INVOKE_ARN_TEMPLATE,
    TP,
    WEBSOCKET_API_ID,
    assert_lambda_role_and_attachment,
    websocket_api_counts,
    when_websocket_api_ready,
)

pytestmark = mark.usefixtures("project_cwd")

SIMPLE_FUNCTION = "chat-functions-simple_handler"
USERS_FUNCTION = "chat-functions-users_handler"
SIMPLE2_FUNCTION = "chat-functions-simple2_handler"

API_GATEWAY_ASSUME_ROLE_POLICY = [
    {
        "actions": ["sts:AssumeRole"],
        "principals": [{"identifiers": ["apigateway.amazonaws.com"], "type": "Service"}],
    }
]


@dataclass(frozen=True)
class RouteSpec:
    route_key: str
    handler: str
    route_name: str
    function_name: str


@dataclass(frozen=True)
class WebsocketApiTestCase:
    test_id: str
    routes: list[RouteSpec] = field(default_factory=list)
    functions: list[str] = field(default_factory=list)
    stage_name: str = "$default"
    route_selection_expression: str = "$request.body.action"
    access_log_retention_days: int | Literal["forever"] = 30


CONNECT_TC = WebsocketApiTestCase(
    test_id="connect",
    routes=[
        RouteSpec(
            "$connect",
            "functions/simple.handler",
            "chat-route-sys-connect",
            SIMPLE_FUNCTION,
        )
    ],
    functions=[SIMPLE_FUNCTION],
)
SHARED_HANDLER_TC = WebsocketApiTestCase(
    test_id="shared-handler",
    routes=[
        RouteSpec(
            "$connect",
            "functions/simple.handler",
            "chat-route-sys-connect",
            SIMPLE_FUNCTION,
        ),
        RouteSpec(
            "$disconnect",
            "functions/simple.handler",
            "chat-route-sys-disconnect",
            SIMPLE_FUNCTION,
        ),
    ],
    functions=[SIMPLE_FUNCTION],
)
DEFAULT_AND_CUSTOM_TC = WebsocketApiTestCase(
    test_id="default-and-custom",
    routes=[
        RouteSpec(
            "$connect",
            "functions/simple.handler",
            "chat-route-sys-connect",
            SIMPLE_FUNCTION,
        ),
        RouteSpec(
            "$default",
            "functions/simple2.handler",
            "chat-route-sys-default",
            SIMPLE2_FUNCTION,
        ),
        RouteSpec(
            "sendMessage",
            "functions/users.handler",
            "chat-route-sendMessage",
            USERS_FUNCTION,
        ),
    ],
    functions=[SIMPLE_FUNCTION, SIMPLE2_FUNCTION, USERS_FUNCTION],
)
CUSTOM_RETENTION_TC = replace(
    CONNECT_TC,
    test_id="custom-retention",
    access_log_retention_days=3653,
)
FOREVER_RETENTION_TC = replace(
    CONNECT_TC,
    test_id="retention-forever",
    access_log_retention_days="forever",
)
NAMED_STAGE_TC = replace(CONNECT_TC, test_id="named-stage", stage_name="v2")
CUSTOM_ROUTE_SELECTION_TC = replace(
    CONNECT_TC,
    test_id="custom-route-selection",
    route_selection_expression="$request.body.type",
)
WEBSOCKET_API_CASES = [
    CONNECT_TC,
    SHARED_HANDLER_TC,
    DEFAULT_AND_CUSTOM_TC,
    CUSTOM_RETENTION_TC,
    FOREVER_RETENTION_TC,
    NAMED_STAGE_TC,
    CUSTOM_ROUTE_SELECTION_TC,
]


def verify_websocket_api(mocks, case: WebsocketApiTestCase) -> None:
    api_id = WEBSOCKET_API_ID
    mocks.assert_res(
        "chat",
        R.HTTP_API,
        {
            "protocolType": "WEBSOCKET",
            "routeSelectionExpression": case.route_selection_expression,
            "disableExecuteApiEndpoint": False,
        },
    )
    mocks.assert_res(
        "StelvioAPIGatewayPushToCloudWatchLogsRole",
        R.ROLE,
        {
            "managedPolicyArns": [API_GATEWAY_LOGS_POLICY],
            "assumeRolePolicy": json.dumps(API_GATEWAY_ASSUME_ROLE_POLICY),
        },
        prefixed=False,
    )
    mocks.assert_res("api-gateway-account-ref", R.API_ACCOUNT, {}, prefixed=False)
    mocks.assert_res(
        "api-gateway-account",
        R.API_ACCOUNT,
        {
            "cloudwatchRoleArn": (
                f"arn:aws:iam::{ACCOUNT_ID}:role/{API_GATEWAY_ROLE_NAME}-test-name"
            )
        },
        prefixed=False,
    )

    log_group_inputs: dict[str, Any] = {"name": f"/aws/apigateway/{api_id}"}
    if case.access_log_retention_days != "forever":
        log_group_inputs["retentionInDays"] = float(case.access_log_retention_days)
    mocks.assert_res("chat-logs", R.LOG_GROUP, log_group_inputs)
    mocks.assert_res(
        "chat-stage",
        R.HTTP_API_STAGE,
        {
            "name": case.stage_name,
            "autoDeploy": True,
            "apiId": api_id,
            "accessLogSettings": {
                "format": _ACCESS_LOG_FORMAT,
                "destinationArn": (
                    f"arn:aws:logs:{DEFAULT_REGION}:{ACCOUNT_ID}:log-group:"
                    f"{tn(TP + 'chat-logs')}:*"
                ),
            },
        },
    )

    for function_name in case.functions:
        handler_value = next(
            r.handler.removeprefix("functions/")
            for r in case.routes
            if r.function_name == function_name
        )
        mocks.assert_res(
            function_name,
            R.FUNCTION,
            # Lambda Function inputs include code/role assets — assert configured fields.
            {"handler": handler_value, "memorySize": 128.0, "timeout": 60.0},
            partial=True,
        )
        assert_lambda_role_and_attachment(mocks, function_name)
        mocks.assert_res(
            f"chat-integration-{function_name}",
            R.HTTP_API_INTEGRATION,
            {
                "integrationType": "AWS_PROXY",
                "integrationMethod": "POST",
                "integrationUri": LAMBDA_INVOKE_ARN_TEMPLATE.format(
                    function_name=tn(TP + function_name)
                ),
                "apiId": api_id,
            },
        )
        mocks.assert_res(
            f"chat-permission-{function_name}",
            R.LAMBDA_PERMISSION,
            {
                "action": "lambda:InvokeFunction",
                "function": tn(TP + function_name),
                "principal": "apigateway.amazonaws.com",
                "sourceArn": (f"arn:aws:execute-api:{DEFAULT_REGION}:{ACCOUNT_ID}:{api_id}/*/*"),
            },
        )

    for route in case.routes:
        mocks.assert_res(
            route.route_name,
            R.HTTP_API_ROUTE,
            {
                "apiId": api_id,
                "routeKey": route.route_key,
                "target": f"integrations/{tid(TP + f'chat-integration-{route.function_name}')}",
            },
        )

    mocks.assert_res_counts(
        websocket_api_counts(
            function_count=len(case.functions),
            route_count=len(case.routes),
            integration_count=len(case.functions),
            permission_count=len(case.functions),
        )
    )


def test_websocket_api_config_dict_matches_websocket_api_config():
    assert_config_dict_matches_dataclass(WebsocketApiConfig, WebsocketApiConfigDict)


def test_websocket_api_rejects_invalid_config_type():
    with raises(TypeError, match="Invalid config type"):
        WebsocketApi("chat", config=123)  # type: ignore[arg-type]


@mark.parametrize(
    ("action", "expected_error"),
    [
        (
            lambda: WebsocketApi(
                "chat",
                config=WebsocketApiConfig(),
                domain_name="chat.example.com",
            ),
            "cannot combine",
        ),
        (
            lambda: WebsocketApiConfig(domain_name="", domain=None),
            "Domain name cannot be empty",
        ),
        (lambda: WebsocketApi("chat", access_log_retention_days=999), "access_log_retention_days"),
        (lambda: WebsocketApi("chat", stage_name="$bad"), "Stage name"),
        (
            lambda: WebsocketApi("chat", stage_name="with spaces"),
            "Stage name must contain only",
        ),
        (
            lambda: WebsocketApi("chat", stage_name="x" * 129),
            "Stage name must be at most 128 characters",
        ),
        (
            lambda: WebsocketApi("chat", route_selection_expression=""),
            "route_selection_expression cannot be empty",
        ),
    ],
    ids=[
        "config_and_opts",
        "empty_domain",
        "invalid_retention",
        "invalid_stage_name",
        "stage_name_spaces",
        "stage_name_too_long",
        "empty_route_selection",
    ],
)
def test_websocket_api_rejects_invalid_configuration(action, expected_error):
    with raises((ValueError, TypeError), match=expected_error):
        action()


@mark.parametrize("case", WEBSOCKET_API_CASES, ids=lambda case: case.test_id)
@pulumi.runtime.test
def test_websocket_api_resource_graph(pulumi_mocks, case):
    api = WebsocketApi(
        "chat",
        stage_name=case.stage_name,
        route_selection_expression=case.route_selection_expression,
        access_log_retention_days=case.access_log_retention_days,
    )
    for route in case.routes:
        api.route(route.route_key, route.handler)
    _ = api.resources

    def check(_):
        verify_websocket_api(pulumi_mocks, case)

    when_websocket_api_ready(api, check)


@pulumi.runtime.test
def test_websocket_api_rejects_empty_routes(pulumi_mocks):
    api = WebsocketApi("chat")

    with raises(ValueError, match="has no routes"):
        _ = api.resources


@pulumi.runtime.test
def test_websocket_api_arn_and_execution_arn_properties(pulumi_mocks):
    api = WebsocketApi("chat")
    arn, execution_arn, api_id = api.arn, api.execution_arn, api.api_id
    api.route("$connect", "functions/simple.handler")

    def check(values):
        resolved_arn, resolved_execution_arn, resolved_api_id = values
        assert resolved_arn == f"arn:aws:apigateway:{DEFAULT_REGION}::/apis/{WEBSOCKET_API_ID}"
        assert (
            resolved_execution_arn
            == f"arn:aws:execute-api:{DEFAULT_REGION}:{ACCOUNT_ID}:{WEBSOCKET_API_ID}"
        )
        assert resolved_api_id == WEBSOCKET_API_ID

    pulumi.Output.all(arn, execution_arn, api_id).apply(check)


@pulumi.runtime.test
def test_websocket_api_url_default_stage_execute_api(pulumi_mocks):
    api = WebsocketApi("chat")
    api.route("$connect", "functions/simple.handler")

    def check(url):
        assert (
            url == f"wss://{WEBSOCKET_API_ID}.execute-api.{DEFAULT_REGION}.amazonaws.com/$default"
        )

    api.url.apply(check)


@pulumi.runtime.test
def test_websocket_api_url_named_stage_execute_api(pulumi_mocks):
    api = WebsocketApi("chat", stage_name="prod")
    api.route("$connect", "functions/simple.handler")

    def check(url):
        assert url == f"wss://{WEBSOCKET_API_ID}.execute-api.{DEFAULT_REGION}.amazonaws.com/prod"

    api.url.apply(check)


@pulumi.runtime.test
def test_websocket_api_url_uses_customized_stage_name(pulumi_mocks):
    api = WebsocketApi("chat", customize={"stage": {"name": "prod"}})
    url = api.url
    api.route("$connect", "functions/simple.handler")
    _ = api.resources

    def check(resolved):
        assert resolved == (
            f"wss://{WEBSOCKET_API_ID}.execute-api.{DEFAULT_REGION}.amazonaws.com/prod"
        )

    url.apply(check)


@pulumi.runtime.test
def test_websocket_api_url_uses_context_aws_region(pulumi_mocks):
    saved = _ContextStore.get()
    try:
        _ContextStore.clear()
        _ContextStore.set(
            AppContext(
                name="test",
                env="test",
                aws=AwsConfig(profile="default", region="eu-west-1"),
                home="aws",
            )
        )
        api = WebsocketApi("chat")
        api.route("$connect", "functions/simple.handler")

        def check(url):
            assert url == f"wss://{WEBSOCKET_API_ID}.execute-api.eu-west-1.amazonaws.com/$default"

        api.url.apply(check)
    finally:
        _ContextStore.clear()
        _ContextStore.set(saved)


@pulumi.runtime.test
def test_websocket_api_link_grants_manage_connections(pulumi_mocks):
    api = WebsocketApi("chat")
    api.route("$connect", "functions/simple.handler")
    _ = api.resources
    link = api.link()

    def check(args):
        properties, permissions = args
        assert set(properties) == {"api_url", "api_execution_arn"}
        assert len(permissions) == 1
        permission = permissions[0]
        assert permission.actions == ["execute-api:ManageConnections"]

        def check_resource(resource):
            assert resource == (
                f"arn:aws:execute-api:{DEFAULT_REGION}:{ACCOUNT_ID}:"
                f"{WEBSOCKET_API_ID}/*/@connections/*"
            )

        return permission.resources[0].apply(check_resource)

    return pulumi.Output.all(link.properties, link.permissions).apply(check)


@pulumi.runtime.test
def test_websocket_api_link_injects_api_url_env_vars(pulumi_mocks):
    api = WebsocketApi("chat")
    api.route("$connect", "functions/simple.handler")
    fn = Function("client", handler="functions/simple.handler", links=[api])
    expected_execution_arn = (
        f"arn:aws:execute-api:{DEFAULT_REGION}:{ACCOUNT_ID}:{WEBSOCKET_API_ID}"
    )

    def check(api_properties):
        # Lambda Function inputs include code/role assets — assert link env vars.
        pulumi_mocks.assert_res(
            "client",
            R.FUNCTION,
            {
                "environment": {
                    "variables": {
                        "STLV_CHAT_API_URL": api_properties[0],
                        "STLV_CHAT_API_EXECUTION_ARN": api_properties[1],
                    }
                }
            },
            partial=True,
        )
        pulumi_mocks.assert_res(
            "client-p",
            R.POLICY,
            {
                "path": "/",
                "policy": json.dumps(
                    [
                        {
                            "actions": ["execute-api:ManageConnections"],
                            "resources": [f"{expected_execution_arn}/*/@connections/*"],
                        }
                    ]
                ),
            },
        )

    pulumi.Output.all(api.url, api.execution_arn, fn.resources.function.id).apply(check)


@pulumi.runtime.test
def test_websocket_api_route_function_can_link_to_same_api(pulumi_mocks):
    api = WebsocketApi("chat")
    function = Function("default", handler="functions/simple.handler", links=[api])
    api.route("$default", function)
    _ = api.resources
    expected_url = f"wss://{WEBSOCKET_API_ID}.execute-api.{DEFAULT_REGION}.amazonaws.com/$default"
    expected_execution_arn = (
        f"arn:aws:execute-api:{DEFAULT_REGION}:{ACCOUNT_ID}:{WEBSOCKET_API_ID}"
    )

    def check(_):
        pulumi_mocks.assert_res_counts(
            websocket_api_counts(
                function_count=1,
                route_count=1,
                integration_count=1,
                permission_count=1,
                policy_count=1,
            )
        )
        pulumi_mocks.assert_res(
            "chat-route-sys-default",
            R.HTTP_API_ROUTE,
            {
                "apiId": WEBSOCKET_API_ID,
                "routeKey": "$default",
                "target": f"integrations/{tid(TP + 'chat-integration-chat-default')}",
            },
        )
        pulumi_mocks.assert_res(
            "default",
            R.FUNCTION,
            {
                "environment": {
                    "variables": {
                        "STLV_CHAT_API_URL": expected_url,
                        "STLV_CHAT_API_EXECUTION_ARN": expected_execution_arn,
                    }
                }
            },
            partial=True,
        )
        pulumi_mocks.assert_res(
            "default-p",
            R.POLICY,
            {
                "path": "/",
                "policy": json.dumps(
                    [
                        {
                            "actions": ["execute-api:ManageConnections"],
                            "resources": [f"{expected_execution_arn}/*/@connections/*"],
                        }
                    ]
                ),
            },
        )

    when_websocket_api_ready(api, check)
    return function.resources.function.id.apply(lambda _: None)


@pulumi.runtime.test
def test_websocket_api_customize_applies_to_resources(pulumi_mocks, app_context_with_dns):
    api = WebsocketApi(
        "chat",
        domain_name="chat.example.com",
        customize={
            "api": {"description": "Custom WebSocket API"},
            "stage": {"description": "Custom stage"},
            "log_group": {"retention_in_days": 90},
            "api_mapping": {"api_mapping_key": "custom"},
        },
    )
    api.route("$connect", "functions/simple.handler")
    _ = api.resources

    def check(_):
        pulumi_mocks.assert_res(
            "chat",
            R.HTTP_API,
            {
                "protocolType": "WEBSOCKET",
                "routeSelectionExpression": "$request.body.action",
                "disableExecuteApiEndpoint": False,
                "description": "Custom WebSocket API",
            },
        )
        pulumi_mocks.assert_res(
            "chat-stage",
            R.HTTP_API_STAGE,
            {
                "name": "$default",
                "autoDeploy": True,
                "apiId": WEBSOCKET_API_ID,
                "description": "Custom stage",
                "accessLogSettings": {
                    "format": _ACCESS_LOG_FORMAT,
                    "destinationArn": (
                        f"arn:aws:logs:{DEFAULT_REGION}:{ACCOUNT_ID}:log-group:"
                        f"{tn(TP + 'chat-logs')}:*"
                    ),
                },
            },
        )
        pulumi_mocks.assert_res(
            "chat-logs",
            R.LOG_GROUP,
            {
                "name": f"/aws/apigateway/{WEBSOCKET_API_ID}",
                "retentionInDays": 90.0,
            },
        )
        pulumi_mocks.assert_res(
            "chat-api-mapping",
            R.HTTP_API_MAPPING,
            {
                "apiId": WEBSOCKET_API_ID,
                "domainName": "chat.example.com",
                "stage": tid(TP + "chat-stage"),
                "apiMappingKey": "custom",
            },
        )

    when_websocket_api_ready(api, check)


@pulumi.runtime.test
def test_multiple_websocket_apis_with_same_routes_coexist_with_unique_resource_names(
    pulumi_mocks,
):
    api1 = WebsocketApi("chat-api")
    api1.route("$connect", "functions/simple.handler")
    api1.route("sendMessage", "functions/users.handler")

    api2 = WebsocketApi("admin-chat")
    api2.route("$connect", "functions/simple.handler")
    api2.route("sendMessage", "functions/users.handler")

    def expected_names(api_slug: str) -> dict[str, set[str]]:
        return {
            "routes": {
                f"{api_slug}-route-sys-connect",
                f"{api_slug}-route-sendMessage",
            },
            "integrations": {
                f"{api_slug}-integration-{api_slug}-functions-simple_handler",
                f"{api_slug}-integration-{api_slug}-functions-users_handler",
            },
            "functions": {
                f"{api_slug}-functions-simple_handler",
                f"{api_slug}-functions-users_handler",
            },
            "permissions": {
                f"{api_slug}-permission-{api_slug}-functions-simple_handler",
                f"{api_slug}-permission-{api_slug}-functions-users_handler",
            },
        }

    def check(_):
        # CloudWatch account/role are shared across APIs; log groups are per API.
        pulumi_mocks.assert_res_counts(
            {
                R.HTTP_API: 2,
                R.HTTP_API_STAGE: 2,
                R.API_ACCOUNT: 2,
                R.LOG_GROUP: 2,
                R.ROLE: 5,  # 4 function roles + 1 shared CloudWatch role
                R.FUNCTION: 4,
                R.ROLE_POLICY_ATTACHMENT: 4,
                R.HTTP_API_INTEGRATION: 4,
                R.LAMBDA_PERMISSION: 4,
                R.HTTP_API_ROUTE: 4,
            }
        )

        pulumi_mocks.assert_res("chat-api", R.HTTP_API)
        pulumi_mocks.assert_res("admin-chat", R.HTTP_API)

        chat = expected_names("chat-api")
        admin = expected_names("admin-chat")
        for name in chat["routes"] | admin["routes"]:
            pulumi_mocks.assert_res(name, R.HTTP_API_ROUTE)
        for name in chat["integrations"] | admin["integrations"]:
            pulumi_mocks.assert_res(name, R.HTTP_API_INTEGRATION)
        for name in chat["functions"] | admin["functions"]:
            pulumi_mocks.assert_res(name, R.FUNCTION)
        for name in chat["permissions"] | admin["permissions"]:
            pulumi_mocks.assert_res(name, R.LAMBDA_PERMISSION)

    when_websocket_api_ready([api1, api2], check)
