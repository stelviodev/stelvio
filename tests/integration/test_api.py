import pytest

from stelvio.aws.api_gateway import Api
from stelvio.aws.function import Function

from .assert_helpers import (
    assert_api_authorizers,
    assert_api_cors_headers,
    assert_api_method_auth,
    assert_api_routes,
)


@pytest.mark.integration
def test_api_basic(stelvio_env, project_dir):
    def infra():
        api = Api("myapi")
        api.route("GET", "/hello", "handlers/echo.main")

    outputs = stelvio_env.deploy(infra)

    assert outputs["api_myapi_invoke_url"].startswith("https://")
    assert_api_routes(
        outputs["api_myapi_id"],
        expected_routes={"/hello": ["GET"]},
    )


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
def test_api_path_parameters(stelvio_env, project_dir):
    def infra():
        api = Api("pathapi")
        api.route("GET", "/users", "handlers/echo.main")
        api.route("GET", "/users/{id}", "handlers/echo.main")
        api.route("GET", "/users/{id}/orders/{order_id}", "handlers/echo.main")

    outputs = stelvio_env.deploy(infra)

    assert_api_routes(
        outputs["api_pathapi_id"],
        expected_routes={
            "/users": ["GET"],
            "/users/{id}": ["GET"],
            "/users/{id}/orders/{order_id}": ["GET"],
        },
    )


@pytest.mark.integration
def test_api_multiple_methods_same_path(stelvio_env, project_dir):
    def infra():
        api = Api("methapi")
        api.route(["GET", "POST", "DELETE"], "/items", "handlers/echo.main")

    outputs = stelvio_env.deploy(infra)

    assert_api_routes(
        outputs["api_methapi_id"],
        expected_routes={"/items": ["GET", "POST", "DELETE"]},
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

    assert_api_authorizers(outputs["api_authapi_id"], expected_types=["TOKEN"])
    assert_api_method_auth(
        outputs["api_authapi_id"],
        path="/protected",
        method="GET",
        auth_type="CUSTOM",
    )


@pytest.mark.integration
def test_api_request_authorizer(stelvio_env, project_dir):
    def infra():
        api = Api("reqauth")
        auth = api.add_request_authorizer("reqjwt", "handlers/auth.handler")
        api.route("GET", "/secure", "handlers/echo.main", auth=auth)

    outputs = stelvio_env.deploy(infra)

    assert_api_authorizers(outputs["api_reqauth_id"], expected_types=["REQUEST"])
    assert_api_method_auth(
        outputs["api_reqauth_id"],
        path="/secure",
        method="GET",
        auth_type="CUSTOM",
    )


@pytest.mark.integration
def test_api_default_auth_with_public_override(stelvio_env, project_dir):
    def infra():
        api = Api("defauth")
        auth = api.add_token_authorizer("jwt", "handlers/auth.handler")
        api.default_auth = auth
        api.route("GET", "/protected", "handlers/echo.main")
        api.route("GET", "/health", "handlers/echo.main", auth=False)

    outputs = stelvio_env.deploy(infra)

    api_id = outputs["api_defauth_id"]
    assert_api_method_auth(api_id, path="/protected", method="GET", auth_type="CUSTOM")
    assert_api_method_auth(api_id, path="/health", method="GET", auth_type="NONE")


@pytest.mark.integration
def test_api_shared_handler(stelvio_env, project_dir):
    def infra():
        fn = Function("shared", handler="handlers/echo.main")
        api = Api("sharedapi")
        api.route("GET", "/one", fn)
        api.route("POST", "/two", fn)

    outputs = stelvio_env.deploy(infra)

    assert_api_routes(
        outputs["api_sharedapi_id"],
        expected_routes={"/one": ["GET"], "/two": ["POST"]},
    )
    # Both routes use the same function — only one function created
    assert "function_shared_arn" in outputs


@pytest.mark.integration
def test_api_custom_stage_name(stelvio_env, project_dir):
    def infra():
        api = Api("stageapi", stage_name="prod")
        api.route("GET", "/hello", "handlers/echo.main")

    outputs = stelvio_env.deploy(infra)

    assert outputs["api_stageapi_stage_name"] == "prod"
    assert "/prod" in outputs["api_stageapi_invoke_url"]
