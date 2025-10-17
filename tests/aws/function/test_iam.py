"""Test that IAM functions use safe_name."""

from unittest.mock import ANY, patch

from pulumi_aws.iam import GetPolicyDocumentStatementArgs

from stelvio import context
from stelvio.aws.function.iam import _create_function_policy, _create_lambda_role


@patch("stelvio.aws.function.iam.get_policy_document")
@patch("stelvio.aws.function.iam.Policy")
@patch("stelvio.aws.function.iam.safe_name", return_value="safe-policy-name")
def test_policy_uses_safe_name(mock_safe_name, mock_policy, mock_get_policy_document):
    # Act
    statements = [GetPolicyDocumentStatementArgs(actions=["s3:GetObject"], resources=["arn"])]
    _create_function_policy("function-name", statements)

    # Assert - verify safe_name was called with correct parameters
    mock_safe_name.assert_called_once_with(context().prefix(), "function-name", 128, "-p")

    # Assert - verify Policy was created with safe_name return value
    mock_policy.assert_called_once_with("safe-policy-name", path="/", policy=ANY)


@patch("stelvio.aws.function.iam.get_policy_document")
@patch("stelvio.aws.function.iam.Role")
@patch("stelvio.aws.function.iam.safe_name", return_value="safe-role-name")
def test_role_uses_safe_name(mock_safe_name, mock_role, mock_get_policy_document):
    # Act
    _create_lambda_role("function-name")

    # Assert - verify safe_name was called with correct parameters
    mock_safe_name.assert_called_once_with(context().prefix(), "function-name", 64, "-r")

    # Assert - verify Role was created with safe_name return value
    mock_role.assert_called_once_with("safe-role-name", assume_role_policy=ANY)
