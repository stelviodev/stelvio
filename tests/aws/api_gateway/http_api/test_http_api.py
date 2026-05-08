"""Unit tests for HttpApi component (API Gateway v2)."""

import pulumi
import pytest

from stelvio.aws.api_gateway.http_api import HttpApi, HttpApiConfig
from stelvio.aws.function import Function, FunctionConfig

from .conftest import when_http_api_ready

pytestmark = pytest.mark.usefixtures("project_cwd")


# ---------------------------------------------------------------------------
# Basic creation
# ---------------------------------------------------------------------------


@pulumi.runtime.test
def test_http_api_creates_api_resource(pulumi_mocks):
    api = HttpApi("my-api")
    api.route("GET", "/users", "functions/simple.handler")
    _ = api.resources

    def check(_):
        apis = pulumi_mocks.created_http_apis()
        assert len(apis) == 1
        assert apis[0].typ == "aws:apigatewayv2/api:Api"
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
        assert stages[0].inputs["autoDeploy"] is True
        assert stages[0].inputs["name"] == "$default"

    api.resources.stage.id.apply(check)


@pulumi.runtime.test
def test_http_api_creates_log_group(pulumi_mocks):
    api = HttpApi("my-api")
    api.route("GET", "/users", "functions/simple.handler")
    _ = api.resources

    def check(_):
        log_groups = pulumi_mocks.created_log_groups()
        assert len(log_groups) >= 1
        assert any(lg.inputs.get("retentionInDays") == 30 for lg in log_groups)

    api.resources.log_group.arn.apply(check)


@pulumi.runtime.test
def test_http_api_arn_property(pulumi_mocks):
    api = HttpApi("my-api")
    api.route("GET", "/users", "functions/simple.handler")

    def check(arn):
        assert arn.startswith("arn:aws:apigateway:us-east-1::/apis/")

    api.arn.apply(check)


@pulumi.runtime.test
def test_http_api_link_injects_api_url_env_vars(pulumi_mocks):
    api = HttpApi("orders-api")
    api.route("GET", "/orders", "functions/simple.handler")
    fn = Function("client", handler="functions/simple.handler", links=[api])

    def check(_):
        functions = pulumi_mocks.created_functions()
        client_fn = next(f for f in functions if f.name == "test-test-client")
        env_vars = client_fn.inputs["environment"]["variables"]
        assert "STLV_ORDERS_API_API_URL" in env_vars
        assert "STLV_ORDERS_API_API_EXECUTION_ARN" in env_vars

    pulumi.Output.all(fn.resources.function.id, api.resources.stage.id).apply(check)


@pulumi.runtime.test
def test_http_api_creates_function_for_route(pulumi_mocks):
    """A Lambda function is created for the route handler."""
    api = HttpApi("my-api")
    api.route("GET", "/users", "functions/simple.handler")
    _ = api.resources

    def check(_):
        fns = pulumi_mocks.created_functions()
        assert len(fns) >= 1

    when_http_api_ready(api, check)


@pulumi.runtime.test
def test_http_api_creates_integration(pulumi_mocks):
    api = HttpApi("my-api")
    api.route("GET", "/users", "functions/simple.handler")
    _ = api.resources

    def check(_):
        all_types = [r.typ for r in pulumi_mocks.created_resources]
        integrations = pulumi_mocks.created_http_api_integrations()
        assert "aws:apigatewayv2/integration:Integration" in all_types, (
            f"Integration not found in resource types: {all_types}"
        )
        assert len(integrations) == 1
        assert integrations[0].inputs["integrationType"] == "AWS_PROXY"
        assert integrations[0].inputs["integrationMethod"] == "POST"
        assert integrations[0].inputs["payloadFormatVersion"] == "2.0"
        assert integrations[0].inputs["timeoutMilliseconds"] == 30000

    when_http_api_ready(api, check)


@pulumi.runtime.test
def test_http_api_creates_route(pulumi_mocks):
    api = HttpApi("my-api")
    api.route("GET", "/users", "functions/simple.handler")
    _ = api.resources

    def check(_):
        routes = pulumi_mocks.created_http_api_routes()
        assert len(routes) == 1
        assert routes[0].inputs["routeKey"] == "GET /users"
        assert routes[0].inputs["authorizationType"] == "NONE"

    when_http_api_ready(api, check)


@pulumi.runtime.test
def test_http_api_creates_lambda_permission(pulumi_mocks):
    api = HttpApi("my-api")
    api.route("GET", "/users", "functions/simple.handler")
    _ = api.resources

    def check(_):
        perms = pulumi_mocks.created_permissions()
        assert len(perms) >= 1
        route_perms = [p for p in perms if "/*/*" in str(p.inputs.get("sourceArn", ""))]
        assert len(route_perms) >= 1
        assert any(p.inputs["action"] == "lambda:InvokeFunction" for p in route_perms)

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

        functions = pulumi_mocks.created_functions()
        assert len([f for f in functions if "simple" in f.name.lower()]) == 1

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

    when_http_api_ready(api, check)


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
        assert "GET /users/{id}" in route_keys
        assert "DELETE /users/{id}" in route_keys

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
        assert cors is not None
        assert "*" in cors["allowOrigins"]
        assert "*" in cors["allowMethods"]

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


def test_http_api_default_route_requires_any_method():
    from stelvio.aws.api_gateway.http_api._routes import _validate_method_for_http_api

    with pytest.raises(ValueError, match=r"\$default"):
        _validate_method_for_http_api("GET", "$default")


def test_http_api_invalid_stage_name_raises():
    with pytest.raises(ValueError, match="Stage name"):
        HttpApi("my-api", stage_name="$bad")


def test_http_api_invalid_log_retention_raises():
    with pytest.raises(ValueError, match="access_log_retention_days"):
        HttpApi("my-api", access_log_retention_days=999)


def test_http_api_lambda_timeout_over_30_raises():
    api = HttpApi("my-api")
    api.route(
        "GET",
        "/slow",
        FunctionConfig(handler="functions/simple.handler", timeout=60),
    )

    with pytest.raises(ValueError, match="timeout=60s"):
        _ = api.resources


# ---------------------------------------------------------------------------
# Access log retention
# ---------------------------------------------------------------------------


@pulumi.runtime.test
def test_http_api_access_log_retention_none(pulumi_mocks):
    api = HttpApi("my-api", access_log_retention_days=None)
    api.route("GET", "/users", "functions/simple.handler")
    _ = api.resources

    def check(_):
        log_groups = pulumi_mocks.created_log_groups()
        assert len(log_groups) >= 1
        assert all("retentionInDays" not in lg.inputs for lg in log_groups)

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
