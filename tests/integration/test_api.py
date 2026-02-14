import pytest

from stelvio.aws.api_gateway import Api


@pytest.mark.integration
def test_api_basic(stelvio_env, project_dir):
    def infra():
        api = Api("myapi")
        api.route("GET", "/hello", "handlers/echo.main")

    outputs = stelvio_env.deploy(infra)

    assert "api_myapi_invoke_url" in outputs
    assert outputs["api_myapi_invoke_url"].startswith("https://")
