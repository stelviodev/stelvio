"""Integration tests for Cognito UserPool custom domain (DNS tier).

Tests deploy real AWS Cognito resources with custom domains and verify via boto3.
"""

import pytest

from stelvio.aws.cognito import UserPool
from stelvio.aws.dns import Route53Dns

from .assert_helpers import (
    assert_acm_certificate,
    assert_cognito_user_pool_domain,
)
from .export_helpers import export_user_pool

pytestmark = pytest.mark.integration_dns


def test_user_pool_custom_domain(stelvio_env, dns_domain, dns_zone_id):
    subdomain = f"auth-cog-{stelvio_env.run_id}.{dns_domain}"
    dns = Route53Dns(zone_id=dns_zone_id)

    def infra():
        pool = UserPool("auth", usernames=["email"], domain=subdomain)
        export_user_pool(pool)

    outputs = stelvio_env.deploy(infra, dns=dns)

    assert_cognito_user_pool_domain(
        outputs["user_pool_auth_id"],
        custom_domain=subdomain,
    )
    assert_acm_certificate(
        subdomain,
        status="ISSUED",
        validation_method="DNS",
        region="us-east-1",
    )
