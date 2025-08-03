"""Passphrase management for Pulumi state encryption using AWS Parameter Store."""

import json
import logging
import secrets

import boto3
from botocore.client import BaseClient
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


def get_passphrase(
    project_name: str, environment: str, aws_profile: str | None, aws_region: str
) -> str:
    """Get passphrase from Parameter Store or create if it doesn't exist."""
    session = boto3.Session(profile_name=aws_profile, region_name=aws_region)
    ssm = session.client("ssm")

    _ensure_bootstrap_parameter(ssm)

    pass_param_name = f"/stlv/passphrase/{project_name}/{environment}"

    try:
        response = ssm.get_parameter(Name=pass_param_name, WithDecryption=True)
        logger.debug("Retrieved existing passphrase from Parameter Store")
        return response["Parameter"]["Value"]
    except ClientError as e:
        if e.response["Error"]["Code"] == "ParameterNotFound":
            # Create new passphrase
            passphrase = secrets.token_urlsafe(32)
            ssm.put_parameter(
                Name=pass_param_name,
                Value=passphrase,
                Type="SecureString",
                Description="DO NOT DELETE! YOU WILL NOT BE ABLE TO RECOVER STATE OF ENVIRONMENT!",
            )
            logger.info("Created new passphrase in Parameter Store: %s", pass_param_name)
            return passphrase
        raise


def _ensure_bootstrap_parameter(ssm: BaseClient) -> None:
    """Ensure the bootstrap parameter exists."""
    bootstrap_param = "/stlv/bootstrap"
    bootstrap_value = json.dumps({"version": 1})

    try:
        ssm.get_parameter(Name=bootstrap_param)
        logger.debug("Bootstrap parameter exists")
    except ClientError as e:
        if e.response["Error"]["Code"] == "ParameterNotFound":
            ssm.put_parameter(
                Name=bootstrap_param,
                Value=bootstrap_value,
                Type="String",
                Description="Stelvio bootstrap metadata",
            )
            logger.info("Created bootstrap parameter: %s", bootstrap_param)
        else:
            raise
