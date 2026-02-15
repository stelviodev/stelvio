import pytest

from stelvio.aws.layer import Layer

from .assert_helpers import assert_lambda_layer

pytestmark = pytest.mark.integration


def test_layer_basic(stelvio_env, project_dir):
    def infra():
        Layer("utils", code="handlers/layer_code")

    outputs = stelvio_env.deploy(infra)

    assert_lambda_layer(
        outputs["layer_utils_version_arn"],
        compatible_runtimes=["python3.12"],
    )
