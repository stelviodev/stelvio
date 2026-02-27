"""Cross-region provider test: app in eu-west-1, ACM cert forced to us-east-1."""

import os

import pytest

from stelvio.aws.acm import AcmValidatedDomain
from stelvio.aws.dns import Route53Dns
from stelvio.aws.queue import Queue

from .assert_helpers import assert_acm_certificate, assert_acm_tags, assert_sqs_tags
from .stelvio_test_env import StelvioTestEnv

pytestmark = pytest.mark.integration_dns


@pytest.fixture
def stelvio_env_eu(request):
    """StelvioTestEnv pinned to eu-west-1 for cross-region testing."""
    env = StelvioTestEnv(
        test_name=request.node.name,
        aws_profile=os.environ.get("STLV_TEST_AWS_PROFILE"),
        aws_region="eu-west-1",
    )
    yield env
    env.destroy()


def test_cross_region_provider(stelvio_env_eu, dns_domain, dns_zone_id):
    """Resources in app region (eu-west-1) and cross-region ACM cert in us-east-1."""
    subdomain = f"xr-{stelvio_env_eu.run_id}.{dns_domain}"
    dns = Route53Dns(zone_id=dns_zone_id)

    def infra():
        Queue("marker")
        AcmValidatedDomain("cert", domain_name=subdomain, region="us-east-1")

    outputs = stelvio_env_eu.deploy(infra, dns=dns)

    # Queue is in the app region (eu-west-1)
    assert ":eu-west-1:" in outputs["queue_marker_arn"]

    expected_tags = {
        "stelvio:app": f"stlv-{stelvio_env_eu.run_id}",
        "stelvio:env": "test",
    }

    # Auto-tags work in non-default region
    assert_sqs_tags(outputs["queue_marker_url"], expected_tags, region="eu-west-1")

    # ACM cert is in us-east-1 (cross-region provider)
    resources = stelvio_env_eu.export_resources()
    acm_cert = _find_acm_certificate(resources)
    cert_arn = acm_cert["id"]
    assert ":us-east-1:" in cert_arn, f"ACM cert should be in us-east-1, got ARN: {cert_arn}"

    # Cross-region cert is valid and has auto-tags
    assert_acm_certificate(subdomain, status="ISSUED", region="us-east-1")
    assert_acm_tags(cert_arn, expected_tags, region="us-east-1")


def _find_acm_certificate(resources: list[dict]) -> dict:
    """Find the ACM Certificate resource in the Pulumi state."""
    matches = [r for r in resources if "aws:acm/certificate:Certificate" in r["type"]]
    # Filter out CertificateValidation resources
    matches = [r for r in matches if "Validation" not in r["type"]]
    assert len(matches) == 1, (
        f"Expected 1 ACM certificate, got {len(matches)}: {[r['urn'] for r in matches]}"
    )
    return matches[0]
