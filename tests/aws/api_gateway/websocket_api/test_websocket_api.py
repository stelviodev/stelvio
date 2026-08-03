import pulumi
import pytest

from stelvio.aws.api_gateway import WebsocketApi
from stelvio.aws.function import Function
from tests.aws.pulumi_mocks import ACCOUNT_ID, DEFAULT_REGION, R, tid, tn

TP = "test-test-"
API_ID = tid(TP + "chat")[:8]
LAMBDA_INVOKE_ARN = (
    f"arn:aws:apigateway:{DEFAULT_REGION}:lambda:path/2015-03-31/functions/"
    f"arn:aws:lambda:{DEFAULT_REGION}:{ACCOUNT_ID}:function:"
    f"{tn(TP + 'chat-functions-simple_handler')}/invocations"
)


@pulumi.runtime.test
def test_websocket_api_creates_connect_route(pulumi_mocks, project_cwd):
    api = WebsocketApi("chat")
    api.route("$connect", "functions/simple.handler")
    _ = api.resources

    def check(_):
        pulumi_mocks.assert_res(
            "chat",
            R.HTTP_API,
            {
                "protocolType": "WEBSOCKET",
                "routeSelectionExpression": "$request.body.action",
            },
            partial=True,
        )
        pulumi_mocks.assert_res(
            "chat-integration-chat-functions-simple_handler",
            R.HTTP_API_INTEGRATION,
            {
                "apiId": API_ID,
                "integrationType": "AWS_PROXY",
                "integrationMethod": "POST",
                "integrationUri": LAMBDA_INVOKE_ARN,
            },
        )
        pulumi_mocks.assert_res(
            "chat-route-default-connect",
            R.HTTP_API_ROUTE,
            {
                "apiId": API_ID,
                "routeKey": "$connect",
                "target": (
                    f"integrations/{tid(TP + 'chat-integration-chat-functions-simple_handler')}"
                ),
            },
        )
        pulumi_mocks.assert_res(
            "chat-stage",
            R.HTTP_API_STAGE,
            {"apiId": API_ID, "name": "$default", "autoDeploy": True},
        )
        pulumi_mocks.assert_res(
            "chat-permission-chat-functions-simple_handler",
            R.LAMBDA_PERMISSION,
            {
                "function": tn(TP + "chat-functions-simple_handler"),
                "principal": "apigateway.amazonaws.com",
                "sourceArn": (f"arn:aws:execute-api:{DEFAULT_REGION}:{ACCOUNT_ID}:{API_ID}/*/*"),
            },
            partial=True,
        )
        pulumi_mocks.assert_res_counts(
            {
                R.HTTP_API: 1,
                R.HTTP_API_STAGE: 1,
                R.HTTP_API_INTEGRATION: 1,
                R.HTTP_API_ROUTE: 1,
                R.LAMBDA_PERMISSION: 1,
                R.FUNCTION: 1,
                R.ROLE: 1,
                R.ROLE_POLICY_ATTACHMENT: 1,
            }
        )

    pulumi.Output.all(api.resources.stage.invoke_url, api.resources.routes[0].id).apply(check)


@pulumi.runtime.test
def test_websocket_api_exposes_wss_url(pulumi_mocks, project_cwd):
    api = WebsocketApi("chat")
    api.route("$connect", "functions/simple.handler")

    def check(url):
        assert url == f"wss://{API_ID}.execute-api.{DEFAULT_REGION}.amazonaws.com"

    api.url.apply(check)


def test_websocket_api_rejects_duplicate_routes():
    api = WebsocketApi("chat")
    api.route("$connect", "functions/simple.handler")

    with pytest.raises(ValueError, match=r"Duplicate route key: '\$connect'"):
        api.route("$connect", "functions/disconnect.main")


@pulumi.runtime.test
def test_websocket_api_rejects_routes_after_resource_creation(pulumi_mocks, project_cwd):
    api = WebsocketApi("chat")
    api.route("$connect", "functions/simple.handler")
    _ = api.resources

    with pytest.raises(RuntimeError, match="after resources have been created"):
        api.route("$default", "functions/default.main")


@pulumi.runtime.test
def test_websocket_api_accepts_existing_function(pulumi_mocks, project_cwd):
    function = Function("connect", handler="functions/simple.handler")
    api = WebsocketApi("chat")
    api.route("$connect", function)

    assert api.resources.integrations
