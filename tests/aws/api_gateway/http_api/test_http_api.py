"""Unit tests for HttpApi component (API Gateway v2)."""

import json

import pulumi
import pytest

from stelvio.aws.api_gateway.http_api import ApiDomain, HttpApi, HttpApiConfig
from stelvio.aws.api_gateway.http_api.http_api import _ACCESS_LOG_FORMAT
from stelvio.aws.api_gateway.rest_api.constants import (
    API_GATEWAY_LOGS_POLICY,
    API_GATEWAY_ROLE_NAME,
)
from stelvio.aws.function import Function, FunctionConfig
from stelvio.config import AwsConfig
from stelvio.context import AppContext, _ContextStore

from ....conftest import TP
from ...pulumi_mocks import (
    ACCOUNT_ID,
    DEFAULT_REGION,
    tid,
    tn,
)
from .conftest import when_http_api_ready

pytestmark = pytest.mark.usefixtures("project_cwd")

# --- Shared constants / templates -------------------------------------------
# The PulumiTestMocks derive the API id from the resource id: api_id = tid(name)[:8].
HTTP_API_ID = tid(TP + "my-api")[:8]
API_EXECUTION_ARN = f"arn:aws:execute-api:{DEFAULT_REGION}:{ACCOUNT_ID}:{HTTP_API_ID}"
ROUTE_PERMISSION_SOURCE_ARN = f"{API_EXECUTION_ARN}/*/*"
LAMBDA_INVOKE_ARN_TEMPLATE = (
    f"arn:aws:apigateway:{DEFAULT_REGION}:lambda:path/2015-03-31/functions/"
    f"arn:aws:lambda:{DEFAULT_REGION}:{ACCOUNT_ID}:function:{{function_name}}/invocations"
)


# ---------------------------------------------------------------------------
# Basic creation
# ---------------------------------------------------------------------------


@pulumi.runtime.test
def test_http_api_creates_complete_resource_graph(pulumi_mocks):
    api = HttpApi("my-api")
    api.route("GET", "/users", "functions/simple.handler")
    api.route("POST", "/orders", "functions/users.handler")
    _ = api.resources

    def check(_):
        assert len(pulumi_mocks.created_http_apis()) == 1
        assert len(pulumi_mocks.created_http_api_stages()) == 1
        assert len(pulumi_mocks.created_log_groups()) == 1
        assert len(pulumi_mocks.created_http_api_integrations()) == 2
        assert len(pulumi_mocks.created_http_api_routes()) == 2
        assert len(pulumi_mocks.created_functions()) == 2
        assert len(pulumi_mocks.created_permissions()) == 2

        roles = pulumi_mocks.created_roles(API_GATEWAY_ROLE_NAME)
        assert len(roles) == 1
        assert json.loads(roles[0].inputs["assumeRolePolicy"]) == [
            {
                "actions": ["sts:AssumeRole"],
                "principals": [{"identifiers": ["apigateway.amazonaws.com"], "type": "Service"}],
            }
        ]
        assert roles[0].inputs["managedPolicyArns"] == [API_GATEWAY_LOGS_POLICY]

        accounts = pulumi_mocks.created_api_accounts("api-gateway-account")
        assert len(accounts) == 1
        assert accounts[0].inputs["cloudwatchRoleArn"] == (
            f"arn:aws:iam::{ACCOUNT_ID}:role/{API_GATEWAY_ROLE_NAME}-test-name"
        )

    when_http_api_ready(api, check)


@pulumi.runtime.test
def test_http_api_empty_api_creates_only_base_resources(pulumi_mocks):
    api = HttpApi("my-api")
    _ = api.resources

    def check(_):
        assert len(pulumi_mocks.created_http_apis()) == 1
        assert len(pulumi_mocks.created_http_api_stages()) == 1
        assert len(pulumi_mocks.created_log_groups()) == 1
        assert len(pulumi_mocks.created_http_api_integrations()) == 0
        assert len(pulumi_mocks.created_http_api_routes()) == 0
        assert len(pulumi_mocks.created_functions()) == 0
        assert len(pulumi_mocks.created_permissions()) == 0

    api.resources.stage.id.apply(check)


@pulumi.runtime.test
def test_http_api_creates_api_resource(pulumi_mocks):
    api = HttpApi("my-api")
    api.route("GET", "/users", "functions/simple.handler")
    _ = api.resources

    def check(_):
        apis = pulumi_mocks.created_http_apis()
        assert len(apis) == 1
        assert apis[0].typ == "aws:apigatewayv2/api:Api"
        assert apis[0].name == TP + "my-api"
        assert apis[0].inputs["protocolType"] == "HTTP"

    api.resources.api.id.apply(check)


@pulumi.runtime.test
def test_http_api_creates_stage(pulumi_mocks):
    api = HttpApi("my-api")
    api.route("GET", "/users", "functions/simple.handler")
    _ = api.resources

    def check(_):
        stages = pulumi_mocks.created_http_api_stages()
        assert len(stages) == 1
        assert stages[0].typ == "aws:apigatewayv2/stage:Stage"
        assert stages[0].name == TP + "my-api-stage"
        assert stages[0].inputs["autoDeploy"] is True
        assert stages[0].inputs["name"] == "$default"
        # Stage must link to the API
        assert stages[0].inputs["apiId"] == tid(TP + "my-api")

        # Access log settings must target this API's log group with the standard format.
        log_groups = pulumi_mocks.created_log_groups(TP + "my-api-logs")
        assert len(log_groups) == 1
        expected_log_arn = (
            f"arn:aws:logs:{DEFAULT_REGION}:{ACCOUNT_ID}:log-group:{tn(TP + 'my-api-logs')}:*"
        )
        access_log = stages[0].inputs["accessLogSettings"]
        assert access_log["destinationArn"] == expected_log_arn
        assert access_log["format"] == _ACCESS_LOG_FORMAT

    api.resources.stage.id.apply(check)


@pulumi.runtime.test
def test_http_api_creates_log_group(pulumi_mocks):
    api = HttpApi("my-api")
    api.route("GET", "/users", "functions/simple.handler")
    _ = api.resources

    def check(_):
        log_groups = pulumi_mocks.created_log_groups(TP + "my-api-logs")
        assert len(log_groups) == 1
        assert log_groups[0].typ == "aws:cloudwatch/logGroup:LogGroup"
        assert log_groups[0].inputs["retentionInDays"] == 30

    api.resources.log_group.arn.apply(check)


@pulumi.runtime.test
def test_http_api_arn_property(pulumi_mocks):
    api = HttpApi("my-api")
    api.route("GET", "/users", "functions/simple.handler")
    _ = api.resources

    def check(arn):
        # Mock returns arn = f"arn:aws:apigateway:{region}::/apis/{api_id}"
        assert arn == f"arn:aws:apigateway:{DEFAULT_REGION}::/apis/{HTTP_API_ID}"

    api.arn.apply(check)


@pulumi.runtime.test
def test_http_api_link_injects_api_url_env_vars(pulumi_mocks):
    api = HttpApi("orders-api")
    api.route("GET", "/orders", "functions/simple.handler")
    fn = Function("client", handler="functions/simple.handler", links=[api])

    def check(api_properties):
        functions = pulumi_mocks.created_functions()
        client_fn = next(f for f in functions if f.name == "test-test-client")
        env_vars = client_fn.inputs["environment"]["variables"]
        assert env_vars["STLV_ORDERS_API_API_URL"] == api_properties[0]
        assert env_vars["STLV_ORDERS_API_API_EXECUTION_ARN"] == api_properties[1]

    pulumi.Output.all(api.url, api.execution_arn, fn.resources.function.id).apply(check)


@pulumi.runtime.test
def test_http_api_creates_function_for_route(pulumi_mocks):
    """A Lambda function is created for the route handler."""
    api = HttpApi("my-api")
    api.route("GET", "/users", "functions/simple.handler")
    _ = api.resources

    def check(_):
        fns = pulumi_mocks.created_functions()
        assert len(fns) == 1
        assert fns[0].typ == "aws:lambda/function:Function"
        assert fns[0].name == TP + "my-api-functions-simple_handler"

    when_http_api_ready(api, check)


@pulumi.runtime.test
def test_http_api_creates_integration(pulumi_mocks):
    api = HttpApi("my-api")
    api.route("GET", "/users", "functions/simple.handler")
    _ = api.resources

    def check(_):
        fns = pulumi_mocks.created_functions()
        assert len(fns) == 1

        integrations = pulumi_mocks.created_http_api_integrations()
        assert len(integrations) == 1
        assert integrations[0].typ == "aws:apigatewayv2/integration:Integration"
        assert integrations[0].name == TP + "my-api-integration-my-api-functions-simple_handler"
        assert integrations[0].inputs["integrationType"] == "AWS_PROXY"
        assert integrations[0].inputs["integrationMethod"] == "POST"
        assert integrations[0].inputs["payloadFormatVersion"] == "2.0"
        assert integrations[0].inputs["timeoutMilliseconds"] == 30000
        # Integration must be wired to the route's Lambda via its invoke ARN
        expected_uri = LAMBDA_INVOKE_ARN_TEMPLATE.format(function_name=tn(fns[0].name))
        assert integrations[0].inputs["integrationUri"] == expected_uri

    when_http_api_ready(api, check)


@pulumi.runtime.test
def test_http_api_creates_route(pulumi_mocks):
    api = HttpApi("my-api")
    api.route("GET", "/users", "functions/simple.handler")
    _ = api.resources

    def check(_):
        routes = pulumi_mocks.created_http_api_routes()
        assert len(routes) == 1
        assert routes[0].typ == "aws:apigatewayv2/route:Route"
        assert routes[0].name == TP + "my-api-route-GET--users"
        assert routes[0].inputs["routeKey"] == "GET /users"
        assert routes[0].inputs["authorizationType"] == "NONE"
        # Route target must point back at the integration
        integrations = pulumi_mocks.created_http_api_integrations()
        assert len(integrations) == 1
        expected_target = f"integrations/{tid(integrations[0].name)}"
        assert routes[0].inputs["target"] == expected_target

    when_http_api_ready(api, check)


@pulumi.runtime.test
def test_http_api_creates_lambda_permission(pulumi_mocks):
    api = HttpApi("my-api")
    api.route("GET", "/users", "functions/simple.handler")
    _ = api.resources

    def check(_):
        fns = pulumi_mocks.created_functions()
        assert len(fns) == 1

        perms = pulumi_mocks.created_permissions()
        assert len(perms) == 1
        perm = perms[0]
        assert perm.typ == "aws:lambda/permission:Permission"
        assert perm.name == TP + "my-api-permission-my-api-functions-simple_handler"
        assert perm.inputs["action"] == "lambda:InvokeFunction"
        assert perm.inputs["principal"] == "apigateway.amazonaws.com"
        # Permission must name the exact route Lambda and grant invoke from this API
        assert perm.inputs["function"] == tn(fns[0].name)
        assert perm.inputs["sourceArn"] == ROUTE_PERMISSION_SOURCE_ARN

    when_http_api_ready(api, check)


# ---------------------------------------------------------------------------
# Multiple routes
# ---------------------------------------------------------------------------


@pulumi.runtime.test
def test_http_api_multiple_routes_same_handler_creates_one_lambda(pulumi_mocks):
    """Routes with same handler path share one Lambda function."""
    api = HttpApi("my-api")
    api.route("GET", "/users", "functions/simple.handler")
    api.route("POST", "/users", "functions/simple.handler")
    _ = api.resources

    def check(_):
        routes = pulumi_mocks.created_http_api_routes()
        assert len(routes) == 2

        integrations = pulumi_mocks.created_http_api_integrations()
        assert len(integrations) == 1  # One integration shared

        expected_target = f"integrations/{tid(integrations[0].name)}"
        assert {route.inputs["target"] for route in routes} == {expected_target}

        # Exactly one Lambda is created for the shared handler, with the expected name
        functions = pulumi_mocks.created_functions()
        assert len(functions) == 1
        assert functions[0].name == TP + "my-api-functions-simple_handler"

    when_http_api_ready(api, check)


@pulumi.runtime.test
def test_http_api_different_handlers_create_different_lambdas(pulumi_mocks):
    api = HttpApi("my-api")
    api.route("GET", "/users", "functions/simple.handler")
    api.route("GET", "/orders", "functions/users.handler")
    _ = api.resources

    def check(_):
        routes = pulumi_mocks.created_http_api_routes()
        assert len(routes) == 2

        integrations = pulumi_mocks.created_http_api_integrations()
        assert len(integrations) == 2

        # Two distinct Lambdas named after their respective handlers
        functions = pulumi_mocks.created_functions()
        assert len(functions) == 2
        names = {f.name for f in functions}
        assert names == {
            TP + "my-api-functions-simple_handler",
            TP + "my-api-functions-users_handler",
        }

        expected_function_by_route = {
            "GET /users": TP + "my-api-functions-simple_handler",
            "GET /orders": TP + "my-api-functions-users_handler",
        }
        integration_by_id = {tid(integration.name): integration for integration in integrations}
        for route in routes:
            integration_id = route.inputs["target"].removeprefix("integrations/")
            integration = integration_by_id[integration_id]
            expected_uri = LAMBDA_INVOKE_ARN_TEMPLATE.format(
                function_name=tn(expected_function_by_route[route.inputs["routeKey"]])
            )
            assert integration.inputs["integrationUri"] == expected_uri

    when_http_api_ready(api, check)


@pulumi.runtime.test
def test_multiple_apis_with_same_routes_coexist_with_unique_resource_names(pulumi_mocks):
    """Two HTTP APIs with identical route structures produce no resource-name collisions.

    Permissions use safe_name(..., 100) which truncates long names, so coexistence
    with identical routes is the regression-prone case.
    """
    api1 = HttpApi("user-api")
    api1.route("GET", "/users", "functions/simple.handler")
    api1.route("POST", "/users", "functions/users.handler")

    api2 = HttpApi("admin-api")
    api2.route("GET", "/users", "functions/simple.handler")
    api2.route("POST", "/users", "functions/users.handler")

    def check(_):
        # Two distinct APIs created
        apis = pulumi_mocks.created_http_apis()
        assert len(apis) == 2
        assert {a.name for a in apis} == {TP + "user-api", TP + "admin-api"}

        # Every resource name across both APIs is unique (no cross-API collisions)
        all_names = [r.name for r in pulumi_mocks.created_resources]
        assert len(all_names) == len(set(all_names)), "Resource names collide across APIs"

        # Per-API: 2 routes + 2 integrations + 2 functions + 2 permissions
        for api_name in ("user-api", "admin-api"):
            routes = [r for r in pulumi_mocks.created_http_api_routes() if api_name in r.name]
            assert len(routes) == 2, f"{api_name}: expected 2 routes"
            integrations = [
                i for i in pulumi_mocks.created_http_api_integrations() if api_name in i.name
            ]
            assert len(integrations) == 2, f"{api_name}: expected 2 integrations"
            functions = [f for f in pulumi_mocks.created_functions() if api_name in f.name]
            assert len(functions) == 2, f"{api_name}: expected 2 functions"
            perms = [p for p in pulumi_mocks.created_permissions() if api_name in p.name]
            assert len(perms) == 2, f"{api_name}: expected 2 permissions"

    # Wait on routes + permissions for BOTH apis so deferred Output-dependent
    # resources are registered before assertions run (stage.id alone fires too early).
    wait_outputs = [api1.resources.stage.id, api2.resources.stage.id]
    wait_outputs.extend(r.id for r in api1.resources.routes)
    wait_outputs.extend(r.id for r in api2.resources.routes)
    wait_outputs.extend(p.id for p in api1.resources.permissions)
    wait_outputs.extend(p.id for p in api2.resources.permissions)
    pulumi.Output.all(*wait_outputs).apply(check)


# ---------------------------------------------------------------------------
# Route keys
# ---------------------------------------------------------------------------


@pulumi.runtime.test
def test_http_api_any_method_route_key(pulumi_mocks):
    api = HttpApi("my-api")
    api.route("ANY", "/health", "functions/simple.handler")
    _ = api.resources

    def check(_):
        routes = pulumi_mocks.created_http_api_routes()
        assert len(routes) == 1
        assert routes[0].inputs["routeKey"] == "ANY /health"

    when_http_api_ready(api, check)


@pulumi.runtime.test
def test_http_api_star_any_normalize(pulumi_mocks):
    api = HttpApi("my-api")
    api.route("*", "/health", "functions/simple.handler")
    _ = api.resources

    def check(_):
        routes = pulumi_mocks.created_http_api_routes()
        assert routes[0].inputs["routeKey"] == "ANY /health"

    when_http_api_ready(api, check)


@pulumi.runtime.test
def test_http_api_default_route_key(pulumi_mocks):
    api = HttpApi("my-api")
    api.route("ANY", "$default", "functions/simple.handler")
    _ = api.resources

    def check(_):
        routes = pulumi_mocks.created_http_api_routes()
        assert len(routes) == 1
        assert routes[0].inputs["routeKey"] == "$default"

    when_http_api_ready(api, check)


@pulumi.runtime.test
def test_http_api_multi_method_creates_multiple_routes(pulumi_mocks):
    api = HttpApi("my-api")
    api.route(["GET", "DELETE"], "/users/{id}", "functions/simple.handler")
    _ = api.resources

    def check(_):
        routes = pulumi_mocks.created_http_api_routes()
        route_keys = {r.inputs["routeKey"] for r in routes}
        assert route_keys == {"GET /users/{id}", "DELETE /users/{id}"}

    when_http_api_ready(api, check)


# ---------------------------------------------------------------------------
# Stage name
# ---------------------------------------------------------------------------


@pulumi.runtime.test
def test_http_api_custom_stage_name(pulumi_mocks):
    api = HttpApi("my-api", stage_name="v2")
    api.route("GET", "/users", "functions/simple.handler")
    _ = api.resources

    def check(_):
        stages = pulumi_mocks.created_http_api_stages()
        assert stages[0].inputs["name"] == "v2"

    api.resources.stage.id.apply(check)


# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------


@pulumi.runtime.test
def test_http_api_cors_true(pulumi_mocks):
    api = HttpApi("my-api", cors=True)
    api.route("GET", "/users", "functions/simple.handler")
    _ = api.resources

    def check(_):
        apis = pulumi_mocks.created_http_apis()
        assert len(apis) == 1
        cors = apis[0].inputs.get("corsConfiguration")
        assert cors == {
            "allowOrigins": ["*"],
            "allowMethods": ["*"],
            "allowHeaders": ["*"],
        }

    api.resources.api.id.apply(check)


@pulumi.runtime.test
def test_http_api_cors_false_no_cors_config(pulumi_mocks):
    api = HttpApi("my-api", cors=False)
    api.route("GET", "/users", "functions/simple.handler")
    _ = api.resources

    def check(_):
        apis = pulumi_mocks.created_http_apis()
        assert "corsConfiguration" not in apis[0].inputs

    api.resources.api.id.apply(check)


def test_http_api_cors_allow_credentials_with_wildcard_raises():
    from stelvio.aws.cors import CorsConfig

    with pytest.raises(ValueError, match="allow_credentials"):
        HttpApi(
            "my-api",
            cors=CorsConfig(allow_origins="*", allow_credentials=True),
        )


# ---------------------------------------------------------------------------
# Validation errors
# ---------------------------------------------------------------------------


def test_http_api_config_and_opts_raises():
    with pytest.raises(ValueError, match="cannot combine"):
        HttpApi("my-api", config=HttpApiConfig(), domain_name="example.com")


def test_http_api_mapping_key_without_domain_raises():
    with pytest.raises(ValueError, match="api_mapping_key requires"):
        HttpApi("my-api", api_mapping_key="v1")


def test_http_api_disable_execute_api_without_domain_raises():
    with pytest.raises(ValueError, match="disable_execute_api_endpoint"):
        HttpApi("my-api", disable_execute_api_endpoint=True)


def test_http_api_duplicate_route_key_raises():
    api = HttpApi("my-api")
    api.route("GET", "/users", "functions/simple.handler")
    with pytest.raises(ValueError, match=r"[Dd]uplicate"):
        api.route("GET", "/users", "functions/users.handler")


def test_http_api_invalid_stage_name_raises():
    with pytest.raises(ValueError, match="Stage name"):
        HttpApi("my-api", stage_name="$bad")


def test_http_api_invalid_log_retention_raises():
    with pytest.raises(ValueError, match="access_log_retention_days"):
        HttpApi("my-api", access_log_retention_days=999)


@pulumi.runtime.test
def test_http_api_allows_lambda_timeout_over_30(pulumi_mocks):
    api = HttpApi("my-api")
    api.route(
        "GET",
        "/slow",
        FunctionConfig(handler="functions/simple.handler", timeout=60),
    )
    _ = api.resources

    def check(_):
        functions = pulumi_mocks.created_functions()
        assert len(functions) == 1
        assert functions[0].inputs["timeout"] == 60

        integrations = pulumi_mocks.created_http_api_integrations()
        assert len(integrations) == 1
        assert integrations[0].inputs["timeoutMilliseconds"] == 30000

    when_http_api_ready(api, check)


# ---------------------------------------------------------------------------
# Access log retention
# ---------------------------------------------------------------------------


@pulumi.runtime.test
def test_http_api_access_log_retention_forever(pulumi_mocks):
    api = HttpApi("my-api", access_log_retention_days="forever")
    api.route("GET", "/users", "functions/simple.handler")
    _ = api.resources

    def check(_):
        log_groups = pulumi_mocks.created_log_groups(TP + "my-api-logs")
        assert len(log_groups) == 1
        assert "retentionInDays" not in log_groups[0].inputs

    api.resources.log_group.arn.apply(check)


# ---------------------------------------------------------------------------
# disable_execute_api_endpoint
# ---------------------------------------------------------------------------


@pulumi.runtime.test
def test_http_api_disable_execute_api_endpoint(pulumi_mocks, app_context_with_dns):
    api = HttpApi("my-api", domain_name="api.example.com", disable_execute_api_endpoint=True)
    api.route("GET", "/users", "functions/simple.handler")
    _ = api.resources

    def check(_):
        apis = pulumi_mocks.created_http_apis()
        assert apis[0].inputs.get("disableExecuteApiEndpoint") is True

    api.resources.api.id.apply(check)


@pulumi.runtime.test
def test_http_api_disable_execute_api_endpoint_with_shared_domain(
    pulumi_mocks, app_context_with_dns
):
    domain = ApiDomain("shared", domain_name="api.example.com")
    api = HttpApi("my-api", domain=domain, disable_execute_api_endpoint=True)
    api.route("GET", "/users", "functions/simple.handler")
    _ = api.resources

    def check(_):
        apis = pulumi_mocks.created_http_apis()
        assert apis[0].inputs.get("disableExecuteApiEndpoint") is True

    api.resources.api.id.apply(check)


# ---------------------------------------------------------------------------
# url property
# ---------------------------------------------------------------------------


@pulumi.runtime.test
def test_http_api_url_default_stage_execute_api(pulumi_mocks):
    api = HttpApi("my-api")
    api.route("GET", "/users", "functions/simple.handler")

    def check(urls):
        assert urls[0] == urls[1]

    pulumi.Output.all(api.url, api.resources.stage.invoke_url).apply(check)


@pulumi.runtime.test
def test_http_api_url_uses_resolved_stage_invoke_url_when_config_region_unset(pulumi_mocks):
    # Save the autouse app_context so we can restore it after temporarily swapping
    # in a context with an unset region (would otherwise leak into sibling tests).
    saved = _ContextStore.get()
    try:
        _ContextStore.clear()
        _ContextStore.set(
            AppContext(
                name="test",
                env="test",
                aws=AwsConfig(),
                home="aws",
            )
        )
        api = HttpApi("my-api")
        api.route("GET", "/users", "functions/simple.handler")

        def check(urls):
            assert urls[0] == urls[1]

        pulumi.Output.all(api.url, api.resources.stage.invoke_url).apply(check)
    finally:
        _ContextStore.clear()
        _ContextStore.set(saved)


@pulumi.runtime.test
def test_http_api_url_named_stage_execute_api(pulumi_mocks):
    api = HttpApi("my-api", stage_name="prod")
    api.route("GET", "/users", "functions/simple.handler")

    def check(urls):
        assert urls[0] == urls[1]

    pulumi.Output.all(api.url, api.resources.stage.invoke_url).apply(check)


@pulumi.runtime.test
def test_http_api_url_with_domain_name(pulumi_mocks, app_context_with_dns):
    api = HttpApi("my-api", domain_name="api.example.com")
    api.route("GET", "/users", "functions/simple.handler")

    def check(url):
        assert url == "https://api.example.com"

    api.url.apply(check)


@pulumi.runtime.test
def test_http_api_url_with_domain_name_and_mapping_key(pulumi_mocks, app_context_with_dns):
    api = HttpApi("my-api", domain_name="api.example.com", api_mapping_key="v1")
    api.route("GET", "/users", "functions/simple.handler")

    def check(url):
        assert url == "https://api.example.com/v1"

    api.url.apply(check)


@pulumi.runtime.test
def test_http_api_url_with_shared_domain_and_mapping_key(pulumi_mocks, app_context_with_dns):
    domain = ApiDomain("shared", domain_name="api.example.com")
    api = HttpApi("my-api", domain=domain, api_mapping_key="v2")
    api.route("GET", "/users", "functions/simple.handler")

    def check(url):
        assert url == "https://api.example.com/v2"

    api.url.apply(check)


def test_http_api_public_domain_properties(app_context_with_dns):
    domain = ApiDomain("shared", domain_name="api.example.com")
    api = HttpApi(
        "my-api",
        domain=domain,
        api_mapping_key="v2",
        disable_execute_api_endpoint=True,
    )

    assert api.config.domain_name is None
    assert api.domain_name == "api.example.com"
    assert api.config.api_mapping_key == "v2"
    assert api.config.disable_execute_api_endpoint is True


# ---------------------------------------------------------------------------
# Link config
# ---------------------------------------------------------------------------


def test_http_api_link_config_structure():
    from stelvio.component import ComponentRegistry

    api = HttpApi("orders")
    api.route("GET", "/orders", "functions/simple.handler")
    creator = ComponentRegistry.get_link_config_creator(HttpApi)
    link = creator(api)
    assert set(link.properties) == {"api_url", "api_execution_arn"}
    assert link.permissions == []


# ---------------------------------------------------------------------------
# Access log retention boundaries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("days", [1, 30, 3653])
def test_http_api_access_log_retention_valid_boundaries(days):
    # Must not raise
    HttpApiConfig(access_log_retention_days=days)


@pytest.mark.parametrize("days", [0, 2, 9999])
def test_http_api_access_log_retention_invalid_boundaries(days):
    with pytest.raises(ValueError, match="access_log_retention_days"):
        HttpApiConfig(access_log_retention_days=days)


# ---------------------------------------------------------------------------
# api_mapping_key validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_key",
    ["/v1", "v1/", "a//b", ""],
)
def test_http_api_invalid_mapping_key_raises(bad_key, app_context_with_dns):
    with pytest.raises(ValueError, match="api_mapping_key"):
        HttpApi("my-api", domain_name="api.example.com", api_mapping_key=bad_key)


# ---------------------------------------------------------------------------
# Cognito ARN parsing (used by Cognito authorizer with string user_pool)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_arn",
    [
        "not-an-arn",
        "arn:aws:s3:::bucket",  # Wrong service
        "arn:aws:cognito-idp:us-east-1:123:something/else",  # Wrong resource prefix
        "arn:aws:cognito-idp::123:userpool/abc",  # Missing region
        "arn:aws:cognito-idp:us-east-1:123:userpool/",  # Empty pool id
    ],
)
def test_http_api_cognito_authorizer_invalid_user_pool_arn(bad_arn):
    api = HttpApi("my-api")
    with pytest.raises(ValueError, match="user_pool ARN is invalid"):
        api.add_cognito_authorizer("auth", user_pool=bad_arn, audiences=["client"])
