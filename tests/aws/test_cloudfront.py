import pytest

from stelvio.aws.cloudfront import CloudFrontDistribution
from stelvio.config import AwsConfig
from stelvio.context import AppContext, _ContextStore


class DummyBucket:
    resources = type(
        "obj",
        (),
        {
            "bucket": type(
                "obj",
                (),
                {
                    "bucket_regional_domain_name": "dummy-bucket.s3.amazonaws.com",
                    "id": "dummy-bucket-id",
                },
            ),
            "bucket_policy": None,
        },
    )
    arn = "arn:aws:s3:::dummy-bucket"


@pytest.fixture
def app_context_without_dns():
    _ContextStore.clear()
    _ContextStore.set(
        AppContext(
            name="test",
            env="test",
            aws=AwsConfig(profile="default", region="us-east-1"),
            dns=None,
        )
    )
    yield
    _ContextStore.clear()


def test_cloudfront_distribution_raises_without_dns(app_context_without_dns):
    bucket = DummyBucket()
    with pytest.raises(ValueError, match="DNS must be configured in StelvioApp"):
        CloudFrontDistribution(
            name="test-dist", bucket=bucket, custom_domain="example.com"
        )._create_resources()
