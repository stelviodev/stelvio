import pytest

from stelvio.aws.api_gateway import Api

from .assert_helpers import (
    assert_api_authorizer_count,
    assert_api_cors_headers,
    assert_api_routes,
)


@pytest.mark.integration
def test_api_basic(stelvio_env, project_dir):
    def infra():
        api = Api("myapi")
        api.route("GET", "/hello", "handlers/echo.main")

    outputs = stelvio_env.deploy(infra)

    assert "api_myapi_invoke_url" in outputs
    assert outputs["api_myapi_invoke_url"].startswith("https://")


@pytest.mark.integration
def test_api_multiple_routes(stelvio_env, project_dir):
    def infra():
        api = Api("multi")
        api.route("GET", "/hello", "handlers/echo.main")
        api.route("POST", "/items", "handlers/echo.main")

    outputs = stelvio_env.deploy(infra)

    assert_api_routes(
        outputs["api_multi_id"],
        expected_routes={"/hello": ["GET"], "/items": ["POST"]},
    )


@pytest.mark.integration
def test_api_any_method(stelvio_env, project_dir):
    def infra():
        api = Api("anyapi")
        api.route("ANY", "/proxy", "handlers/echo.main")

    outputs = stelvio_env.deploy(infra)

    assert_api_routes(
        outputs["api_anyapi_id"],
        expected_routes={"/proxy": ["ANY"]},
    )


@pytest.mark.integration
def test_api_cors(stelvio_env, project_dir):
    def infra():
        api = Api("corsapi", cors=True)
        api.route("GET", "/hello", "handlers/echo.main")

    outputs = stelvio_env.deploy(infra)

    assert_api_cors_headers(outputs["api_corsapi_invoke_url"], path="/hello")


@pytest.mark.integration
def test_api_token_authorizer(stelvio_env, project_dir):
    def infra():
        api = Api("authapi")
        auth = api.add_token_authorizer("jwt", "handlers/auth.handler")
        api.route("GET", "/protected", "handlers/echo.main", auth=auth)

    outputs = stelvio_env.deploy(infra)

    assert_api_authorizer_count(outputs["api_authapi_id"], count=1)
