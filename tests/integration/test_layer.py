import pytest

from stelvio.aws.function import Function
from stelvio.aws.layer import Layer

from .assert_helpers import assert_lambda_function, assert_lambda_layer, invoke_lambda

pytestmark = pytest.mark.integration


# --- Properties ---


@pytest.mark.parametrize(
    ("name", "kwargs"),
    [
        ("code-only", {"code": "handlers/layer_code"}),
        ("reqs-only", {"requirements": ["requests"]}),
        ("code-and-reqs", {"code": "handlers/layer_code", "requirements": ["requests"]}),
        ("arm64-reqs", {"requirements": ["requests"], "architecture": "arm64"}),
        ("py311-reqs", {"requirements": ["requests"], "runtime": "python3.11"}),
    ],
)
def test_layer_properties(stelvio_env, project_dir, name, kwargs):
    def infra():
        Layer(name, **kwargs)

    outputs = stelvio_env.deploy(infra)

    assert_lambda_layer(
        outputs[f"layer_{name}_version_arn"],
        compatible_runtimes=[kwargs.get("runtime", "python3.12")],
        compatible_architectures=[kwargs.get("architecture", "x86_64")],
    )


def test_layer_requirements_file(stelvio_env, project_dir):
    """Layer with requirements specified as a file path."""
    (project_dir / "layer_requirements.txt").write_text("requests\n")

    def infra():
        Layer("file-deps", requirements="layer_requirements.txt")

    outputs = stelvio_env.deploy(infra)

    assert_lambda_layer(
        outputs["layer_file-deps_version_arn"],
        compatible_runtimes=["python3.12"],
        compatible_architectures=["x86_64"],
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
    # use_requests handler returns "requests {version}"
    assert result["body"].startswith("requests ")
