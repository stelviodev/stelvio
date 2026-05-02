import json
import time

import pytest

from stelvio.aws.http_api import HttpApi
from stelvio.component import ComponentRegistry

from .assert_helpers import (
    assert_apigatewayv2_tags,
    assert_http_api_authorizers,
    assert_http_api_cors_headers,
    assert_http_api_route_auth,
    assert_http_api_routes,
    assert_lambda_tags,
    http_request,
)
from .export_helpers import export_function, export_http_api

pytestmark = pytest.mark.integration

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

    assert_http_api_cors_headers(outputs["http_api_corsapi_url"], path="/hello")


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
            identity_sources="$request.header.Authorization",
        )
        api.route("GET", "/secure", "handlers/echo.main", auth=auth)
        export_http_api(api)

    outputs = stelvio_env.deploy(infra)
    api_id = outputs["http_api_authapi_id"]

    assert_http_api_authorizers(api_id, expected_types=["REQUEST"])
    assert_http_api_route_auth(api_id, route_key="GET /secure", auth_type="CUSTOM")

    time.sleep(_HTTP_API_DEPLOY_WAIT)
    url = outputs["http_api_authapi_url"].rstrip("/") + "/secure"

    status, _ = http_request(url)
    assert status == 401

    status, _ = http_request(url, headers={"Authorization": "Bearer deny"})
    assert status == 403

    status, body = http_request(url, headers={"Authorization": "Bearer allow"})
    assert status == 200
    event = json.loads(body)
    assert event["version"] == "2.0"
