"""Unit tests for HttpApi component (API Gateway v2)."""

import json
from dataclasses import dataclass, field, replace
from typing import Any

import pulumi
from pytest import mark, raises

from stelvio.aws.api_gateway import ApiDomain, HttpApi, HttpApiConfig, HttpApiConfigDict
from stelvio.aws.api_gateway.http_api.http_api import _ACCESS_LOG_FORMAT
from stelvio.aws.api_gateway.methods import HTTPMethod
from stelvio.aws.api_gateway.rest_api.constants import (
    API_GATEWAY_LOGS_POLICY,
    API_GATEWAY_ROLE_NAME,
)
from stelvio.aws.cors import CorsConfig
from stelvio.aws.function import Function, FunctionConfig
from stelvio.config import AwsConfig
from stelvio.context import AppContext, _ContextStore
from tests.test_utils import assert_config_dict_matches_dataclass

from ...pulumi_mocks import ACCOUNT_ID, DEFAULT_REGION, R, tid, tn
from .conftest import HTTP_API_ID, LAMBDA_INVOKE_ARN_TEMPLATE, TP, when_http_api_ready

pytestmark = mark.usefixtures("project_cwd")
LAMBDA_ASSUME_ROLE_POLICY = [
    {
        "actions": ["sts:AssumeRole"],
        "principals": [{"identifiers": ["lambda.amazonaws.com"], "type": "Service"}],
    }
]
API_GATEWAY_ASSUME_ROLE_POLICY = [
    {
        "actions": ["sts:AssumeRole"],
        "principals": [{"identifiers": ["apigateway.amazonaws.com"], "type": "Service"}],
    }
]


@dataclass(frozen=True)
class RouteSpec:
    method: str
    path: str
    handler: str
    route_key: str
    route_name: str
    function_name: str


@dataclass(frozen=True)
class FunctionSpec:
    name: str
    handler: str
    memory: int = 128
    timeout: int = 60


@dataclass(frozen=True)
class HttpApiTestCase:
    test_id: str
    routes: list[RouteSpec] = field(default_factory=list)
    functions: list[FunctionSpec] = field(default_factory=list)
    stage_name: str = "$default"
    cors: bool | CorsConfig | dict[str, Any] = False
    expected_cors: dict[str, Any] | None = None
    access_log_retention_days: int | str = 30


SIMPLE_FUNCTION = FunctionSpec("my-api-functions-simple_handler", "simple.handler")
USERS_FUNCTION = FunctionSpec("my-api-functions-users_handler", "users.handler")
DEFAULT_TC = HttpApiTestCase(
    test_id="different-handlers",
    routes=[
        RouteSpec(
            "GET",
            "/users",
            "functions/simple.handler",
            "GET /users",
            "my-api-route-GET--users",
            SIMPLE_FUNCTION.name,
        ),
        RouteSpec(
            "POST",
            "/orders",
            "functions/users.handler",
            "POST /orders",
            "my-api-route-POST--orders",
            USERS_FUNCTION.name,
        ),
    ],
    functions=[SIMPLE_FUNCTION, USERS_FUNCTION],
)
EMPTY_TC = HttpApiTestCase(test_id="empty")
SHARED_HANDLER_TC = HttpApiTestCase(
    test_id="shared-handler",
    routes=[
        RouteSpec(
            "GET",
            "/users",
            "functions/simple.handler",
            "GET /users",
            "my-api-route-GET--users",
            SIMPLE_FUNCTION.name,
        ),
        RouteSpec(
            "POST",
            "/users",
            "functions/simple.handler",
            "POST /users",
            "my-api-route-POST--users",
            SIMPLE_FUNCTION.name,
        ),
    ],
    functions=[SIMPLE_FUNCTION],
)
NAMED_STAGE_TC = replace(DEFAULT_TC, test_id="named-stage", stage_name="v2")
CORS_TC = replace(
    DEFAULT_TC,
    test_id="cors",
    cors=True,
    expected_cors={"allowOrigins": ["*"], "allowMethods": ["*"], "allowHeaders": ["*"]},
)
CUSTOM_CORS_TC = replace(
    DEFAULT_TC,
    test_id="custom-cors",
    cors=CorsConfig(
        allow_origins=["https://app.example.com", "https://admin.example.com"],
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "Authorization"],
        allow_credentials=True,
        max_age=3600,
        expose_headers=["X-Request-Id"],
    ),
    expected_cors={
        "allowOrigins": ["https://app.example.com", "https://admin.example.com"],
        "allowMethods": ["GET", "POST"],
        "allowHeaders": ["Content-Type", "Authorization"],
        "allowCredentials": True,
        "maxAge": 3600,
        "exposeHeaders": ["X-Request-Id"],
    },
)
CUSTOM_RETENTION_TC = replace(
    DEFAULT_TC,
    test_id="custom-retention",
    access_log_retention_days=3653,
)
FOREVER_RETENTION_TC = replace(
    DEFAULT_TC,
    test_id="retention-forever",
    access_log_retention_days="forever",
)
HTTP_API_CASES = [
    DEFAULT_TC,
    EMPTY_TC,
    SHARED_HANDLER_TC,
    NAMED_STAGE_TC,
    CORS_TC,
    CUSTOM_CORS_TC,
    CUSTOM_RETENTION_TC,
    FOREVER_RETENTION_TC,
]


def verify_http_api(mocks, case: HttpApiTestCase) -> None:
    api_id = HTTP_API_ID
    api_inputs: dict[str, Any] = {
        "disableExecuteApiEndpoint": False,
        "protocolType": "HTTP",
    }
    if case.expected_cors is not None:
        api_inputs["corsConfiguration"] = case.expected_cors
    mocks.assert_res("my-api", R.HTTP_API, api_inputs)

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

    log_group_inputs: dict[str, Any] = {"name": f"/aws/apigateway/{HTTP_API_ID}"}
    if case.access_log_retention_days != "forever":
        log_group_inputs["retentionInDays"] = float(case.access_log_retention_days)
    mocks.assert_res("my-api-logs", R.LOG_GROUP, log_group_inputs)
    mocks.assert_res(
        "my-api-stage",
        R.HTTP_API_STAGE,
        {
            "name": case.stage_name,
            "accessLogSettings": {
                "format": _ACCESS_LOG_FORMAT,
                "destinationArn": (
                    f"arn:aws:logs:{DEFAULT_REGION}:{ACCOUNT_ID}:log-group:"
                    f"{tn(TP + 'my-api-logs')}:*"
                ),
            },
            "autoDeploy": True,
            "apiId": api_id,
        },
    )

    functions_by_name = {function.name: function for function in case.functions}
    for function in case.functions:
        mocks.assert_res(
            function.name,
            R.FUNCTION,
            {
                "handler": function.handler,
                "memorySize": float(function.memory),
                "timeout": float(function.timeout),
            },
            partial=True,
        )
        role_name = f"{function.name}-r"
        mocks.assert_res(
            role_name,
            R.ROLE,
            {"assumeRolePolicy": json.dumps(LAMBDA_ASSUME_ROLE_POLICY)},
        )
        mocks.assert_res(
            f"{function.name}-basic-execution-r-p-attachment",
            R.ROLE_POLICY_ATTACHMENT,
            {
                "policyArn": "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
                "role": tn(TP + role_name),
            },
        )
        mocks.assert_res(
            f"my-api-integration-{function.name}",
            R.HTTP_API_INTEGRATION,
            {
                "payloadFormatVersion": "2.0",
                "timeoutMilliseconds": 30000.0,
                "integrationType": "AWS_PROXY",
                "integrationUri": LAMBDA_INVOKE_ARN_TEMPLATE.format(
                    function_name=tn(TP + function.name)
                ),
                "integrationMethod": "POST",
                "apiId": api_id,
            },
        )
        mocks.assert_res(
            f"my-api-permission-{function.name}",
            R.LAMBDA_PERMISSION,
            {
                "function": tn(TP + function.name),
                "principal": "apigateway.amazonaws.com",
                "sourceArn": (
                    f"arn:aws:execute-api:{DEFAULT_REGION}:{ACCOUNT_ID}:{HTTP_API_ID}/*/*"
                ),
                "action": "lambda:InvokeFunction",
            },
        )

    for route in case.routes:
        assert route.function_name in functions_by_name
        integration_name = f"my-api-integration-{route.function_name}"
        mocks.assert_res(
            route.route_name,
            R.HTTP_API_ROUTE,
            {
                "target": f"integrations/{tid(TP + integration_name)}",
                "routeKey": route.route_key,
                "authorizationType": "NONE",
                "apiId": api_id,
            },
        )

    function_count = len(case.functions)
    counts = {
        R.HTTP_API: 1,
        R.API_ACCOUNT: 2,
        R.ROLE: function_count + 1,
        R.LOG_GROUP: 1,
        R.HTTP_API_STAGE: 1,
    }
    if function_count:
        counts |= {
            R.ROLE_POLICY_ATTACHMENT: function_count,
            R.FUNCTION: function_count,
            R.HTTP_API_INTEGRATION: function_count,
            R.LAMBDA_PERMISSION: function_count,
        }
    if case.routes:
        counts[R.HTTP_API_ROUTE] = len(case.routes)
    mocks.assert_res_counts(counts)


def test_http_api_config_dict_matches_http_api_config():
    assert_config_dict_matches_dataclass(HttpApiConfig, HttpApiConfigDict)


def test_http_api_rejects_invalid_config_type():
    with raises(TypeError, match="Invalid config type"):
        HttpApi("my-api", config=123)  # type: ignore[arg-type]


@mark.parametrize("case", HTTP_API_CASES, ids=lambda case: case.test_id)
@pulumi.runtime.test
def test_http_api_resource_graph(pulumi_mocks, case):
    api = HttpApi(
        "my-api",
        stage_name=case.stage_name,
        cors=case.cors,
        access_log_retention_days=case.access_log_retention_days,
    )
    for route in case.routes:
        api.route(route.method, route.path, route.handler)
    _ = api.resources

    def check(_):
        verify_http_api(pulumi_mocks, case)

    when_http_api_ready(api, check)


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
        pulumi_mocks.assert_no_res(R.POLICY, R.ROLE_POLICY)

    pulumi.Output.all(api.url, api.execution_arn, fn.resources.function.id).apply(check)


@pulumi.runtime.test
def test_multiple_apis_with_same_routes_coexist_with_unique_resource_names(pulumi_mocks):
    """Two HTTP APIs with identical route structures produce no resource-name collisions.

    Permissions use safe_name(..., 100) which truncates long names, so coexistence
    with identical routes is the regression-prone case.
    """
    api1 = HttpApi("user-api", cors=True)
    api1.route("GET", "/users", "functions/simple.handler")
    api1.route("POST", "/users", "functions/users.handler")

    api2 = HttpApi("admin-api")
    api2.route("GET", "/users", "functions/simple.handler")
    api2.route("POST", "/users", "functions/users.handler")

    def expected_names(api_slug: str) -> dict[str, set[str]]:
        prefix = TP + api_slug
        return {
            "routes": {
                f"{prefix}-route-GET--users",
                f"{prefix}-route-POST--users",
            },
            "integrations": {
                f"{prefix}-integration-{api_slug}-functions-simple_handler",
                f"{prefix}-integration-{api_slug}-functions-users_handler",
            },
            "functions": {
                f"{prefix}-functions-simple_handler",
                f"{prefix}-functions-users_handler",
            },
            "permissions": {
                f"{prefix}-permission-{api_slug}-functions-simple_handler",
                f"{prefix}-permission-{api_slug}-functions-users_handler",
            },
        }

    def check(_):
        apis = pulumi_mocks.created(R.HTTP_API)
        assert {a.name for a in apis} == {TP + "user-api", TP + "admin-api"}

        all_names = [r.name for r in pulumi_mocks.created_resources]
        assert len(all_names) == len(set(all_names)), "Resource names collide across APIs"

        user = expected_names("user-api")
        admin = expected_names("admin-api")
        assert {r.name for r in pulumi_mocks.created(R.HTTP_API_ROUTE)} == (
            user["routes"] | admin["routes"]
        )
        assert {r.name for r in pulumi_mocks.created(R.HTTP_API_INTEGRATION)} == (
            user["integrations"] | admin["integrations"]
        )
        assert {r.name for r in pulumi_mocks.created(R.FUNCTION)} == (
            user["functions"] | admin["functions"]
        )
        assert {r.name for r in pulumi_mocks.created(R.LAMBDA_PERMISSION)} == (
            user["permissions"] | admin["permissions"]
        )

    when_http_api_ready([api1, api2], check)


# ---------------------------------------------------------------------------
# Route keys
# ---------------------------------------------------------------------------


@mark.parametrize(
    ("method", "path", "expected_route_keys"),
    [
        ("get", "/health", {"GET /health"}),
        (HTTPMethod.GET, "/health", {"GET /health"}),
        ("ANY", "/health", {"ANY /health"}),
        ("*", "/health", {"ANY /health"}),
        ("ANY", "$default", {"$default"}),
        (["GET", "DELETE"], "/users/{id}", {"GET /users/{id}", "DELETE /users/{id}"}),
        (["get", HTTPMethod.POST], "/users", {"GET /users", "POST /users"}),
    ],
    ids=[
        "lowercase",
        "enum",
        "any",
        "star_normalized_to_any",
        "default",
        "multiple_methods",
        "mixed_methods",
    ],
)
@pulumi.runtime.test
def test_http_api_route_keys(pulumi_mocks, method, path, expected_route_keys):
    api = HttpApi("my-api")
    api.route(method, path, "functions/simple.handler")
    _ = api.resources

    def check(_):
        routes = pulumi_mocks.created(R.HTTP_API_ROUTE)
        assert {route.inputs["routeKey"] for route in routes} == expected_route_keys

    when_http_api_ready(api, check)


@pulumi.runtime.test
def test_http_api_customize_applies_to_resources(pulumi_mocks, app_context_with_dns):
    api = HttpApi(
        "my-api",
        domain_name="api.example.com",
        customize={
            "api": {"description": "Custom API description"},
            "stage": {"description": "Custom stage description"},
            "log_group": {"retention_in_days": 90},
            "api_mapping": {"api_mapping_key": "custom"},
        },
    )
    api.route("GET", "/users", "functions/simple.handler")
    _ = api.resources

    def check(_):
        pulumi_mocks.assert_res(
            "my-api",
            R.HTTP_API,
            {"description": "Custom API description"},
            partial=True,
        )
        pulumi_mocks.assert_res(
            "my-api-stage",
            R.HTTP_API_STAGE,
            {"description": "Custom stage description"},
            partial=True,
        )
        pulumi_mocks.assert_res(
            "my-api-logs",
            R.LOG_GROUP,
            {"retentionInDays": 90.0},
            partial=True,
        )
        pulumi_mocks.assert_res(
            "my-api-api-mapping",
            R.HTTP_API_MAPPING,
            {"apiMappingKey": "custom"},
            partial=True,
        )

    when_http_api_ready(api, check)


# ---------------------------------------------------------------------------
# Validation errors
# ---------------------------------------------------------------------------


def _duplicate_route_key() -> None:
    api = HttpApi("my-api")
    api.route("GET", "/users", "functions/simple.handler")
    api.route("GET", "/users", "functions/users.handler")


@mark.parametrize(
    ("action", "expected_error"),
    [
        (
            lambda: HttpApi(
                "my-api",
                cors=CorsConfig(allow_origins="*", allow_credentials=True),
            ),
            "allow_credentials",
        ),
        (
            lambda: HttpApi("my-api", config=HttpApiConfig(), domain_name="example.com"),
            "cannot combine",
        ),
        (lambda: HttpApi("my-api", api_mapping_key="v1"), "api_mapping_key requires"),
        (
            lambda: HttpApi("my-api", disable_execute_api_endpoint=True),
            "disable_execute_api_endpoint",
        ),
        (_duplicate_route_key, r"[Dd]uplicate"),
        (lambda: HttpApi("my-api", stage_name="$bad"), "Stage name"),
        (lambda: HttpApi("my-api", access_log_retention_days=999), "access_log_retention_days"),
        (lambda: HttpApi("my-api", domain_name=""), "Domain name cannot be empty"),
        (lambda: HttpApi("my-api", domain_name="   "), "Domain name cannot be empty"),
        (
            lambda: HttpApi("my-api", stage_name="with spaces"),
            "Stage name must contain only",
        ),
        (
            lambda: HttpApi("my-api", stage_name="x" * 129),
            "Stage name must be at most 128 characters",
        ),
        (
            lambda: HttpApi("my-api", domain_name="api.example.com", domain=object()),
            "Cannot specify both",
        ),
    ],
    ids=[
        "cors_credentials_wildcard",
        "config_and_opts",
        "mapping_key_without_domain",
        "disable_execute_without_domain",
        "duplicate_route_key",
        "invalid_stage_name",
        "invalid_log_retention",
        "empty_domain",
        "whitespace_domain",
        "stage_name_spaces",
        "stage_name_too_long",
        "domain_name_and_domain",
    ],
)
def test_http_api_rejects_invalid_configuration(action, expected_error):
    with raises(ValueError, match=expected_error):
        action()


@mark.parametrize("domain_name", [123, [], {}, True])
def test_http_api_rejects_invalid_domain_name_type(domain_name):
    with raises(TypeError, match="Domain name must be a string"):
        HttpApi("my-api", domain_name=domain_name)  # type: ignore[arg-type]


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
        # Lambda keeps the requested 60s; the integration stays at the API Gateway cap.
        pulumi_mocks.assert_res(
            "my-api-functions-simple_handler", R.FUNCTION, {"timeout": 60.0}, partial=True
        )
        pulumi_mocks.assert_res(
            "my-api-integration-my-api-functions-simple_handler",
            R.HTTP_API_INTEGRATION,
            {"timeoutMilliseconds": 30000.0},
            partial=True,
        )

    when_http_api_ready(api, check)


# ---------------------------------------------------------------------------
# disable_execute_api_endpoint
# ---------------------------------------------------------------------------


@pulumi.runtime.test
def test_http_api_disable_execute_api_endpoint(pulumi_mocks, app_context_with_dns):
    api = HttpApi("my-api", domain_name="api.example.com", disable_execute_api_endpoint=True)
    api.route("GET", "/users", "functions/simple.handler")
    _ = api.resources

    def check(_):
        pulumi_mocks.assert_res(
            "my-api",
            R.HTTP_API,
            {"disableExecuteApiEndpoint": True},
            partial=True,
        )

    when_http_api_ready(api, check)


@pulumi.runtime.test
def test_http_api_disable_execute_api_endpoint_with_shared_domain(
    pulumi_mocks, app_context_with_dns
):
    domain = ApiDomain("shared", domain_name="api.example.com")
    api = HttpApi("my-api", domain=domain, disable_execute_api_endpoint=True)
    api.route("GET", "/users", "functions/simple.handler")
    _ = api.resources

    def check(_):
        pulumi_mocks.assert_res(
            "my-api",
            R.HTTP_API,
            {"disableExecuteApiEndpoint": True},
            partial=True,
        )

    when_http_api_ready(api, check)


# ---------------------------------------------------------------------------
# url property
# ---------------------------------------------------------------------------


@pulumi.runtime.test
def test_http_api_url_default_stage_execute_api(pulumi_mocks):
    api = HttpApi("my-api")
    api.route("GET", "/users", "functions/simple.handler")

    def check(url):
        assert url == f"https://{HTTP_API_ID}.execute-api.us-east-1.amazonaws.com"

    api.url.apply(check)


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

        def check(url):
            assert url == f"https://{HTTP_API_ID}.execute-api.us-east-1.amazonaws.com"

        api.url.apply(check)
    finally:
        _ContextStore.clear()
        _ContextStore.set(saved)


@pulumi.runtime.test
def test_http_api_url_named_stage_execute_api(pulumi_mocks):
    api = HttpApi("my-api", stage_name="prod")
    api.route("GET", "/users", "functions/simple.handler")

    def check(url):
        assert url == f"https://{HTTP_API_ID}.execute-api.us-east-1.amazonaws.com/prod"

    api.url.apply(check)


@pulumi.runtime.test
def test_http_api_url_with_domain_name(pulumi_mocks, app_context_with_dns):
    api = HttpApi("my-api", domain_name="api.example.com")
    api.route("GET", "/users", "functions/simple.handler")

    def check(url):
        assert url == "https://api.example.com"

    api.url.apply(check)


@pulumi.runtime.test
def test_http_api_url_with_domain_allows_adding_routes_after(pulumi_mocks, app_context_with_dns):
    # With a domain the url is known upfront, so reading it must not create resources —
    # otherwise passing api.url to another component locks the api before all routes
    # are added. Adding a route after is what fails if the fallback is evaluated eagerly.
    api = HttpApi("my-api", domain_name="api.example.com")
    url = api.url
    api.route("GET", "/users", "functions/simple.handler")

    def check_route_created(_):
        assert len(pulumi_mocks.created(R.HTTP_API_ROUTE)) == 1

    def check_url(resolved):
        assert resolved == "https://api.example.com"

    when_http_api_ready(api, check_route_created)
    url.apply(check_url)


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

    assert api.domain_name == "api.example.com"


# ---------------------------------------------------------------------------
# Access log retention boundaries
# ---------------------------------------------------------------------------


@mark.parametrize(
    "days",
    [
        1,
        3,
        5,
        7,
        14,
        30,
        60,
        90,
        120,
        150,
        180,
        365,
        400,
        545,
        731,
        1096,
        1827,
        2192,
        2557,
        2922,
        3288,
        3653,
    ],
)
def test_http_api_access_log_retention_valid_boundaries(days):
    HttpApi("my-api", access_log_retention_days=days)


@mark.parametrize("days", [0, 2, 9999])
def test_http_api_access_log_retention_invalid_boundaries(days):
    with raises(ValueError, match="access_log_retention_days"):
        HttpApi("my-api", access_log_retention_days=days)


# ---------------------------------------------------------------------------
# api_mapping_key validation
# ---------------------------------------------------------------------------


@mark.parametrize(
    "bad_key",
    ["/v1", "v1/", "a//b", ""],
)
def test_http_api_invalid_mapping_key_raises(bad_key, app_context_with_dns):
    with raises(ValueError, match="api_mapping_key"):
        HttpApi("my-api", domain_name="api.example.com", api_mapping_key=bad_key)
