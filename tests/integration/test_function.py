import pytest

from stelvio.aws.function import Function
from stelvio.aws.layer import Layer

from .assert_helpers import assert_lambda_function, assert_lambda_function_url

pytestmark = pytest.mark.integration


def test_function_basic(stelvio_env, project_dir):
    def infra():
        Function("echo", handler="handlers/echo.main")

    outputs = stelvio_env.deploy(infra)

    assert_lambda_function(
        outputs["function_echo_arn"],
        runtime="python3.12",
        memory=128,
        timeout=60,
    )


def test_function_memory_timeout(stelvio_env, project_dir):
    def infra():
        Function("configured", handler="handlers/echo.main", memory=256, timeout=30)

    outputs = stelvio_env.deploy(infra)

    assert_lambda_function(
        outputs["function_configured_arn"],
        memory=256,
        timeout=30,
    )


def test_function_environment(stelvio_env, project_dir):
    def infra():
        Function(
            "with-env",
            handler="handlers/echo.main",
            environment={"MY_VAR": "hello", "OTHER_VAR": "world"},
        )

    outputs = stelvio_env.deploy(infra)

    assert_lambda_function(
        outputs["function_with-env_arn"],
        environment={"MY_VAR": "hello", "OTHER_VAR": "world"},
    )


def test_function_url_public(stelvio_env, project_dir):
    def infra():
        Function("public-api", handler="handlers/echo.main", url="public")

    outputs = stelvio_env.deploy(infra)

    assert "function_public-api_url" in outputs
    assert outputs["function_public-api_url"].startswith("https://")
    assert_lambda_function_url(
        outputs["function_public-api_arn"],
        auth_type="NONE",
        cors=True,
    )


def test_function_url_private(stelvio_env, project_dir):
    def infra():
        Function("private-api", handler="handlers/echo.main", url="private")

    outputs = stelvio_env.deploy(infra)

    assert "function_private-api_url" in outputs
    assert_lambda_function_url(
        outputs["function_private-api_arn"],
        auth_type="AWS_IAM",
        cors=False,
    )


def test_function_with_layer(stelvio_env, project_dir):
    def infra():
        layer = Layer("utils", code="handlers/layer_code")
        Function("with-layer", handler="handlers/echo.main", layers=[layer])

    outputs = stelvio_env.deploy(infra)

    assert_lambda_function(
        outputs["function_with-layer_arn"],
        layers_count=1,
    )
