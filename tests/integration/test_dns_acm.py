import pytest

from stelvio.aws.acm import AcmValidatedDomain
from stelvio.aws.dns import Route53Dns

from .assert_helpers import assert_acm_certificate

pytestmark = pytest.mark.integration_dns


def test_acm_certificate_validation(stelvio_env, dns_domain, dns_zone_id):
    subdomain = f"acm-test-{stelvio_env.run_id}.{dns_domain}"
    dns = Route53Dns(zone_id=dns_zone_id)

    def infra():
        AcmValidatedDomain("cert", domain_name=subdomain)

    stelvio_env.deploy(infra, dns=dns)

    assert_acm_certificate(
        subdomain,
        status="ISSUED",
        validation_method="DNS",
        key_algorithm="RSA-2048",
    )
