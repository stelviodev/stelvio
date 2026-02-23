import pytest

from stelvio.aws.cloudfront.router import Router
from stelvio.aws.dns import Route53Dns
from stelvio.aws.s3 import Bucket

from .assert_helpers import assert_acm_certificate, assert_cloudfront_distribution
from .conftest import NO_WAIT_DEPLOY

pytestmark = pytest.mark.integration_dns


def test_router_custom_domain(stelvio_env, dns_domain, dns_zone_id):
    subdomain = f"app-rt.{dns_domain}"
    dns = Route53Dns(zone_id=dns_zone_id)

    def infra():
        bucket = Bucket("site")
        router = Router("app", custom_domain=subdomain, customize=NO_WAIT_DEPLOY)
        router.route("/", bucket)

    outputs = stelvio_env.deploy(infra, dns=dns)

    assert_cloudfront_distribution(
        outputs["router_app_distribution_id"],
        enabled=True,
        aliases=[subdomain],
        default_certificate=False,
        ssl_support_method="sni-only",
        minimum_protocol_version="TLSv1.2_2021",
        acm_certificate_domain=subdomain,
    )
    assert_acm_certificate(subdomain, status="ISSUED")
    assert outputs["router_app_domain_name"].endswith(".cloudfront.net")
    assert outputs["router_app_num_origins"] == 1
