import logging
from functools import cache

from pulumi import Output, ResourceOptions
from pulumi_aws.apigateway import Account
from pulumi_aws.iam import (
    GetPolicyDocumentStatementArgs,
    GetPolicyDocumentStatementPrincipalArgs,
    Role,
    get_policy_document,
)

from stelvio.aws.api_gateway.constants import API_GATEWAY_LOGS_POLICY, API_GATEWAY_ROLE_NAME
from stelvio.provider import ProviderStore

logger = logging.getLogger("stelvio.aws.api_gateway")


@cache
def _create_api_gateway_account_and_role() -> Output[Account]:
    provider_opts = ResourceOptions(provider=ProviderStore.aws())

    # Read existing account to check if CloudWatch role is already configured.
    # API Gateway has one Account settings per region — this reads the current state.
    existing_account = Account.get(
        "api-gateway-account-ref", "APIGatewayAccount", opts=provider_opts
    )

    def handle_existing_role(existing_arn: str) -> Account:
        if existing_arn:
            # Account already has a CloudWatch role configured (by us, SST, CDK, or manual).
            # Leave it alone — don't try to re-adopt or override.
            logger.info("CloudWatch role already configured: %s", existing_arn)
            return existing_account

        # No role configured — create one and set it on the Account.
        logger.info("No CloudWatch role found, creating Stelvio configuration")
        role = _create_api_gateway_role(provider_opts)
        return Account(
            "api-gateway-account",
            cloudwatch_role_arn=role.arn,
            opts=ResourceOptions.merge(
                provider_opts,
                ResourceOptions(retain_on_delete=True),
            ),
        )

    return existing_account.cloudwatch_role_arn.apply(handle_existing_role)


def _create_api_gateway_role(provider_opts: ResourceOptions) -> Role:
    assume_role_policy = get_policy_document(
        statements=[
            GetPolicyDocumentStatementArgs(
                actions=["sts:AssumeRole"],
                principals=[
                    GetPolicyDocumentStatementPrincipalArgs(
                        identifiers=["apigateway.amazonaws.com"], type="Service"
                    )
                ],
            )
        ]
    )
    return Role(
        API_GATEWAY_ROLE_NAME,
        assume_role_policy=assume_role_policy.json,
        managed_policy_arns=[API_GATEWAY_LOGS_POLICY],
        opts=ResourceOptions.merge(
            provider_opts,
            ResourceOptions(retain_on_delete=True),
        ),
    )
