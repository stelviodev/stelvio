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

logger = logging.getLogger("stelvio.aws.api_gateway")


@cache
def _create_api_gateway_account_and_role() -> Output[Account]:
    # Get existing account configuration (read-only reference)
    existing_account = Account.get("api-gateway-account-ref", "APIGatewayAccount")

    def create_managed_account() -> Account:
        role = _create_api_gateway_role()
        return Account(
            "api-gateway-account",
            cloudwatch_role_arn=role.arn,
            opts=ResourceOptions(retain_on_delete=True),
        )

    def handle_existing_role(existing_arn: str) -> Account:
        if existing_arn:
            if API_GATEWAY_ROLE_NAME in existing_arn:  # Check if this is our Stelvio-managed role
                logger.info("Found Stelvio-managed role, returning managed Account")
                return create_managed_account()
            logger.info("Found user-managed role, using read-only reference: %s", existing_arn)
            return existing_account

        logger.info("No CloudWatch role found, creating Stelvio configuration")
        return create_managed_account()

    return existing_account.cloudwatch_role_arn.apply(handle_existing_role)


def _create_api_gateway_role() -> Role:
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
        opts=ResourceOptions(retain_on_delete=True),
    )
