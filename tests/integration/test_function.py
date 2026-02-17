import pytest

from stelvio.aws.function import Function
from stelvio.aws.function.config import FunctionUrlConfig
from stelvio.aws.layer import Layer

from .assert_helpers import assert_lambda_function, assert_lambda_function_url

pytestmark = pytest.mark.integration


# --- Properties ---


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


@pytest.mark.parametrize(
    ("name", "kwargs"),
    [
        ("custom-mem", {"memory": 256, "timeout": 30}),
        ("custom-env", {"environment": {"MY_VAR": "hello", "OTHER_VAR": "world"}}),
        ("arm-fn", {"architecture": "arm64"}),
        ("py311", {"runtime": "python3.11"}),
    ],
)
def test_function_config(stelvio_env, project_dir, name, kwargs):
    def infra():
        Function(name, handler="handlers/echo.main", **kwargs)

    outputs = stelvio_env.deploy(infra)

    assert_lambda_function(outputs[f"function_{name}_arn"], **kwargs)


def test_function_folder(stelvio_env, project_dir):
    def infra():
        Function("with-folder", handler="handlers::echo.main")

    outputs = stelvio_env.deploy(infra)

    assert_lambda_function(outputs["function_with-folder_arn"])


def test_function_requirements(stelvio_env, project_dir):
    def infra():
        Function(
            "with-deps",
            handler="handlers/echo.main",
            requirements=["requests"],
        )

    outputs = stelvio_env.deploy(infra)

    assert_lambda_function(outputs["function_with-deps_arn"])


# --- Function URL ---


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


def test_function_url_streaming(stelvio_env, project_dir):
    def infra():
        Function(
            "stream-fn",
            handler="handlers/echo.main",
            url=FunctionUrlConfig(cors=True, streaming=True),
        )

    outputs = stelvio_env.deploy(infra)

    assert "function_stream-fn_url" in outputs
    assert_lambda_function_url(
        outputs["function_stream-fn_arn"],
        auth_type="NONE",
        cors=True,
        invoke_mode="RESPONSE_STREAM",
    )


def test_function_url_custom_cors(stelvio_env, project_dir):
    def infra():
        Function(
            "cors-fn",
            handler="handlers/echo.main",
            url=FunctionUrlConfig(
                cors={
                    "allow_origins": ["https://example.com"],
                    "allow_methods": ["GET", "POST"],
                    "allow_headers": ["Content-Type"],
                },
            ),
        )

    outputs = stelvio_env.deploy(infra)

    assert_lambda_function_url(
        outputs["function_cors-fn_arn"],
        auth_type="NONE",
        cors=True,
        cors_origins=["https://example.com"],
    )


# --- Layers ---


def test_function_with_layer(stelvio_env, project_dir):
    def infra():
        layer = Layer("utils", code="handlers/layer_code")
        Function("with-layer", handler="handlers/echo.main", layers=[layer])

    outputs = stelvio_env.deploy(infra)

    assert_lambda_function(
        outputs["function_with-layer_arn"],
        layers_count=1,
    )
