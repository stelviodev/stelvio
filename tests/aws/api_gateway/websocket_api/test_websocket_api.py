"""Unit tests for WebsocketApi component (API Gateway v2 WebSocket).

Owns resource-graph, link, customize, and config coverage. Route and
authorizer behavior lives in the specialty modules.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import Any, Literal

import pulumi
from pytest import mark, param, raises

from stelvio.aws.api_gateway import (
    ApiDomain,
    WebsocketApi,
    WebsocketApiConfig,
    WebsocketApiConfigDict,
)
from stelvio.aws.api_gateway.rest_api.constants import (
    API_GATEWAY_LOGS_POLICY,
    API_GATEWAY_ROLE_NAME,
)
from stelvio.aws.function import Function
from stelvio.config import AwsConfig
from stelvio.context import AppContext, _ContextStore
from tests.test_utils import assert_config_dict_matches_dataclass

from ...pulumi_mocks import ACCOUNT_ID, DEFAULT_REGION, R, tid, tn
from ..conftest import assert_lambda_role_and_attachment
from .conftest import LAMBDA_INVOKE_ARN_TEMPLATE, TP, WEBSOCKET_API_ID

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
ACCESS_LOG_FORMAT = (
    '{"requestId":"$context.requestId",'
    '"ip":"$context.identity.sourceIp",'
    '"requestTime":"$context.requestTime",'
    '"routeKey":"$context.routeKey",'
    '"connectionId":"$context.connectionId",'
    '"eventType":"$context.eventType",'
    '"status":"$context.status",'
    '"integrationErrorMessage":"$context.integrationErrorMessage"}'
)
DEFAULT_WSS_URL = f"wss://{WEBSOCKET_API_ID}.execute-api.{DEFAULT_REGION}.amazonaws.com/$default"
DEFAULT_MANAGEMENT_URL = (
    f"https://{WEBSOCKET_API_ID}.execute-api.{DEFAULT_REGION}.amazonaws.com/$default"
)
DEFAULT_EXECUTION_ARN = f"arn:aws:execute-api:{DEFAULT_REGION}:{ACCOUNT_ID}:{WEBSOCKET_API_ID}"


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
    counts: dict[R, int] = field(default_factory=dict)


_ONE_ROUTE_COUNTS = {
    R.HTTP_API: 1,
    R.HTTP_API_STAGE: 1,
    R.API_ACCOUNT: 2,
    R.LOG_GROUP: 1,
    R.ROLE: 2,
    R.FUNCTION: 1,
    R.ROLE_POLICY_ATTACHMENT: 1,
    R.HTTP_API_INTEGRATION: 1,
    R.LAMBDA_PERMISSION: 1,
    R.HTTP_API_ROUTE: 1,
}

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
    counts=_ONE_ROUTE_COUNTS,
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
    counts={
        R.HTTP_API: 1,
        R.HTTP_API_STAGE: 1,
        R.API_ACCOUNT: 2,
        R.LOG_GROUP: 1,
        R.ROLE: 2,
        R.FUNCTION: 1,
        R.ROLE_POLICY_ATTACHMENT: 1,
        R.HTTP_API_INTEGRATION: 1,
        R.LAMBDA_PERMISSION: 1,
        R.HTTP_API_ROUTE: 2,
    },
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
    counts={
        R.HTTP_API: 1,
        R.HTTP_API_STAGE: 1,
        R.API_ACCOUNT: 2,
        R.LOG_GROUP: 1,
        R.ROLE: 4,
        R.FUNCTION: 3,
        R.ROLE_POLICY_ATTACHMENT: 3,
        R.HTTP_API_INTEGRATION: 3,
        R.LAMBDA_PERMISSION: 3,
        R.HTTP_API_ROUTE: 3,
    },
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
                "format": ACCESS_LOG_FORMAT,
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

    mocks.assert_res_counts(case.counts)


def test_websocket_api_config_dict_matches_websocket_api_config():
    assert_config_dict_matches_dataclass(WebsocketApiConfig, WebsocketApiConfigDict)


def test_websocket_api_rejects_invalid_config_type():
    with raises(TypeError, match="Invalid config type"):
        WebsocketApi("chat", config=123)  # type: ignore[arg-type]


@mark.parametrize(
    ("action", "expected_error"),
    [
        param(
            lambda: WebsocketApi(
                "chat",
                config=WebsocketApiConfig(),
                domain_name="chat.example.com",
            ),
            "cannot combine",
            id="config_and_opts",
        ),
        param(
            lambda: WebsocketApi("chat", access_log_retention_days=999),
            "access_log_retention_days",
            id="invalid_retention",
        ),
        param(
            lambda: WebsocketApi("chat", stage_name="$bad"),
            r"Stage name starting with '\$' must be exactly '\$default'",
            id="invalid_stage_name",
        ),
        param(
            lambda: WebsocketApi("chat", stage_name="with spaces"),
            "Stage name must contain only",
            id="stage_name_spaces",
        ),
        param(
            lambda: WebsocketApi("chat", stage_name="x" * 129),
            "Stage name must be at most 128 characters",
            id="stage_name_too_long",
        ),
        param(
            lambda: WebsocketApi("chat", route_selection_expression=""),
            "route_selection_expression cannot be empty",
            id="empty_route_selection",
        ),
    ],
)
def test_websocket_api_rejects_invalid_configuration(action, expected_error):
    with raises(ValueError, match=expected_error):
        action()


@mark.parametrize("case", WEBSOCKET_API_CASES, ids=lambda case: case.test_id)
def test_websocket_api_resource_graph(pulumi_mocks, case):
    api = WebsocketApi(
        "chat",
        stage_name=case.stage_name,
        route_selection_expression=case.route_selection_expression,
        access_log_retention_days=case.access_log_retention_days,
    )
    for route in case.routes:
        api.route(route.route_key, route.handler)

    @pulumi.runtime.test
    def deploy():
        return api.resources

    deploy()
    verify_websocket_api(pulumi_mocks, case)


def test_websocket_api_shared_function_instance(pulumi_mocks):
    function = Function("shared", handler="functions/simple.handler")
    api = WebsocketApi("chat")
    api.route("$connect", function)
    api.route("$disconnect", function)

    @pulumi.runtime.test
    def deploy():
        return api.resources

    deploy()

    integration_id = tid(TP + "chat-integration-chat-shared")
    pulumi_mocks.assert_res("shared", R.FUNCTION)
    pulumi_mocks.assert_res(
        "chat-route-sys-connect",
        R.HTTP_API_ROUTE,
        {
            "apiId": WEBSOCKET_API_ID,
            "routeKey": "$connect",
            "target": f"integrations/{integration_id}",
        },
    )
    pulumi_mocks.assert_res(
        "chat-route-sys-disconnect",
        R.HTTP_API_ROUTE,
        {
            "apiId": WEBSOCKET_API_ID,
            "routeKey": "$disconnect",
            "target": f"integrations/{integration_id}",
        },
    )
    pulumi_mocks.assert_res_counts(
        {
            R.HTTP_API: 1,
            R.HTTP_API_STAGE: 1,
            R.API_ACCOUNT: 2,
            R.LOG_GROUP: 1,
            R.ROLE: 2,
            R.FUNCTION: 1,
            R.ROLE_POLICY_ATTACHMENT: 1,
            R.HTTP_API_INTEGRATION: 1,
            R.LAMBDA_PERMISSION: 1,
            R.HTTP_API_ROUTE: 2,
        }
    )


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


@mark.parametrize(
    ("kwargs", "expected_url"),
    [
        param(dict, DEFAULT_WSS_URL, id="default_stage"),
        param(
            lambda: {"stage_name": "prod"},
            f"wss://{WEBSOCKET_API_ID}.execute-api.{DEFAULT_REGION}.amazonaws.com/prod",
            id="named_stage",
        ),
        param(
            lambda: {"domain_name": "chat.example.com"},
            "wss://chat.example.com",
            id="owned_domain",
        ),
        param(
            lambda: {"domain_name": "chat.example.com", "api_mapping_key": "v1"},
            "wss://chat.example.com/v1",
            id="owned_domain_key",
        ),
        param(
            lambda: {
                "domain": ApiDomain("shared", domain_name="chat.example.com"),
                "api_mapping_key": "v2",
            },
            "wss://chat.example.com/v2",
            id="shared_domain_key",
        ),
    ],
)
@pulumi.runtime.test
def test_websocket_api_url(pulumi_mocks, app_context_with_dns, kwargs, expected_url):
    api = WebsocketApi("chat", **kwargs())
    api.route("$connect", "functions/simple.handler")

    def check(url):
        assert url == expected_url

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


@mark.parametrize(
    ("kwargs", "expected_url"),
    [
        param(
            {},
            f"https://{WEBSOCKET_API_ID}.execute-api.{DEFAULT_REGION}.amazonaws.com/$default",
            id="default_stage",
        ),
        param(
            {"stage_name": "prod"},
            f"https://{WEBSOCKET_API_ID}.execute-api.{DEFAULT_REGION}.amazonaws.com/prod",
            id="named_stage",
        ),
        param(
            {"domain_name": "chat.example.com", "api_mapping_key": "v1"},
            f"https://{WEBSOCKET_API_ID}.execute-api.{DEFAULT_REGION}.amazonaws.com/$default",
            id="custom_domain",
        ),
    ],
)
@pulumi.runtime.test
def test_websocket_api_management_url_is_https_execute_api(pulumi_mocks, kwargs, expected_url):
    api = WebsocketApi("chat", **kwargs)
    management_url = api.management_url
    api.route("$connect", "functions/simple.handler")

    def check(resolved):
        assert resolved == expected_url

    management_url.apply(check)


@pulumi.runtime.test
def test_websocket_api_link_grants_manage_connections(pulumi_mocks):
    api = WebsocketApi("chat")
    api.route("$connect", "functions/simple.handler")
    _ = api.resources
    link = api.link()

    def check(args):
        properties, permissions, resource = args
        assert properties == {
            "api_url": DEFAULT_WSS_URL,
            "api_execution_arn": DEFAULT_EXECUTION_ARN,
            "api_management_url": DEFAULT_MANAGEMENT_URL,
        }
        assert len(permissions) == 1
        permission = permissions[0]
        assert permission.actions == ["execute-api:ManageConnections"]
        assert resource == f"{DEFAULT_EXECUTION_ARN}/*/*/@connections/*"

    return pulumi.Output.all(
        link.properties, link.permissions, link.permissions[0].resources[0]
    ).apply(check)


def test_websocket_api_link_injects_api_url_env_vars(pulumi_mocks):
    api = WebsocketApi("chat")
    api.route("$connect", "functions/simple.handler")
    fn = Function("client", handler="functions/simple.handler", links=[api])

    @pulumi.runtime.test
    def deploy():
        return api.resources, fn.resources

    deploy()

    pulumi_mocks.assert_res(
        "client",
        R.FUNCTION,
        {
            "environment": {
                "variables": {
                    "STLV_CHAT_API_URL": DEFAULT_WSS_URL,
                    "STLV_CHAT_API_EXECUTION_ARN": DEFAULT_EXECUTION_ARN,
                    "STLV_CHAT_API_MANAGEMENT_URL": DEFAULT_MANAGEMENT_URL,
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
                        "resources": [f"{DEFAULT_EXECUTION_ARN}/*/*/@connections/*"],
                    }
                ]
            ),
        },
    )


def test_websocket_api_route_function_can_link_to_same_api(pulumi_mocks):
    api = WebsocketApi("chat")
    function = Function("default", handler="functions/simple.handler", links=[api])
    api.route("$default", function)
    expected_url = DEFAULT_WSS_URL
    expected_management_url = DEFAULT_MANAGEMENT_URL
    expected_execution_arn = DEFAULT_EXECUTION_ARN

    @pulumi.runtime.test
    def deploy():
        return api.resources

    deploy()

    pulumi_mocks.assert_res_counts(
        {
            R.HTTP_API: 1,
            R.HTTP_API_STAGE: 1,
            R.API_ACCOUNT: 2,
            R.LOG_GROUP: 1,
            R.ROLE: 2,
            R.FUNCTION: 1,
            R.ROLE_POLICY_ATTACHMENT: 2,
            R.POLICY: 1,
            R.HTTP_API_INTEGRATION: 1,
            R.LAMBDA_PERMISSION: 1,
            R.HTTP_API_ROUTE: 1,
        }
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
                    "STLV_CHAT_API_MANAGEMENT_URL": expected_management_url,
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
                        "resources": [f"{expected_execution_arn}/*/*/@connections/*"],
                    }
                ]
            ),
        },
    )


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

    @pulumi.runtime.test
    def deploy():
        return api.resources

    deploy()

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
                "format": ACCESS_LOG_FORMAT,
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
    pulumi_mocks.assert_res_counts(
        {
            R.HTTP_API: 1,
            R.HTTP_API_STAGE: 1,
            R.API_ACCOUNT: 2,
            R.LOG_GROUP: 1,
            R.ROLE: 2,
            R.FUNCTION: 1,
            R.ROLE_POLICY_ATTACHMENT: 1,
            R.HTTP_API_INTEGRATION: 1,
            R.LAMBDA_PERMISSION: 1,
            R.HTTP_API_ROUTE: 1,
            R.HTTP_API_MAPPING: 1,
            R.CERTIFICATE: 1,
            R.CLOUDFLARE_RECORD: 2,
            R.CERTIFICATE_VALIDATION: 1,
            R.HTTP_API_DOMAIN_NAME: 1,
        }
    )


def test_multiple_websocket_apis_with_same_routes_coexist_with_unique_resource_names(
    pulumi_mocks,
):
    api1 = WebsocketApi("chat-api")
    api1.route("$connect", "functions/simple.handler")
    api1.route("sendMessage", "functions/users.handler")

    api2 = WebsocketApi("admin-chat")
    api2.route("$connect", "functions/simple.handler")
    api2.route("sendMessage", "functions/users.handler")

    @pulumi.runtime.test
    def deploy():
        return api1.resources, api2.resources

    deploy()

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
