import pytest

from stelvio.aws.cloudfront import CloudFrontDistribution
from stelvio.aws.s3 import Bucket

from .assert_helpers import (
    assert_cloudfront_distribution,
    assert_cloudfront_tags,
    assert_s3_bucket,
)
from .conftest import NO_WAIT_DEPLOY

pytestmark = pytest.mark.integration_cf


# --- Properties ---


def test_cloudfront_basic(stelvio_env):
    def infra():
        bucket = Bucket("site")
        CloudFrontDistribution("cdn", bucket=bucket, customize=NO_WAIT_DEPLOY)

    outputs = stelvio_env.deploy(infra)

    assert_s3_bucket(outputs["s3bucket_site_name"])
    assert_cloudfront_distribution(
        outputs["cloudfront_cdn_distribution_id"],
        enabled=True,
        origins_count=1,
        default_certificate=True,
        price_class="PriceClass_100",
    )


def test_cloudfront_price_class(stelvio_env):
    def infra():
        bucket = Bucket("static")
        CloudFrontDistribution(
            "global-cdn",
            bucket=bucket,
            price_class="PriceClass_All",
            customize=NO_WAIT_DEPLOY,
        )

    outputs = stelvio_env.deploy(infra)

    assert_cloudfront_distribution(
        outputs["cloudfront_global-cdn_distribution_id"],
        price_class="PriceClass_All",
    )


def test_cloudfront_exports(stelvio_env):
    def infra():
        bucket = Bucket("assets")
        CloudFrontDistribution("dist", bucket=bucket, customize=NO_WAIT_DEPLOY)

    outputs = stelvio_env.deploy(infra)

    assert outputs["cloudfront_dist_domain_name"].endswith(".cloudfront.net")
    assert outputs["cloudfront_dist_distribution_id"]
    assert outputs["cloudfront_dist_arn"].startswith("arn:aws:cloudfront:")
    assert outputs["cloudfront_dist_bucket_policy"]


def test_cloudfront_tags(stelvio_env):
    def infra():
        bucket = Bucket("tagged-site")
        CloudFrontDistribution(
            "tagged-cdn",
            bucket=bucket,
            tags={"Team": "platform"},
            customize=NO_WAIT_DEPLOY,
        )

    outputs = stelvio_env.deploy(infra)
    assert_cloudfront_tags(outputs["cloudfront_tagged-cdn_arn"], {"Team": "platform"})
