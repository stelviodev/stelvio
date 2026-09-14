from collections.abc import Callable

import pulumi
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

from .constants import LAMBDA_BASIC_EXECUTION_ROLE, LAMBDA_VPC_ACCESS_EXECUTION_ROLE


def _create_lambda_role(
    name: str,
    customizer: Callable[[str, dict], dict] | None = None,
    opts: pulumi.ResourceOptions | None = None,
) -> Role:
    """Create basic execution role for Lambda.

    Args:
        name: The function name used for resource naming.
        customizer: Optional callback to apply customizations to the role properties.
        opts: Pulumi resource options (parent, aliases, etc.).
    """
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

    default_props = {"assume_role_policy": assume_role_policy.json}
    if customizer:
        default_props = customizer("role", default_props)

    return Role(
        safe_name(context().prefix(), name, 64, "-r"),
        **default_props,
        opts=opts,
    )


def _attach_role_policies(
    name: str,
    role: Role,
    function_policy: Policy | None,
    opts: pulumi.ResourceOptions | None = None,
    *,
    vpc: bool = False,
) -> list[RolePolicyAttachment]:
    """Attach required policies to Lambda role."""
    attachments = [
        RolePolicyAttachment(
            context().prefix(f"{name}-basic-execution-r-p-attachment"),
            role=role.name,
            policy_arn=LAMBDA_BASIC_EXECUTION_ROLE,
            opts=opts,
        )
    ]
    if vpc:
        attachments.append(
            RolePolicyAttachment(
                context().prefix(f"{name}-vpc-execution-r-p-attachment"),
                role=role.name,
                policy_arn=LAMBDA_VPC_ACCESS_EXECUTION_ROLE,
                opts=opts,
            )
        )
    if function_policy:
        attachments.append(
            RolePolicyAttachment(
                context().prefix(f"{name}-default-r-p-attachment"),
                role=role.name,
                policy_arn=function_policy.arn,
                opts=opts,
            )
        )
    return attachments
