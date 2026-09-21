"""Test that IAM functions use resource_name."""

from unittest.mock import ANY, patch

from pulumi_aws.iam import GetPolicyDocumentStatementArgs

from stelvio.aws.function.function import Function
from stelvio.aws.function.iam import _create_lambda_role


@patch("stelvio.aws.function.function.get_policy_document")
@patch("stelvio.aws.function.function.Policy")
@patch("stelvio.aws.function.function.resource_name", return_value="safe-policy-name")
def test_policy_uses_resource_name(mock_resource_name, mock_policy, mock_get_policy_document):
    # Create a Function instance (with mocked internals) to test _create_function_policy
    with patch.object(Function, "__init__", lambda self, *args, **kwargs: None):
        func = Function.__new__(Function)
        func._customize = {}  # Set up required attribute for _customizer method
        func._tags = {}

        # Act
        statements = [GetPolicyDocumentStatementArgs(actions=["s3:GetObject"], resources=["arn"])]
        func._create_function_policy("function-name", statements)

        # Assert - verify resource_name was called with correct parameters
        mock_resource_name.assert_called_once_with("function-name", limit=128, suffix="-p")

        # Assert - verify Policy was created with resource_name return value
        mock_policy.assert_called_once_with("safe-policy-name", path="/", policy=ANY, opts=ANY)


@patch("stelvio.aws.function.iam.get_policy_document")
@patch("stelvio.aws.function.iam.Role")
@patch("stelvio.aws.function.iam.resource_name", return_value="safe-role-name")
def test_role_uses_resource_name(mock_resource_name, mock_role, mock_get_policy_document):
    # Act
    _create_lambda_role("function-name")

    # Assert - verify resource_name was called with correct parameters
    mock_resource_name.assert_called_once_with("function-name", limit=64, suffix="-r")

    # Assert - verify Role was created with resource_name return value
    mock_role.assert_called_once_with("safe-role-name", assume_role_policy=ANY, opts=None)
