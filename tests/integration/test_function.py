import pytest

from stelvio.aws.function import Function

from .assert_helpers import assert_lambda_function


@pytest.mark.integration
def test_function(stelvio_env, project_dir):
    def infra():
        Function("echo", handler="handlers/echo.main")

    outputs = stelvio_env.deploy(infra)

    assert_lambda_function(outputs["function_echo_arn"], runtime="python3.12")
