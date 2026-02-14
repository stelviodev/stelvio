import pytest

from stelvio.aws.layer import Layer


@pytest.mark.integration
def test_layer(stelvio_env, project_dir):
    def infra():
        Layer("utils", code="handlers/layer_code")

    outputs = stelvio_env.deploy(infra)

    assert "layer_utils_version_arn" in outputs
