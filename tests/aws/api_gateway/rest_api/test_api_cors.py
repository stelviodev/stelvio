import pulumi
import pytest

from stelvio.aws.api_gateway import RestApi
from stelvio.aws.api_gateway.rest_api.config import CorsConfig, path_to_resource_name
from stelvio.component import resource_name

from ....conftest import TP
from ...conftest import spy_old_names
from ...pulumi_mocks import ROOT_RESOURCE_ID, PulumiTestMocks, tid
from .conftest import when_api_ready
from .test_rest_api import Funcs

STANDARD_HTTP_METHODS = ["DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"]
pytestmark = pytest.mark.usefixtures("project_cwd")


def assert_options_method(mocks: PulumiTestMocks, api_full_name: str, path: str, resource_id: str):
    expected_name = f"{api_full_name}-method-OPTIONS {path}"
    methods = mocks.created_methods(expected_name)
    assert len(methods) == 1
    method = methods[0]

    assert method.inputs["httpMethod"] == "OPTIONS"
    assert method.inputs["authorization"] == "NONE"
    assert method.inputs["restApi"] == tid(api_full_name)
    assert method.inputs["resourceId"] == resource_id

    return method


def assert_options_method_response(
    mocks: PulumiTestMocks, api_full_name: str, path: str, resource_id: str
):
    expected_name = f"{api_full_name}-method-response-OPTIONS {path}"
    method_responses = mocks.created_method_responses(expected_name)
    assert len(method_responses) == 1
    response = method_responses[0]

    assert response.inputs["statusCode"] == "200"
    assert response.inputs["httpMethod"] == "OPTIONS"
    assert response.inputs["restApi"] == tid(api_full_name)
    assert response.inputs["resourceId"] == resource_id

    params = response.inputs["responseParameters"]
    # Check required CORS headers
    assert params["method.response.header.Access-Control-Allow-Origin"] is False
    assert params["method.response.header.Access-Control-Allow-Methods"] is False
    assert params["method.response.header.Access-Control-Allow-Headers"] is False
    # Optional headers may also be present (Max-Age, Allow-Credentials)
    for value in params.values():
        assert value is False  # All MethodResponse parameters should be False

    return response


def assert_options_integration(
    mocks: PulumiTestMocks, api_full_name: str, path: str, resource_id: str
):
    expected_name = f"{api_full_name}-integration-OPTIONS {path}"
    integrations = mocks.created_integrations(expected_name)
    assert len(integrations) == 1
    integration = integrations[0]

    assert integration.inputs["type"] == "MOCK"
    assert integration.inputs["requestTemplates"] == {"application/json": '{"statusCode": 200}'}
    assert integration.inputs["httpMethod"] == "OPTIONS"
    assert integration.inputs["restApi"] == tid(api_full_name)
    assert integration.inputs["resourceId"] == resource_id

    return integration


def assert_options_integration_response(  # noqa: PLR0913
    mocks: PulumiTestMocks,
    api_full_name: str,
    path: str,
    resource_id: str,
    expected_origin: str,
    expected_methods: list[str],
    expected_headers: str,
    max_age: int | None = None,
    allow_credentials: bool = False,
):
    expected_name = f"{api_full_name}-integration-response-OPTIONS {path}"
    integration_responses = mocks.created_integration_responses(expected_name)
    assert len(integration_responses) == 1
    response = integration_responses[0]

    assert response.inputs["statusCode"] == "200"
    assert response.inputs["httpMethod"] == "OPTIONS"
    assert response.inputs["restApi"] == tid(api_full_name)
    assert response.inputs["resourceId"] == resource_id

    params = response.inputs["responseParameters"]
    assert params["method.response.header.Access-Control-Allow-Origin"] == f"'{expected_origin}'"

    methods_header = params["method.response.header.Access-Control-Allow-Methods"]
    actual_methods = set(methods_header.strip("'").split(","))
    assert actual_methods == set(expected_methods)

    assert params["method.response.header.Access-Control-Allow-Headers"] == f"'{expected_headers}'"

    if max_age is not None:
        assert params["method.response.header.Access-Control-Max-Age"] == f"'{max_age}'"
    else:
        assert "method.response.header.Access-Control-Max-Age" not in params

    if allow_credentials:
        assert params["method.response.header.Access-Control-Allow-Credentials"] == "'true'"
    else:
        assert "method.response.header.Access-Control-Allow-Credentials" not in params

    return response


def assert_gateway_responses(
    mocks: PulumiTestMocks,
    api_full_name: str,
    expected_origin: str,
    expose_headers: str | None = None,
    allow_credentials: bool = False,
):
    gateway_responses = mocks.created_gateway_responses()
    expected_names = {
        f"{api_full_name}-gateway-response-cors-default_4xx",
        f"{api_full_name}-gateway-response-cors-default_5xx",
    }
    api_responses = [r for r in gateway_responses if r.name in expected_names]

    if len(api_responses) != 2:
        available = [r.name for r in gateway_responses]
        raise AssertionError(
            f"Expected 2 gateway responses {expected_names}, found {len(api_responses)}. "
            f"Available: {available}"
        )

    response_types = {r.inputs["responseType"] for r in api_responses}
    assert response_types == {"DEFAULT_4XX", "DEFAULT_5XX"}

    for response in api_responses:
        params = response.inputs["responseParameters"]
        assert (
            params["gatewayresponse.header.Access-Control-Allow-Origin"] == f"'{expected_origin}'"
        )

        if expose_headers:
            assert (
                params["gatewayresponse.header.Access-Control-Expose-Headers"]
                == f"'{expose_headers}'"
            )
        else:
            assert "gatewayresponse.header.Access-Control-Expose-Headers" not in params

        if allow_credentials:
            assert params["gatewayresponse.header.Access-Control-Allow-Credentials"] == "'true'"
        else:
            assert "gatewayresponse.header.Access-Control-Allow-Credentials" not in params

    return api_responses


def test_api_rest_api_v1_rejects_list_origins():
    with pytest.raises(ValueError, match="REST API v1 only supports single origin string"):
        RestApi("test-api", cors=CorsConfig(allow_origins=["https://a.com", "https://b.com"]))


def test_api_rest_api_v1_accepts_single_origin():
    api = RestApi("test-api", cors=CorsConfig(allow_origins="https://example.com"))
    assert api.config.normalized_cors is not None
    assert api.config.normalized_cors.allow_origins == "https://example.com"


@pulumi.runtime.test
def test_api_without_cors_creates_no_cors_resources(pulumi_mocks):
    api = RestApi("test-api", cors=False)
    api.route("GET", "/users", handler=Funcs.USERS.handler)

    def check(_):
        options_methods = [
            m for m in pulumi_mocks.created_methods() if m.inputs["httpMethod"] == "OPTIONS"
        ]
        assert len(options_methods) == 0

        gateway_responses = pulumi_mocks.created_gateway_responses()
        assert len(gateway_responses) == 0

    when_api_ready(api, check)


@pulumi.runtime.test
def test_api_cors_true_creates_options_and_gateway_responses(pulumi_mocks):
    api = RestApi("test-api", cors=True)
    api.route("GET", "/users", handler=Funcs.USERS.handler)

    def check(_):
        users_resources = pulumi_mocks.created_api_resources(f"{TP}test-api-resource-/users")
        assert len(users_resources) == 1
        expected_users_res_id = tid(users_resources[0].name)

        assert_options_method(pulumi_mocks, TP + "test-api", "/users", expected_users_res_id)
        assert_options_method_response(
            pulumi_mocks, TP + "test-api", "/users", expected_users_res_id
        )
        assert_options_integration(pulumi_mocks, TP + "test-api", "/users", expected_users_res_id)
        assert_options_integration_response(
            pulumi_mocks,
            TP + "test-api",
            "/users",
            expected_users_res_id,
            expected_origin="*",
            expected_methods=STANDARD_HTTP_METHODS,
            expected_headers="*",
        )
        assert_gateway_responses(pulumi_mocks, TP + "test-api", expected_origin="*")

    when_api_ready(api, check)


@pulumi.runtime.test
def test_api_cors_creates_options_for_each_unique_path(pulumi_mocks):
    api = RestApi("test-api", cors=True)
    api.route("GET", "/users", handler=Funcs.USERS.handler)
    api.route("POST", "/users", handler=Funcs.USERS.handler)
    api.route("GET", "/orders", handler=Funcs.ORDERS.handler)

    def check(_):
        options_methods = [
            m for m in pulumi_mocks.created_methods() if m.inputs["httpMethod"] == "OPTIONS"
        ]
        assert len(options_methods) == 2

        users_resources = pulumi_mocks.created_api_resources(f"{TP}test-api-resource-/users")
        assert len(users_resources) == 1
        expected_users_res_id = tid(users_resources[0].name)

        orders_resources = pulumi_mocks.created_api_resources(f"{TP}test-api-resource-/orders")
        assert len(orders_resources) == 1
        expected_orders_res_id = tid(orders_resources[0].name)

        assert_options_method(pulumi_mocks, TP + "test-api", "/users", expected_users_res_id)
        assert_options_method_response(
            pulumi_mocks, TP + "test-api", "/users", expected_users_res_id
        )
        assert_options_integration(pulumi_mocks, TP + "test-api", "/users", expected_users_res_id)
        assert_options_method(pulumi_mocks, TP + "test-api", "/orders", expected_orders_res_id)
        assert_options_method_response(
            pulumi_mocks, TP + "test-api", "/orders", expected_orders_res_id
        )
        assert_options_integration(
            pulumi_mocks, TP + "test-api", "/orders", expected_orders_res_id
        )

        assert_options_integration_response(
            pulumi_mocks,
            TP + "test-api",
            "/users",
            expected_users_res_id,
            expected_origin="*",
            expected_methods=STANDARD_HTTP_METHODS,
            expected_headers="*",
        )
        assert_options_integration_response(
            pulumi_mocks,
            TP + "test-api",
            "/orders",
            expected_orders_res_id,
            expected_origin="*",
            expected_methods=STANDARD_HTTP_METHODS,
            expected_headers="*",
        )

    when_api_ready(api, check)


@pulumi.runtime.test
def test_api_cors_custom_config_creates_correct_headers(pulumi_mocks):
    api = RestApi(
        "test-api",
        cors=CorsConfig(
            allow_origins="https://example.com",
            allow_methods=["GET", "POST"],
            allow_headers=["Content-Type", "Authorization"],
            allow_credentials=True,
            max_age=3600,
            expose_headers=["X-Request-Id", "X-Custom"],
        ),
    )
    api.route("GET", "/users", handler=Funcs.USERS.handler)
    api.route("POST", "/users", handler=Funcs.USERS.handler)

    def check(_):
        users_resources = pulumi_mocks.created_api_resources(f"{TP}test-api-resource-/users")
        assert len(users_resources) == 1
        expected_users_res_id = tid(users_resources[0].name)

        assert_options_method(pulumi_mocks, TP + "test-api", "/users", expected_users_res_id)
        assert_options_method_response(
            pulumi_mocks, TP + "test-api", "/users", expected_users_res_id
        )
        assert_options_integration(pulumi_mocks, TP + "test-api", "/users", expected_users_res_id)
        assert_options_integration_response(
            pulumi_mocks,
            TP + "test-api",
            "/users",
            expected_users_res_id,
            expected_origin="https://example.com",
            expected_methods=["GET", "POST", "OPTIONS"],
            expected_headers="Content-Type,Authorization",
            max_age=3600,
            allow_credentials=True,
        )
        assert_gateway_responses(
            pulumi_mocks,
            TP + "test-api",
            expected_origin="https://example.com",
            expose_headers="X-Request-Id,X-Custom",
            allow_credentials=True,
        )

    when_api_ready(api, check)


@pulumi.runtime.test
def test_api_cors_methods_limited_to_route_methods(pulumi_mocks):
    api = RestApi(
        "test-api",
        cors=CorsConfig(
            allow_origins="https://example.com",
            allow_methods=["GET", "POST", "PUT", "DELETE"],
        ),
    )
    api.route("GET", "/users", handler=Funcs.USERS.handler)

    def check(_):
        users_resources = pulumi_mocks.created_api_resources(f"{TP}test-api-resource-/users")
        assert len(users_resources) == 1
        expected_users_res_id = tid(users_resources[0].name)

        assert_options_method(pulumi_mocks, TP + "test-api", "/users", expected_users_res_id)
        assert_options_method_response(
            pulumi_mocks, TP + "test-api", "/users", expected_users_res_id
        )
        assert_options_integration(pulumi_mocks, TP + "test-api", "/users", expected_users_res_id)
        assert_options_integration_response(
            pulumi_mocks,
            TP + "test-api",
            "/users",
            expected_users_res_id,
            expected_origin="https://example.com",
            expected_methods=["GET", "OPTIONS"],
            expected_headers="*",
        )

    when_api_ready(api, check)


@pulumi.runtime.test
def test_api_cors_root_path_creates_options_with_root_path(pulumi_mocks):
    api = RestApi("test-api", cors=True)
    api.route("GET", "/", handler=Funcs.SIMPLE.handler)

    def check(_):
        expected_root_res_id = ROOT_RESOURCE_ID

        assert_options_method(pulumi_mocks, TP + "test-api", "/", expected_root_res_id)
        assert_options_method_response(pulumi_mocks, TP + "test-api", "/", expected_root_res_id)
        assert_options_integration(pulumi_mocks, TP + "test-api", "/", expected_root_res_id)
        assert_options_integration_response(
            pulumi_mocks,
            TP + "test-api",
            "/",
            expected_root_res_id,
            expected_origin="*",
            expected_methods=STANDARD_HTTP_METHODS,
            expected_headers="*",
        )

    when_api_ready(api, check)


@pulumi.runtime.test
def test_api_cors_nested_path_includes_all_parts_in_name(pulumi_mocks):
    api = RestApi("test-api", cors=True)
    api.route("GET", "/users/{id}/orders", handler=Funcs.USERS.handler)

    def check(_):
        orders_resources = pulumi_mocks.created_api_resources(
            f"{TP}test-api-resource-/users/{{id}}/orders"
        )
        assert len(orders_resources) == 1
        expected_orders_res_id = tid(orders_resources[0].name)

        assert_options_method(
            pulumi_mocks, TP + "test-api", "/users/{id}/orders", expected_orders_res_id
        )
        assert_options_method_response(
            pulumi_mocks, TP + "test-api", "/users/{id}/orders", expected_orders_res_id
        )
        assert_options_integration(
            pulumi_mocks, TP + "test-api", "/users/{id}/orders", expected_orders_res_id
        )
        assert_options_integration_response(
            pulumi_mocks,
            TP + "test-api",
            "/users/{id}/orders",
            expected_orders_res_id,
            expected_origin="*",
            expected_methods=STANDARD_HTTP_METHODS,
            expected_headers="*",
        )

    when_api_ready(api, check)


@pulumi.runtime.test
def test_multiple_apis_with_cors_create_uniquely_named_resources(pulumi_mocks):
    api1 = RestApi("api1", cors=True)
    api1.route("GET", "/users", handler=Funcs.USERS.handler)

    api2 = RestApi("api2", cors=True)
    api2.route("GET", "/users", handler=Funcs.USERS.handler)

    def check(_):
        gateway_responses = pulumi_mocks.created_gateway_responses()
        assert len(gateway_responses) == 4

        response_names = {r.name for r in gateway_responses}
        assert f"{TP}api1-gateway-response-cors-default_4xx" in response_names
        assert f"{TP}api1-gateway-response-cors-default_5xx" in response_names
        assert f"{TP}api2-gateway-response-cors-default_4xx" in response_names
        assert f"{TP}api2-gateway-response-cors-default_5xx" in response_names

        options_methods = [
            m for m in pulumi_mocks.created_methods() if m.inputs["httpMethod"] == "OPTIONS"
        ]
        assert len(options_methods) == 2

        method_names = {m.name for m in options_methods}
        assert f"{TP}api1-method-OPTIONS /users" in method_names
        assert f"{TP}api2-method-OPTIONS /users" in method_names

    pulumi.Output.all(api1.resources.stage.id, api2.resources.stage.id).apply(check)


@pulumi.runtime.test
def test_api_cors_options_resources_alias_their_old_names(pulumi_mocks, monkeypatch):
    """The four OPTIONS resources per path keep their pre-rename names as aliases, `root`
    standing in for `/` as it did then, and long names truncated with the same
    `resource_name(..., limit=128)` hash the old builder applied."""
    old_names = spy_old_names(monkeypatch, RestApi)
    long_parts = [
        "organizations-departments-employees-timesheets",
        "reports-quarterly-summary-by-region-and-team",
    ]
    api = RestApi("test-api", cors=True)
    api.route("GET", "/users/{id}", handler=Funcs.SIMPLE.handler)
    api.route("GET", "/", handler=Funcs.SIMPLE.handler)
    api.route("GET", "/" + "/".join(long_parts), handler=Funcs.SIMPLE.handler)

    def check(_):
        legacy_paths = ("users-id", "root", path_to_resource_name(long_parts))
        assert {n for n in old_names if "OPTIONS" in n} == {
            resource_name(f"test-api-{kind}-OPTIONS-{path}", limit=128)
            for kind in ("method", "method-response", "integration", "integration-response")
            for path in legacy_paths
        }

    when_api_ready(api, check)


@pulumi.runtime.test
def test_api_cors_trailing_slash_shares_one_options_set(pulumi_mocks):
    """`/users/` and `/users` are one AWS resource, so they get one OPTIONS set named after
    the canonical path and advertising both routes' verbs, whichever was declared first."""
    api = RestApi("test-api", cors=CorsConfig(allow_methods=["GET", "POST"]))
    api.route("GET", "/users/", handler=Funcs.SIMPLE.handler)
    api.route("POST", "/users", handler=Funcs.USERS.handler)

    def check(_):
        options = [
            m for m in pulumi_mocks.created_methods() if m.inputs["httpMethod"] == "OPTIONS"
        ]
        assert len(options) == 1
        users = pulumi_mocks.created_api_resources(f"{TP}test-api-resource-/users")
        assert_options_integration_response(
            pulumi_mocks,
            TP + "test-api",
            "/users",
            tid(users[0].name),
            expected_origin="*",
            expected_methods=["GET", "POST", "OPTIONS"],
            expected_headers="*",
        )

    when_api_ready(api, check)
