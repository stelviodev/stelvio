import pytest

from stelvio.aws.function import Function
from stelvio.aws.layer import Layer

from .assert_helpers import assert_lambda_function, assert_lambda_layer, invoke_lambda

pytestmark = pytest.mark.integration


# --- Properties ---


@pytest.mark.parametrize(
    ("name", "kwargs", "expected_arch"),
    [
        ("code-only", {"code": "handlers/layer_code"}, "x86_64"),
        ("reqs-only", {"requirements": ["requests"]}, "x86_64"),
        ("code-and-reqs", {"code": "handlers/layer_code", "requirements": ["requests"]}, "x86_64"),
        ("arm64-reqs", {"requirements": ["requests"], "architecture": "arm64"}, "arm64"),
    ],
)
def test_layer_properties(stelvio_env, project_dir, name, kwargs, expected_arch):
    def infra():
        Layer(name, **kwargs)

    outputs = stelvio_env.deploy(infra)

    assert_lambda_layer(
        outputs[f"layer_{name}_version_arn"],
        compatible_runtimes=["python3.12"],
        compatible_architectures=[expected_arch],
    )


# --- Functional: verify packages are actually importable ---


def test_layer_requirements_importable(stelvio_env, project_dir):
    """Attach layer to function, invoke it, verify the packaged dependency works."""

    def infra():
        layer = Layer("importable", requirements=["requests"])
        Function(
            "use-layer",
            handler="handlers/use_requests.main",
            layers=[layer],
        )

    outputs = stelvio_env.deploy(infra)

    assert_lambda_function(
        outputs["function_use-layer_arn"],
        layers_count=1,
    )

    result = invoke_lambda(outputs["function_use-layer_arn"])
    assert result["statusCode"] == 200
    assert "requests" in result["body"]
