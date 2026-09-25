import re
from collections import Counter

import pulumi
from pytest import mark, param, raises

from stelvio.aws.api_gateway import RestApi
from stelvio.aws.api_gateway.rest_api.config import CorsConfig, path_to_resource_name
from stelvio.aws.function import Function
from stelvio.component import resource_name

from ....conftest import TP
from ...conftest import spy_old_names
from ...pulumi_mocks import ROOT_RESOURCE_ID, PulumiTestMocks, R, tid
from .test_rest_api import Funcs, rest_api_counts

pytestmark = mark.usefixtures("project_cwd")

ALL_METHODS = "DELETE,GET,HEAD,OPTIONS,PATCH,POST,PUT"
WILDCARD_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": ALL_METHODS,
    "Access-Control-Allow-Headers": "*",
}


def cors_counts(paths: int, *, apis: int = 1) -> Counter[R]:
    """Two gateway responses per API plus a four-resource OPTIONS set per path."""
    return Counter(
        {
            R.API_GATEWAY_RESPONSE: 2 * apis,
            R.API_METHOD: paths,
            R.API_METHOD_RESPONSE: paths,
            R.API_INTEGRATION: paths,
            R.API_INTEGRATION_RESPONSE: paths,
        }
    )


def assert_options_set(
    mocks: PulumiTestMocks, api_name: str, path: str, headers: dict[str, str]
) -> None:
    """One preflight: the OPTIONS method, its 200 response declaring the headers, the MOCK
    integration, and the integration response carrying the header values."""
    resource_id = ROOT_RESOURCE_ID if path == "/" else tid(f"{TP}{api_name}-resource-{path}")
    common = {"restApi": tid(TP + api_name), "resourceId": resource_id, "httpMethod": "OPTIONS"}
    mocks.assert_res(
        f"{api_name}-method-OPTIONS {path}", R.API_METHOD, {**common, "authorization": "NONE"}
    )
    mocks.assert_res(
        f"{api_name}-method-response-OPTIONS {path}",
        R.API_METHOD_RESPONSE,
        {
            **common,
            "statusCode": "200",
            "responseParameters": {f"method.response.header.{h}": False for h in headers},
        },
    )
    mocks.assert_res(
        f"{api_name}-integration-OPTIONS {path}",
        R.API_INTEGRATION,
        {
            **common,
            "type": "MOCK",
            "requestTemplates": {"application/json": '{"statusCode": 200}'},
        },
    )
    mocks.assert_res(
        f"{api_name}-integration-response-OPTIONS {path}",
        R.API_INTEGRATION_RESPONSE,
        {
            **common,
            "statusCode": "200",
            "responseParameters": {
                f"method.response.header.{h}": f"'{v}'" for h, v in headers.items()
            },
        },
    )


def assert_gateway_responses(
    mocks: PulumiTestMocks, api_name: str, headers: dict[str, str]
) -> None:
    """4XX and 5XX gateway responses carry CORS headers so browsers can read the error."""
    for response_type in ("DEFAULT_4XX", "DEFAULT_5XX"):
        mocks.assert_res(
            f"{api_name}-gateway-response-cors-{response_type.lower()}",
            R.API_GATEWAY_RESPONSE,
            {
                "restApiId": tid(TP + api_name),
                "responseType": response_type,
                "responseParameters": {
                    f"gatewayresponse.header.{h}": f"'{v}'" for h, v in headers.items()
                },
            },
        )


def test_api_rest_api_v1_rejects_list_origins():
    with raises(ValueError, match="REST API v1 only supports single origin string"):
        RestApi("test-api", cors=CorsConfig(allow_origins=["https://a.com", "https://b.com"]))


def test_api_without_cors_creates_no_cors_resources(pulumi_mocks):
    api = RestApi("test-api", cors=False)
    api.route("GET", "/users", handler=Funcs.USERS.handler)

    @pulumi.runtime.test
    def deploy():
        return api.resources

    deploy()

    pulumi_mocks.assert_res_counts(rest_api_counts(1, 1, 1))


def test_api_cors_true_creates_options_and_gateway_responses(pulumi_mocks):
    api = RestApi("test-api", cors=True)
    api.route("GET", "/users", handler=Funcs.USERS.handler)

    @pulumi.runtime.test
    def deploy():
        return api.resources

    deploy()

    assert_options_set(pulumi_mocks, "test-api", "/users", WILDCARD_HEADERS)
    assert_gateway_responses(pulumi_mocks, "test-api", {"Access-Control-Allow-Origin": "*"})
    pulumi_mocks.assert_res_counts(rest_api_counts(1, 1, 1) + cors_counts(1))


def test_api_cors_creates_options_for_each_unique_path(pulumi_mocks):
    api = RestApi("test-api", cors=True)
    api.route("GET", "/users", handler=Funcs.USERS.handler)
    api.route("POST", "/users", handler=Funcs.USERS.handler)
    api.route("GET", "/orders", handler=Funcs.ORDERS.handler)

    @pulumi.runtime.test
    def deploy():
        return api.resources

    deploy()

    assert_options_set(pulumi_mocks, "test-api", "/users", WILDCARD_HEADERS)
    assert_options_set(pulumi_mocks, "test-api", "/orders", WILDCARD_HEADERS)
    pulumi_mocks.assert_res_counts(rest_api_counts(2, 2, 3) + cors_counts(2))


def test_api_cors_custom_config_creates_correct_headers(pulumi_mocks):
    """Every configured field shows up on the preflight, the gateway responses, and as
    `STLV_CORS_*` in the route Lambda's environment for the handler-side headers."""
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

    @pulumi.runtime.test
    def deploy():
        return api.resources

    deploy()

    assert_options_set(
        pulumi_mocks,
        "test-api",
        "/users",
        {
            "Access-Control-Allow-Origin": "https://example.com",
            "Access-Control-Allow-Methods": "GET,OPTIONS,POST",
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
            "Access-Control-Max-Age": "3600",
            "Access-Control-Allow-Credentials": "true",
        },
    )
    assert_gateway_responses(
        pulumi_mocks,
        "test-api",
        {
            "Access-Control-Allow-Origin": "https://example.com",
            "Access-Control-Expose-Headers": "X-Request-Id,X-Custom",
            "Access-Control-Allow-Credentials": "true",
        },
    )
    pulumi_mocks.assert_res(
        Funcs.USERS.full_name("test-api"),
        R.FUNCTION,
        {
            "environment": {
                "variables": {
                    "STLV_CORS_ALLOW_ORIGIN": "https://example.com",
                    "STLV_CORS_EXPOSE_HEADERS": "X-Custom,X-Request-Id",
                    "STLV_CORS_ALLOW_CREDENTIALS": "true",
                }
            }
        },
        partial=True,
    )
    pulumi_mocks.assert_res_counts(rest_api_counts(1, 1, 2) + cors_counts(1))


def test_api_cors_methods_limited_to_route_methods(pulumi_mocks):
    api = RestApi(
        "test-api",
        cors=CorsConfig(
            allow_origins="https://example.com",
            allow_methods=["GET", "POST", "PUT", "DELETE"],
        ),
    )
    api.route("GET", "/users", handler=Funcs.USERS.handler)

    @pulumi.runtime.test
    def deploy():
        return api.resources

    deploy()

    assert_options_set(
        pulumi_mocks,
        "test-api",
        "/users",
        {
            "Access-Control-Allow-Origin": "https://example.com",
            "Access-Control-Allow-Methods": "GET,OPTIONS",
            "Access-Control-Allow-Headers": "*",
        },
    )


def test_api_cors_root_path_creates_options_with_root_path(pulumi_mocks):
    api = RestApi("test-api", cors=True)
    api.route("GET", "/", handler=Funcs.SIMPLE.handler)

    @pulumi.runtime.test
    def deploy():
        return api.resources

    deploy()

    assert_options_set(pulumi_mocks, "test-api", "/", WILDCARD_HEADERS)
    pulumi_mocks.assert_res_counts(rest_api_counts(1, 0, 1) + cors_counts(1))


def test_api_cors_nested_path_includes_all_parts_in_name(pulumi_mocks):
    api = RestApi("test-api", cors=True)
    api.route("GET", "/users/{id}/orders", handler=Funcs.USERS.handler)

    @pulumi.runtime.test
    def deploy():
        return api.resources

    deploy()

    assert_options_set(pulumi_mocks, "test-api", "/users/{id}/orders", WILDCARD_HEADERS)


def test_multiple_apis_with_cors_create_uniquely_named_resources(pulumi_mocks):
    api1 = RestApi("api1", cors=True)
    api1.route("GET", "/users", handler=Funcs.USERS.handler)

    api2 = RestApi("api2", cors=True)
    api2.route("GET", "/users", handler=Funcs.USERS.handler)

    @pulumi.runtime.test
    def deploy():
        return [api1.resources, api2.resources]

    deploy()

    for api_name in ("api1", "api2"):
        assert_options_set(pulumi_mocks, api_name, "/users", WILDCARD_HEADERS)
        assert_gateway_responses(pulumi_mocks, api_name, {"Access-Control-Allow-Origin": "*"})
    pulumi_mocks.assert_res_counts(rest_api_counts(2, 2, 2, apis=2) + cors_counts(2, apis=2))


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

    @pulumi.runtime.test
    def deploy():
        return api.resources

    deploy()

    legacy_paths = ("users-id", "root", path_to_resource_name(long_parts))
    assert {n for n in old_names if "OPTIONS" in n} == {
        resource_name(f"test-api-{kind}-OPTIONS-{path}", limit=128)
        for kind in ("method", "method-response", "integration", "integration-response")
        for path in legacy_paths
    }


def test_api_cors_trailing_slash_shares_one_options_set(pulumi_mocks):
    """`/users/` and `/users` are one AWS resource, so they get one OPTIONS set named after
    the canonical path and advertising both routes' verbs, whichever was declared first."""
    api = RestApi("test-api", cors=CorsConfig(allow_methods=["GET", "POST"]))
    api.route("GET", "/users/", handler=Funcs.SIMPLE.handler)
    api.route("POST", "/users", handler=Funcs.USERS.handler)

    @pulumi.runtime.test
    def deploy():
        return api.resources

    deploy()

    assert_options_set(
        pulumi_mocks,
        "test-api",
        "/users",
        {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET,OPTIONS,POST",
            "Access-Control-Allow-Headers": "*",
        },
    )
    pulumi_mocks.assert_res_counts(rest_api_counts(2, 1, 2) + cors_counts(1))


@mark.parametrize("api_first", [param(True, id="api_first"), param(False, id="function_first")])
def test_api_cors_env_vars_reach_a_function_instance_in_either_build_order(
    pulumi_mocks, api_first
):
    """drive() builds components type by type, so a user's Function may be built before the
    API that routes to it; the CORS env vars must be on the Lambda either way."""
    fn = Function("my-fn", handler=Funcs.USERS.handler)
    api = RestApi("test-api", cors=True)
    api.route("GET", "/users", fn)

    @pulumi.runtime.test
    def deploy():
        return [api.resources, fn.resources] if api_first else [fn.resources, api.resources]

    deploy()

    pulumi_mocks.assert_res(
        "my-fn",
        R.FUNCTION,
        {"environment": {"variables": {"STLV_CORS_ALLOW_ORIGIN": "*"}}},
        partial=True,
    )
    pulumi_mocks.assert_res_counts(rest_api_counts(1, 1, 1) + cors_counts(1))


def test_api_cors_route_rejects_a_function_already_built(pulumi_mocks):
    """A built Lambda can't take env vars any more; fail here rather than deploy without CORS."""
    fn = Function("my-fn", handler=Funcs.USERS.handler)
    _ = fn.resources
    api = RestApi("test-api", cors=True)

    with raises(RuntimeError, match="Cannot modify Function 'my-fn' after resources"):
        api.route("GET", "/users", fn)


def test_api_route_accepts_a_built_function_without_cors(pulumi_mocks):
    """Nothing to add without CORS, so a Function read before routing keeps working."""
    fn = Function("my-fn", handler=Funcs.USERS.handler)
    api = RestApi("test-api", cors=False)

    @pulumi.runtime.test
    def deploy():
        _ = fn.resources
        api.route("GET", "/users", fn)
        return api.resources

    deploy()

    pulumi_mocks.assert_res("test-api-permission-my-fn", R.LAMBDA_PERMISSION)
    pulumi_mocks.assert_res_counts(rest_api_counts(1, 1, 1))


@mark.parametrize(
    ("api2_cors", "api2_cors_counts"),
    [
        param(
            CorsConfig(allow_origins="https://a.example", expose_headers=["X-A", "X-B"]),
            cors_counts(1),
            id="same_settings",
        ),
        param(
            CorsConfig(allow_origins="https://a.example", expose_headers=["X-B", "X-A"]),
            cors_counts(1),
            id="same_settings_other_order",
        ),
        param(False, Counter(), id="no_cors"),
    ],
)
@mark.parametrize("built_first", [param(False, id="routed_first"), param(True, id="built_first")])
def test_api_cors_one_function_on_two_apis(pulumi_mocks, api2_cors, api2_cors_counts, built_first):
    """The same settings twice are no conflict, list order included, and an API without
    CORS adds nothing, so a Lambda can serve a CORS API and a plain one (its handler may
    then send CORS headers on the plain one too). Neither cares whether the Lambda was
    built before the second route: nothing new would go into its env."""
    fn = Function("my-fn", handler=Funcs.USERS.handler)
    api1 = RestApi(
        "api1", cors=CorsConfig(allow_origins="https://a.example", expose_headers=["X-A", "X-B"])
    )
    api1.route("GET", "/users", fn)
    api2 = RestApi("api2", cors=api2_cors)

    @pulumi.runtime.test
    def deploy():
        if built_first:
            _ = fn.resources
        api2.route("GET", "/users", fn)
        return [api1.resources, api2.resources]

    deploy()

    pulumi_mocks.assert_res(
        "my-fn",
        R.FUNCTION,
        {
            "environment": {
                "variables": {
                    "STLV_CORS_ALLOW_ORIGIN": "https://a.example",
                    "STLV_CORS_EXPOSE_HEADERS": "X-A,X-B",
                }
            }
        },
        partial=True,
    )
    pulumi_mocks.assert_res_counts(
        rest_api_counts(1, 2, 2, apis=2)
        + cors_counts(1)
        + api2_cors_counts
        + Counter({R.LAMBDA_PERMISSION: 1})
    )


def test_api_cors_rejects_one_function_on_two_apis_with_different_settings():
    """One Lambda has one set of `STLV_CORS_*` values, so its APIs must agree on them."""
    fn = Function("my-fn", handler=Funcs.USERS.handler)
    RestApi("api1", cors=CorsConfig(allow_origins="https://a.example")).route("GET", "/users", fn)
    api2 = RestApi("api2", cors=CorsConfig(allow_origins="https://b.example"))

    with raises(
        ValueError,
        match=re.escape(
            "Conflicting environment variables for Function 'my-fn': "
            "{'STLV_CORS_ALLOW_ORIGIN': 'https://a.example'} and "
            "{'STLV_CORS_ALLOW_ORIGIN': 'https://b.example'}"
        ),
    ):
        api2.route("GET", "/users", fn)
