import json
import time

from pytest import mark

from stelvio.aws.api_gateway.http_api import HttpApi
from stelvio.component import ComponentRegistry

from .assert_helpers import (
    assert_api_cors_headers,
    assert_apigatewayv2_tags,
    assert_http_api_authorizers,
    assert_http_api_integrations_share_uri,
    assert_http_api_route_auth,
    assert_http_api_routes,
    assert_lambda_tags,
    http_request,
)
from .export_helpers import export_function, export_http_api, export_user_pool

pytestmark = mark.integration

_HTTP_API_DEPLOY_WAIT = 3


def test_http_api_basic(stelvio_env, project_dir):
    def infra():
        api = HttpApi("myapi")
        api.route("GET", "/hello", "handlers/echo.main")
        export_http_api(api)

    outputs = stelvio_env.deploy(infra)
    base_url = outputs["http_api_myapi_url"]

    assert base_url.startswith("https://")
    assert_http_api_routes(outputs["http_api_myapi_id"], expected_route_keys={"GET /hello"})

    time.sleep(_HTTP_API_DEPLOY_WAIT)
    status, body = http_request(f"{base_url}/hello")
    assert status == 200
    event = json.loads(body)
    assert event["version"] == "2.0"
    assert event["routeKey"] == "GET /hello"


def test_http_api_multiple_routes_and_default(stelvio_env, project_dir):
    def infra():
        api = HttpApi("routes")
        api.route(["GET", "DELETE"], "/users/{id}", "handlers/echo.main")
        api.route("ANY", "$default", "handlers/echo.main")
        export_http_api(api)

    outputs = stelvio_env.deploy(infra)

    assert_http_api_routes(
        outputs["http_api_routes_id"],
        expected_route_keys={"GET /users/{id}", "DELETE /users/{id}", "$default"},
    )


def test_http_api_cors(stelvio_env, project_dir):
    def infra():
        api = HttpApi("corsapi", cors=True)
        api.route("GET", "/hello", "handlers/echo.main")
        export_http_api(api)

    outputs = stelvio_env.deploy(infra)
    time.sleep(_HTTP_API_DEPLOY_WAIT)

    assert_api_cors_headers(outputs["http_api_corsapi_url"], path="/hello")


def test_http_api_tags_and_generated_function_tags(stelvio_env, project_dir):
    def infra():
        api = HttpApi("tagged-api", tags={"Team": "platform"})
        api.route("GET", "/hello", "handlers/echo.main")
        export_http_api(api)
        fn = ComponentRegistry.get_component_by_name("tagged-api-handlers-echo_main")
        export_function(fn)

    outputs = stelvio_env.deploy(infra)

    assert_apigatewayv2_tags(outputs["http_api_tagged-api_arn"], {"Team": "platform"})
    assert_lambda_tags(outputs["function_tagged-api-handlers-echo_main_arn"], {"Team": "platform"})


def test_http_api_custom_stage_name(stelvio_env, project_dir):
    def infra():
        api = HttpApi("stageapi", stage_name="prod")
        api.route("GET", "/hello", "handlers/echo.main")
        export_http_api(api)

    outputs = stelvio_env.deploy(infra)

    assert outputs["http_api_stageapi_stage_name"] == "prod"
    assert outputs["http_api_stageapi_url"].endswith("/prod")


def test_http_api_lambda_authorizer(stelvio_env, project_dir):
    def infra():
        api = HttpApi("authapi")
        auth = api.add_lambda_authorizer(
            "jwt",
            "handlers/http_api_auth.handler",
            identity_sources=["$request.header.Authorization"],
        )
        api.route("GET", "/secure", "handlers/echo.main", auth=auth)
        export_http_api(api)

    outputs = stelvio_env.deploy(infra)
    api_id = outputs["http_api_authapi_id"]

    assert_http_api_authorizers(api_id, expected_types=["REQUEST"])
    assert_http_api_route_auth(api_id, route_key="GET /secure", auth_type="CUSTOM")

    time.sleep(_HTTP_API_DEPLOY_WAIT)
    url = f"{outputs['http_api_authapi_url']}/secure"

    status, _ = http_request(url)
    assert status == 401

    status, _ = http_request(url, headers={"Authorization": "Bearer deny"})
    assert status == 403

    status, body = http_request(url, headers={"Authorization": "Bearer allow"})
    assert status == 200
    event = json.loads(body)
    assert event["version"] == "2.0"


def test_http_api_any_method(stelvio_env, project_dir):
    def infra():
        api = HttpApi("anyhttp")
        api.route("ANY", "/proxy", "handlers/echo.main")
        export_http_api(api)

    outputs = stelvio_env.deploy(infra)
    base_url = outputs["http_api_anyhttp_url"]

    assert_http_api_routes(outputs["http_api_anyhttp_id"], expected_route_keys={"ANY /proxy"})

    time.sleep(_HTTP_API_DEPLOY_WAIT)
    for method in ("GET", "POST", "DELETE"):
        status, _ = http_request(f"{base_url}/proxy", method=method)
        assert status == 200, f"{method} /proxy expected 200, got {status}"


def test_http_api_path_parameters(stelvio_env, project_dir):
    def infra():
        api = HttpApi("pathhttp")
        api.route("GET", "/users", "handlers/echo.main")
        api.route("GET", "/users/{id}", "handlers/echo.main")
        api.route("GET", "/users/{id}/orders/{order_id}", "handlers/echo.main")
        export_http_api(api)

    outputs = stelvio_env.deploy(infra)
    base_url = outputs["http_api_pathhttp_url"]

    assert_http_api_routes(
        outputs["http_api_pathhttp_id"],
        expected_route_keys={
            "GET /users",
            "GET /users/{id}",
            "GET /users/{id}/orders/{order_id}",
        },
    )

    time.sleep(_HTTP_API_DEPLOY_WAIT)
    status, body = http_request(f"{base_url}/users/42/orders/7")
    assert status == 200
    event = json.loads(body)
    assert event["pathParameters"] == {"id": "42", "order_id": "7"}


def test_http_api_multiple_methods_same_path(stelvio_env, project_dir):
    def infra():
        api = HttpApi("methhttp")
        api.route(["GET", "POST", "DELETE"], "/items", "handlers/echo.main")
        export_http_api(api)

    outputs = stelvio_env.deploy(infra)

    assert_http_api_routes(
        outputs["http_api_methhttp_id"],
        expected_route_keys={"GET /items", "POST /items", "DELETE /items"},
    )


def test_http_api_iam_auth(stelvio_env, project_dir):
    def infra():
        api = HttpApi("iamhttp")
        api.route("GET", "/admin", "handlers/echo.main", auth="IAM")
        export_http_api(api)

    outputs = stelvio_env.deploy(infra)
    api_id = outputs["http_api_iamhttp_id"]
    base_url = outputs["http_api_iamhttp_url"]

    assert_http_api_route_auth(api_id, route_key="GET /admin", auth_type="AWS_IAM")

    time.sleep(_HTTP_API_DEPLOY_WAIT)
    status, _ = http_request(f"{base_url}/admin")
    assert status == 403


def test_http_api_jwt_authorizer(stelvio_env, project_dir):
    def infra():
        api = HttpApi("jwthttp")
        auth = api.add_jwt_authorizer(
            "jwt",
            issuer="https://accounts.google.com",
            audiences=["api://example"],
        )
        api.route("GET", "/secure", "handlers/echo.main", auth=auth)
        export_http_api(api)

    outputs = stelvio_env.deploy(infra)
    api_id = outputs["http_api_jwthttp_id"]

    assert_http_api_authorizers(
        api_id,
        expected_types=["JWT"],
        expected_jwt={
            "issuer": "https://accounts.google.com",
            "audiences": ["api://example"],
        },
    )
    assert_http_api_route_auth(api_id, route_key="GET /secure", auth_type="JWT")


def test_http_api_cognito_authorizer(stelvio_env, project_dir):
    from stelvio.aws.cognito import UserPool

    def infra():
        pool = UserPool("coghttppool", usernames=["email"])
        api = HttpApi("coghttp")
        auth = api.add_cognito_authorizer(
            "cog",
            user_pool=pool,
            audiences=["dummy-client-id"],
        )
        api.route("GET", "/protected", "handlers/echo.main", auth=auth)
        export_http_api(api)
        export_user_pool(pool)

    outputs = stelvio_env.deploy(infra)
    api_id = outputs["http_api_coghttp_id"]
    pool_id = outputs["user_pool_coghttppool_id"]
    issuer = f"https://cognito-idp.{stelvio_env.aws_region}.amazonaws.com/{pool_id}"

    assert_http_api_authorizers(
        api_id,
        expected_types=["JWT"],
        expected_jwt={"issuer": issuer, "audiences": ["dummy-client-id"]},
    )
    assert_http_api_route_auth(api_id, route_key="GET /protected", auth_type="JWT")


def test_http_api_default_auth_with_public_override(stelvio_env, project_dir):
    def infra():
        api = HttpApi("defhttp")
        auth = api.add_lambda_authorizer(
            "jwt",
            "handlers/http_api_auth.handler",
            identity_sources=["$request.header.Authorization"],
        )
        api.default_auth = auth
        api.route("GET", "/protected", "handlers/echo.main")
        api.route("GET", "/health", "handlers/echo.main", auth=False)
        export_http_api(api)

    outputs = stelvio_env.deploy(infra)
    api_id = outputs["http_api_defhttp_id"]

    assert_http_api_route_auth(api_id, route_key="GET /protected", auth_type="CUSTOM")
    assert_http_api_route_auth(api_id, route_key="GET /health", auth_type="NONE")


def test_http_api_multiple_authorizers(stelvio_env, project_dir):
    def infra():
        api = HttpApi("multihttp")
        lambda_auth = api.add_lambda_authorizer(
            "lambda",
            "handlers/http_api_auth.handler",
            identity_sources=["$request.header.Authorization"],
        )
        jwt_auth = api.add_jwt_authorizer(
            "jwt",
            issuer="https://accounts.google.com",
            audiences=["api://example"],
        )
        api.route("GET", "/lambda-protected", "handlers/echo.main", auth=lambda_auth)
        api.route("GET", "/jwt-protected", "handlers/echo.main", auth=jwt_auth)
        export_http_api(api)

    outputs = stelvio_env.deploy(infra)
    api_id = outputs["http_api_multihttp_id"]

    assert_http_api_authorizers(api_id, expected_types=["JWT", "REQUEST"])
    assert_http_api_route_auth(api_id, route_key="GET /lambda-protected", auth_type="CUSTOM")
    assert_http_api_route_auth(api_id, route_key="GET /jwt-protected", auth_type="JWT")


def test_http_api_shared_handler(stelvio_env, project_dir):
    from stelvio.aws.function import Function

    def infra():
        fn = Function("sharedhttp", handler="handlers/echo.main")
        api = HttpApi("sharedhttpapi")
        api.route("GET", "/one", fn)
        api.route("POST", "/two", fn)
        export_function(fn)
        export_http_api(api)

    outputs = stelvio_env.deploy(infra)
    api_id = outputs["http_api_sharedhttpapi_id"]
    function_arn = outputs["function_sharedhttp_arn"]

    assert_http_api_routes(
        api_id,
        expected_route_keys={"GET /one", "POST /two"},
    )
    assert_http_api_integrations_share_uri(api_id, expected_function_arn=function_arn)

    time.sleep(_HTTP_API_DEPLOY_WAIT)
    base_url = outputs["http_api_sharedhttpapi_url"]
    for method, path, route_key in (
        ("GET", "/one", "GET /one"),
        ("POST", "/two", "POST /two"),
    ):
        status, body = http_request(f"{base_url}{path}", method=method)
        assert status == 200
        assert json.loads(body)["routeKey"] == route_key
