from typing import Any

from pulumi_aws.iam import (
    GetPolicyDocumentStatementArgs,
    GetPolicyDocumentStatementPrincipalArgs,
    Policy,
    Role,
    RolePolicyAttachment,
    get_policy_document,
)

from stelvio import context

from .constants import LAMBDA_BASIC_EXECUTION_ROLE


def _create_function_policy(name: str, statements: list[dict[str, Any]]) -> Policy | None:
    """Create IAM policy for Lambda if there are any statements."""
    if not statements:
        return None

    policy_document = get_policy_document(statements=statements)
    return Policy(context().prefix(f"{name}-policy"), path="/", policy=policy_document.json)


def _create_lambda_role(name: str) -> Role:
    """Create basic execution role for Lambda."""
    assume_role_policy = get_policy_document(
        statements=[
            GetPolicyDocumentStatementArgs(
                actions=["sts:AssumeRole"],
                principals=[
                    GetPolicyDocumentStatementPrincipalArgs(
                        identifiers=["lambda.amazonaws.com"], type="Service"
                    )
                ],
            )
        ]
    )
    return Role(context().prefix(f"{name}-role"), assume_role_policy=assume_role_policy.json)


def _attach_role_policies(name: str, role: Role, function_policy: Policy | None) -> None:
    """Attach required policies to Lambda role."""
    RolePolicyAttachment(
        context().prefix(f"{name}-basic-execution-role-policy-attachment"),
        role=role.name,
        policy_arn=LAMBDA_BASIC_EXECUTION_ROLE,
    )
    if function_policy:
        RolePolicyAttachment(
            context().prefix(f"{name}-default-role-policy-attachment"),
            role=role.name,
            policy_arn=function_policy.arn,
        )
