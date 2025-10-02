from collections.abc import Sequence

from pulumi_aws.iam import (
    GetPolicyDocumentStatementArgs,
    GetPolicyDocumentStatementPrincipalArgs,
    Policy,
    Role,
    RolePolicyAttachment,
    get_policy_document,
)

from stelvio import context
from stelvio.component import safe_name

from .constants import LAMBDA_BASIC_EXECUTION_ROLE


def _create_function_policy(
    name: str, statements: Sequence[GetPolicyDocumentStatementArgs]
) -> Policy | None:
    """Create IAM policy for Lambda if there are any statements."""
    if not statements:
        return None

    policy_document = get_policy_document(statements=statements)

    return Policy(
        safe_name(context().prefix(), name, 128, "-p"), path="/", policy=policy_document.json
    )


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

    return Role(
        safe_name(context().prefix(), name, 64, "-r"), assume_role_policy=assume_role_policy.json
    )


def _attach_role_policies(
    name: str, role: Role, function_policy: Policy | None
) -> list[RolePolicyAttachment]:
    """Attach required policies to Lambda role."""
    basic_role_attachment = RolePolicyAttachment(
        context().prefix(f"{name}-basic-execution-r-p-attachment"),
        role=role.name,
        policy_arn=LAMBDA_BASIC_EXECUTION_ROLE,
    )
    if function_policy:
        default_role_attachment = RolePolicyAttachment(
            context().prefix(f"{name}-default-r-p-attachment"),
            role=role.name,
            policy_arn=function_policy.arn,
        )
        return [basic_role_attachment, default_role_attachment]

    return [basic_role_attachment]
