import json

import pulumi
from pytest import raises

from stelvio.aws.api_gateway import ApiDomain, WebsocketApi, WebsocketApiConfig
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
            "chat-route-sys-connect",
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

    return pulumi.Output.all(api.resources.stage.invoke_url, api.resources.routes[0].id).apply(
        check
    )


@pulumi.runtime.test
def test_websocket_api_exposes_wss_url(pulumi_mocks, project_cwd):
    api = WebsocketApi("chat")
    api.route("$connect", "functions/simple.handler")

    def check(url):
        assert url == f"wss://{API_ID}.execute-api.{DEFAULT_REGION}.amazonaws.com"

    return api.url.apply(check)


def test_websocket_api_rejects_duplicate_routes():
    api = WebsocketApi("chat")
    api.route("$connect", "functions/simple.handler")

    with raises(ValueError, match=r"Duplicate route key: '\$connect'"):
        api.route("$connect", "functions/disconnect.main")


@pulumi.runtime.test
def test_websocket_api_lambda_authorizer_protects_connect(pulumi_mocks, project_cwd):
    api = WebsocketApi("chat")
    auth = api.add_lambda_authorizer(
        "jwt-auth",
        "functions/users.handler",
        identity_sources=["route.request.header.Authorization"],
    )
    api.route("$connect", "functions/simple.handler", auth=auth)
    resources = api.resources

    def check(_):
        pulumi_mocks.assert_res(
            "chat-authorizer-jwt-auth",
            R.HTTP_API_AUTHORIZER,
            {
                "apiId": API_ID,
                "authorizerType": "REQUEST",
                "authorizerUri": (
                    f"arn:aws:apigateway:{DEFAULT_REGION}:lambda:path/2015-03-31/functions/"
                    f"arn:aws:lambda:{DEFAULT_REGION}:{ACCOUNT_ID}:function:"
                    f"{tn(TP + 'chat-auth-jwt-auth')}/invocations"
                ),
                "identitySources": ["route.request.header.Authorization"],
                "name": "jwt-auth",
            },
        )
        pulumi_mocks.assert_res(
            "chat-auth-permission-jwt-auth",
            R.LAMBDA_PERMISSION,
            {
                "function": tn(TP + "chat-auth-jwt-auth"),
                "principal": "apigateway.amazonaws.com",
                "sourceArn": (
                    f"arn:aws:execute-api:{DEFAULT_REGION}:{ACCOUNT_ID}:{API_ID}/authorizers/"
                    f"{tid(TP + 'chat-authorizer-jwt-auth')}"
                ),
            },
            partial=True,
        )
        pulumi_mocks.assert_res(
            "chat-route-sys-connect",
            R.HTTP_API_ROUTE,
            {
                "routeKey": "$connect",
                "authorizationType": "CUSTOM",
                "authorizerId": tid(TP + "chat-authorizer-jwt-auth"),
            },
            partial=True,
        )
        pulumi_mocks.assert_res_counts(
            {
                R.HTTP_API: 1,
                R.HTTP_API_AUTHORIZER: 1,
                R.HTTP_API_STAGE: 1,
                R.HTTP_API_INTEGRATION: 1,
                R.HTTP_API_ROUTE: 1,
                R.LAMBDA_PERMISSION: 2,
                R.FUNCTION: 2,
                R.ROLE: 2,
                R.ROLE_POLICY_ATTACHMENT: 2,
            }
        )

    return pulumi.Output.all(resources.routes[0].id).apply(check)


@pulumi.runtime.test
def test_websocket_api_iam_authorizer_does_not_create_lambda_authorizer(pulumi_mocks, project_cwd):
    api = WebsocketApi("chat")
    api.route("$connect", "functions/simple.handler", auth="IAM")
    resources = api.resources

    def check(_):
        pulumi_mocks.assert_res(
            "chat-route-sys-connect",
            R.HTTP_API_ROUTE,
            {"routeKey": "$connect", "authorizationType": "AWS_IAM"},
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

    return resources.routes[0].id.apply(check)


def test_websocket_api_rejects_auth_on_non_connect_routes():
    api = WebsocketApi("chat")
    auth = api.add_lambda_authorizer(
        "jwt-auth",
        "functions/users.handler",
        identity_sources=["route.request.querystring.token"],
    )

    with raises(ValueError, match=r"only be configured on the '\$connect' route"):
        api.route("$default", "functions/simple.handler", auth=auth)

    with raises(ValueError, match=r"only be configured on the '\$connect' route"):
        api.route("message", "functions/simple.handler", auth="IAM")


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


@pulumi.runtime.test
def test_websocket_api_rejects_routes_after_resource_creation(pulumi_mocks, project_cwd):
    api = WebsocketApi("chat")
    api.route("$connect", "functions/simple.handler")
    _ = api.resources

    with raises(
        RuntimeError,
        match=r"after resources have been created.*routes and authorizers",
    ):
        api.route("$default", "functions/default.main")

    with raises(
        RuntimeError,
        match=r"after resources have been created.*routes and authorizers",
    ):
        api.add_lambda_authorizer(
            "jwt-auth",
            "functions/users.handler",
            identity_sources=["route.request.header.Authorization"],
        )


@pulumi.runtime.test
def test_websocket_api_rejects_unused_authorizers(pulumi_mocks, project_cwd):
    api = WebsocketApi("chat")
    api.add_lambda_authorizer(
        "jwt-auth",
        "functions/users.handler",
        identity_sources=["route.request.header.Authorization"],
    )
    api.route("$connect", "functions/simple.handler")

    with raises(ValueError, match=r"unused authorizer\(s\): 'jwt-auth'"):
        _ = api.resources


@pulumi.runtime.test
def test_websocket_api_accepts_existing_function(pulumi_mocks, project_cwd):
    function = Function("connect", handler="functions/simple.handler")
    api = WebsocketApi("chat")
    api.route("$connect", function)

    resources = api.resources

    def check(_):
        pulumi_mocks.assert_res(
            "chat-route-sys-connect",
            R.HTTP_API_ROUTE,
            {"apiId": API_ID, "routeKey": "$connect"},
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

    return pulumi.Output.all(
        resources.stage.id,
        resources.integrations[0].id,
        resources.routes[0].id,
    ).apply(check)


@pulumi.runtime.test
def test_websocket_api_dedupes_shared_handler(pulumi_mocks, project_cwd):
    api = WebsocketApi("chat")
    api.route("$connect", "functions/simple.handler")
    api.route("$disconnect", "functions/simple.handler")
    _ = api.resources

    def check(_):
        pulumi_mocks.assert_res_counts(
            {
                R.HTTP_API: 1,
                R.HTTP_API_STAGE: 1,
                R.HTTP_API_INTEGRATION: 1,
                R.HTTP_API_ROUTE: 2,
                R.LAMBDA_PERMISSION: 1,
                R.FUNCTION: 1,
                R.ROLE: 1,
                R.ROLE_POLICY_ATTACHMENT: 1,
            }
        )
        pulumi_mocks.assert_res(
            "chat-route-sys-connect",
            R.HTTP_API_ROUTE,
            {"routeKey": "$connect"},
            partial=True,
        )
        pulumi_mocks.assert_res(
            "chat-route-sys-disconnect",
            R.HTTP_API_ROUTE,
            {"routeKey": "$disconnect"},
            partial=True,
        )

    return pulumi.Output.all(
        api.resources.stage.invoke_url, *[r.id for r in api.resources.routes]
    ).apply(check)


@pulumi.runtime.test
def test_websocket_api_rejects_multiple_handler_configs(pulumi_mocks, project_cwd):
    api = WebsocketApi("chat")
    api.route("$connect", "functions/simple.handler", memory=256)
    api.route("$disconnect", "functions/simple.handler", timeout=30)

    with raises(ValueError, match="Multiple routes trying to configure"):
        _ = api.resources


@pulumi.runtime.test
def test_websocket_api_folder_handlers_get_distinct_lambdas(pulumi_mocks, project_cwd):
    """folder/:: configs that share a handler suffix must not collide."""
    api = WebsocketApi("chat")
    api.route("$connect", "functions/folder::handler.fn")
    api.route("$disconnect", "functions/folder2::handler.fn")
    _ = api.resources

    def check(_):
        pulumi_mocks.assert_res_counts(
            {
                R.HTTP_API: 1,
                R.HTTP_API_STAGE: 1,
                R.HTTP_API_INTEGRATION: 2,
                R.HTTP_API_ROUTE: 2,
                R.LAMBDA_PERMISSION: 2,
                R.FUNCTION: 2,
                R.ROLE: 2,
                R.ROLE_POLICY_ATTACHMENT: 2,
            }
        )

    return pulumi.Output.all(
        api.resources.stage.invoke_url, *[r.id for r in api.resources.routes]
    ).apply(check)


@pulumi.runtime.test
def test_websocket_api_custom_route(pulumi_mocks, project_cwd):
    api = WebsocketApi("chat")
    api.route("sendMessage", "functions/simple.handler")
    _ = api.resources

    def check(_):
        pulumi_mocks.assert_res(
            "chat-route-sendMessage",
            R.HTTP_API_ROUTE,
            {
                "routeKey": "sendMessage",
                "routeResponseSelectionExpression": "$default",
            },
            partial=True,
        )
        pulumi_mocks.assert_res(
            "chat-route-response-sendMessage",
            R.HTTP_API_ROUTE_RESPONSE,
            {
                "apiId": API_ID,
                "routeId": tid(TP + "chat-route-sendMessage"),
                "routeResponseKey": "$default",
            },
        )
        pulumi_mocks.assert_res_counts(
            {
                R.HTTP_API: 1,
                R.HTTP_API_STAGE: 1,
                R.HTTP_API_INTEGRATION: 1,
                R.HTTP_API_ROUTE: 1,
                R.HTTP_API_ROUTE_RESPONSE: 1,
                R.LAMBDA_PERMISSION: 1,
                R.FUNCTION: 1,
                R.ROLE: 1,
                R.ROLE_POLICY_ATTACHMENT: 1,
            }
        )

    return pulumi.Output.all(
        api.resources.stage.invoke_url,
        api.resources.routes[0].id,
        api.resources.route_responses[0].id,
    ).apply(check)


@pulumi.runtime.test
def test_websocket_api_default_route_gets_route_response(pulumi_mocks, project_cwd):
    api = WebsocketApi("chat")
    api.route("$connect", "functions/simple.handler")
    api.route("$default", "functions/simple2.handler")
    _ = api.resources

    def check(_):
        pulumi_mocks.assert_res(
            "chat-route-sys-default",
            R.HTTP_API_ROUTE,
            {
                "routeKey": "$default",
                "routeResponseSelectionExpression": "$default",
            },
            partial=True,
        )
        pulumi_mocks.assert_res(
            "chat-route-response-sys-default",
            R.HTTP_API_ROUTE_RESPONSE,
            {
                "apiId": API_ID,
                "routeId": tid(TP + "chat-route-sys-default"),
                "routeResponseKey": "$default",
            },
        )
        pulumi_mocks.assert_res_counts(
            {
                R.HTTP_API: 1,
                R.HTTP_API_STAGE: 1,
                R.HTTP_API_INTEGRATION: 2,
                R.HTTP_API_ROUTE: 2,
                R.HTTP_API_ROUTE_RESPONSE: 1,
                R.LAMBDA_PERMISSION: 2,
                R.FUNCTION: 2,
                R.ROLE: 2,
                R.ROLE_POLICY_ATTACHMENT: 2,
            }
        )

    return pulumi.Output.all(
        api.resources.stage.invoke_url,
        *[r.id for r in api.resources.routes],
        *[rr.id for rr in api.resources.route_responses],
    ).apply(check)


@pulumi.runtime.test
def test_websocket_api_link_grants_manage_connections(pulumi_mocks, project_cwd):
    api = WebsocketApi("chat")
    api.route("$connect", "functions/simple.handler")
    _ = api.resources
    link = api.link()

    def check(args):
        properties, permissions = args
        assert "api_url" in properties
        assert "api_execution_arn" in properties
        assert len(permissions) == 1
        permission = permissions[0]
        assert permission.actions == ["execute-api:ManageConnections"]

        def check_resource(resource):
            assert resource == (
                f"arn:aws:execute-api:{DEFAULT_REGION}:{ACCOUNT_ID}:{API_ID}/*/@connections/*"
            )

        return permission.resources[0].apply(check_resource)

    return pulumi.Output.all(link.properties, link.permissions).apply(check)


@pulumi.runtime.test
def test_websocket_api_route_function_can_link_to_same_api(pulumi_mocks, project_cwd):
    api = WebsocketApi("chat")
    function = Function("default", handler="functions/simple.handler", links=[api])
    api.route("$default", function)

    resources = api.resources
    expected_url = f"wss://{API_ID}.execute-api.{DEFAULT_REGION}.amazonaws.com"
    expected_execution_arn = f"arn:aws:execute-api:{DEFAULT_REGION}:{ACCOUNT_ID}:{API_ID}"

    def check(_):
        pulumi_mocks.assert_res_counts(
            {
                R.HTTP_API: 1,
                R.HTTP_API_STAGE: 1,
                R.HTTP_API_INTEGRATION: 1,
                R.HTTP_API_ROUTE: 1,
                R.HTTP_API_ROUTE_RESPONSE: 1,
                R.LAMBDA_PERMISSION: 1,
                R.FUNCTION: 1,
                R.ROLE: 1,
                R.POLICY: 1,
                R.ROLE_POLICY_ATTACHMENT: 2,
            }
        )
        pulumi_mocks.assert_res(
            "chat-route-sys-default",
            R.HTTP_API_ROUTE,
            {"routeKey": "$default"},
            partial=True,
        )
        pulumi_mocks.assert_res(
            "chat-route-response-sys-default",
            R.HTTP_API_ROUTE_RESPONSE,
            {
                "apiId": API_ID,
                "routeId": tid(TP + "chat-route-sys-default"),
                "routeResponseKey": "$default",
            },
        )
        functions = pulumi_mocks.created_functions(TP + "default")
        assert len(functions) == 1
        env_vars = functions[0].inputs["environment"]["variables"]
        assert env_vars["STLV_CHAT_API_URL"] == expected_url
        assert env_vars["STLV_CHAT_API_EXECUTION_ARN"] == expected_execution_arn

        policies = pulumi_mocks.created_policies(TP + "default-p")
        assert len(policies) == 1
        statements = json.loads(policies[0].inputs["policy"])
        assert len(statements) == 1
        assert statements[0]["actions"] == ["execute-api:ManageConnections"]
        assert statements[0]["resources"] == [f"{expected_execution_arn}/*/@connections/*"]

    return pulumi.Output.all(
        resources.stage.invoke_url,
        resources.routes[0].id,
        resources.route_responses[0].id,
        function.resources.function.id,
    ).apply(check)


def test_websocket_api_rejects_function_with_options():
    function = Function("connect", handler="functions/simple.handler")
    api = WebsocketApi("chat")

    with raises(ValueError, match="Cannot combine a Function handler"):
        api.route("$connect", function, memory=256)


@pulumi.runtime.test
def test_websocket_api_owned_domain_creates_mapping_and_custom_url(
    pulumi_mocks, app_context_with_dns, project_cwd
):
    api = WebsocketApi("chat", domain_name="chat.example.com", api_mapping_key="v1")
    api.route("$connect", "functions/simple.handler")
    resources = api.resources

    def check(values):
        url, mapping_id = values
        assert url == "wss://chat.example.com/v1"
        assert mapping_id == tid(TP + "chat-api-mapping")
        pulumi_mocks.assert_res(
            "chat-api-mapping",
            R.HTTP_API_MAPPING,
            {
                "apiId": API_ID,
                "domainName": "chat.example.com",
                "stage": tid(TP + "chat-stage"),
                "apiMappingKey": "v1",
            },
        )
        pulumi_mocks.assert_res(
            "chat-domain-domain",
            R.HTTP_API_DOMAIN_NAME,
            {"domainName": "chat.example.com"},
            partial=True,
        )

    return pulumi.Output.all(api.url, resources.api_mapping.id).apply(check)


def test_websocket_api_rejects_invalid_domain_configuration():
    domain = ApiDomain("shared-domain", domain_name="chat.example.com")

    with raises(ValueError, match="Cannot specify both 'domain_name' and 'domain'"):
        WebsocketApiConfig(domain=domain, domain_name="other.example.com")

    with raises(ValueError, match="api_mapping_key requires"):
        WebsocketApiConfig(api_mapping_key="v1")

    with raises(ValueError, match="disable_execute_api_endpoint=True requires"):
        WebsocketApiConfig(disable_execute_api_endpoint=True)


@pulumi.runtime.test
def test_websocket_api_disable_execute_api_endpoint(
    pulumi_mocks, app_context_with_dns, project_cwd
):
    api = WebsocketApi("chat", domain_name="chat.example.com", disable_execute_api_endpoint=True)
    api.route("$connect", "functions/simple.handler")
    _ = api.resources

    def check(_):
        pulumi_mocks.assert_res(
            "chat",
            R.HTTP_API,
            {
                "protocolType": "WEBSOCKET",
                "disableExecuteApiEndpoint": True,
            },
            partial=True,
        )

    return api.resources.api.id.apply(check)
