"""Test that IAM functions use safe_name."""

from unittest.mock import patch

from stelvio.aws.function.iam import _create_function_policy, _create_lambda_role


@patch("stelvio.aws.function.iam.context")
@patch("stelvio.aws.function.iam.Policy")
@patch("stelvio.aws.function.iam.get_policy_document")
@patch("stelvio.aws.function.iam.safe_name", return_value="safe-name")
def test_policy_uses_safe_name(mock_safe_name, *_):
    _create_function_policy("function-name", [{"Effect": "Allow"}])
    mock_safe_name.assert_called_once_with(
        mock_safe_name.call_args[0][0], "function-name", 128, "-p"
    )


@patch("stelvio.aws.function.iam.context")
@patch("stelvio.aws.function.iam.Role")
@patch("stelvio.aws.function.iam.get_policy_document")
@patch("stelvio.aws.function.iam.safe_name", return_value="safe-name")
def test_role_uses_safe_name(mock_safe_name, *_):
    _create_lambda_role("function-name")
    mock_safe_name.assert_called_once_with(
        mock_safe_name.call_args[0][0], "function-name", 64, "-r"
    )
