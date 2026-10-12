import json
import re

import pulumi
import pytest

from stelvio.aws.cloudfront import CloudFrontDistribution, Router
from stelvio.aws.cloudfront.dtos import Route
from stelvio.aws.s3 import Bucket, S3StaticWebsite
from stelvio.dns import DnsProviderNotConfiguredError

from ...conftest import TP
from ..conftest import assert_urn
from ..pulumi_mocks import ACCOUNT_ID, DEFAULT_REGION, R, provider_urn, tid, tn

pytestmark = pytest.mark.usefixtures("project_cwd")


@pytest.fixture
def mock_s3_bucket(pulumi_mocks, app_context_with_dns):
    """Create a mock S3 bucket for CloudFront tests"""
    return Bucket(name="test-bucket")


@pytest.fixture
def mock_s3_bucket_no_dns(pulumi_mocks, app_context_without_dns):
    """Create a mock S3 bucket for CloudFront tests (without DNS)"""
    return Bucket(name="test-bucket")


@pytest.mark.parametrize("price_class", ["PriceClass_100", "PriceClass_200", "PriceClass_All"])
def test_cloudfront_distribution_price_class(
    price_class, pulumi_mocks, app_context_with_dns, mock_s3_bucket
):
    distribution = CloudFrontDistribution(
        name="test-price",
        bucket=mock_s3_bucket,
        custom_domain="price.example.com",
        price_class=price_class,
    )

    @pulumi.runtime.test
    def deploy():
        return distribution.resources

    deploy()

    pulumi_mocks.assert_res(
        "test-price", R.DISTRIBUTION, {"priceClass": price_class}, partial=True
    )


def test_cloudfront_distribution_creates_all_resources(
    pulumi_mocks, app_context_with_dns, component_registry, mock_s3_bucket
):
    """Test that CloudFrontDistribution creates all expected AWS resources"""
    # Arrange
    distribution = CloudFrontDistribution(
        name="test-all-resources",
        bucket=mock_s3_bucket,
        custom_domain="all-resources.example.com",
    )

    # Act
    @pulumi.runtime.test
    def deploy():
        return distribution.resources

    deploy()

    # Assert
    certificate_id = tid(TP + "test-all-resources-acm-validated-domain-certificate")
    certificate_arn = f"arn:aws:acm:{DEFAULT_REGION}:{ACCOUNT_ID}:certificate/{certificate_id}"
    pulumi_mocks.assert_res(
        "test-all-resources",
        R.DISTRIBUTION,
        {
            "aliases": ["all-resources.example.com"],
            "enabled": True,
            "isIpv6Enabled": True,
            "priceClass": "PriceClass_100",
            "defaultRootObject": "index.html",
            "origins": [
                {
                    "originId": "test-all-resources-S3-Origin",
                    "domainName": f"{tn(TP + 'test-bucket')}.s3.{DEFAULT_REGION}.amazonaws.com",
                    "originAccessControlId": tid(TP + "test-all-resources-oac"),
                }
            ],
            "defaultCacheBehavior": {
                "targetOriginId": "test-all-resources-S3-Origin",
                "allowedMethods": ["GET", "HEAD", "OPTIONS"],
                "cachedMethods": ["GET", "HEAD"],
                "cachePolicyId": tid(TP + "test-all-resources-cache-policy"),
                "compress": True,
                "viewerProtocolPolicy": "redirect-to-https",
                "functionAssociations": [],
            },
            "customErrorResponses": [
                {
                    "errorCode": 403,
                    "responseCode": 404,
                    "responsePagePath": "/error.html",
                    "errorCachingMinTtl": 0,
                },
                {
                    "errorCode": 404,
                    "responseCode": 404,
                    "responsePagePath": "/error.html",
                    "errorCachingMinTtl": 300,
                },
            ],
            "restrictions": {"geoRestriction": {"restrictionType": "none"}},
            "viewerCertificate": {
                "acmCertificateArn": certificate_arn,
                "sslSupportMethod": "sni-only",
                "minimumProtocolVersion": "TLSv1.2_2021",
            },
        },
    )
    pulumi_mocks.assert_res(
        "test-all-resources-cache-policy",
        R.CACHE_POLICY,
        {
            "comment": "Cache policy for test-all-resources",
            "minTtl": 0,
            "defaultTtl": 300,
            "maxTtl": 3600,
            "parametersInCacheKeyAndForwardedToOrigin": {
                "cookiesConfig": {"cookieBehavior": "none"},
                "headersConfig": {
                    "headerBehavior": "whitelist",
                    "headers": {"items": ["If-Modified-Since"]},
                },
                "queryStringsConfig": {"queryStringBehavior": "none"},
                "enableAcceptEncodingGzip": True,
                "enableAcceptEncodingBrotli": True,
            },
        },
    )
    pulumi_mocks.assert_res(
        "test-all-resources-cloudfront-record",
        R.CLOUDFLARE_RECORD,
        {
            "zoneId": "test-zone-id",
            "name": "all-resources.example.com",
            "type": "CNAME",
            "content": f"{tid(TP + 'test-all-resources')}.cloudfront.net",
            "ttl": 1,
        },
    )
    pulumi_mocks.assert_res(
        "test-all-resources-acm-validated-domain-certificate",
        R.CERTIFICATE,
        {"domainName": "all-resources.example.com"},
        partial=True,
    )
    pulumi_mocks.assert_res_counts(
        {
            R.BUCKET: 1,
            R.BUCKET_PUBLIC_ACCESS_BLOCK: 1,
            R.DISTRIBUTION: 1,
            R.ORIGIN_ACCESS_CONTROL: 1,
            R.CACHE_POLICY: 1,
            R.BUCKET_POLICY: 1,
            R.CERTIFICATE: 1,
            R.CERTIFICATE_VALIDATION: 1,
            # cert validation + distribution CNAME
            R.CLOUDFLARE_RECORD: 2,
        }
    )


@pulumi.runtime.test
def test_cloudfront_parented(
    pulumi_mocks, app_context_with_dns, component_registry, mock_s3_bucket
):
    distribution = CloudFrontDistribution(
        name="test-cf-parented",
        bucket=mock_s3_bucket,
        custom_domain="parented.example.com",
    )
    record = distribution.resources.record
    acm = distribution.resources.acm_validated_domain
    assert record is not None
    assert acm is not None

    def check(urns):
        record_urn, acm_urn = urns
        assert_urn(
            record_urn,
            "stelvio:aws:CloudFrontDistribution",
            R.CLOUDFLARE_RECORD,
            TP + "test-cf-parented-cloudfront-record",
        )
        assert_urn(
            acm_urn,
            "stelvio:aws:CloudFrontDistribution",
            "stelvio:aws:AcmValidatedDomain",
            "test-cf-parented-acm-validated-domain",
        )

    return pulumi.Output.all(record.pulumi_resource.urn, acm.urn).apply(check)


@pulumi.runtime.test
def test_cloudfront_distribution_component_registry(
    pulumi_mocks, app_context_with_dns, component_registry, mock_s3_bucket
):
    """Test CloudFrontDistribution integrates properly with component registry"""
    # Arrange
    initial_count = len(component_registry._instances)

    distribution = CloudFrontDistribution(
        name="test-registry-cloudfront",
        bucket=mock_s3_bucket,
        custom_domain="registry.example.com",
    )

    # Act
    _ = distribution.resources

    # Assert - component should be registered along with its nested components
    # CloudFrontDistribution creates:
    # + CloudFrontDistribution
    # + AcmValidatedDomain
    # = 2 total (the AcmValidatedDomain also creates sub-components internally)
    assert len(component_registry._instances) == initial_count + 2
    assert "test-registry-cloudfront" in component_registry._registered_names


def test_cloudfront_distribution_origin_access_control_config(
    pulumi_mocks, app_context_with_dns, component_registry, mock_s3_bucket
):
    """Test CloudFront Origin Access Control configuration"""
    # Arrange
    distribution = CloudFrontDistribution(
        name="test-oac-config",
        bucket=mock_s3_bucket,
        custom_domain="oac.example.com",
    )

    # Act
    @pulumi.runtime.test
    def deploy():
        return distribution.resources

    deploy()

    # Assert
    [oac] = pulumi_mocks.created(R.ORIGIN_ACCESS_CONTROL)
    assert oac.inputs == {
        "description": "Origin Access Control for test-oac-config",
        "originAccessControlOriginType": "s3",
        "signingBehavior": "always",
        "signingProtocol": "sigv4",
    }


def test_cloudfront_distribution_s3_bucket_policy_creation(
    pulumi_mocks, app_context_with_dns, component_registry, mock_s3_bucket
):
    """Test that CloudFront creates appropriate S3 bucket policy"""
    # Arrange
    distribution = CloudFrontDistribution(
        name="test-bucket-policy",
        bucket=mock_s3_bucket,
        custom_domain="bucket-policy.example.com",
    )

    # Act
    @pulumi.runtime.test
    def deploy():
        return distribution.resources

    deploy()

    # Assert
    distribution_arn = (
        f"arn:aws:cloudfront::{ACCOUNT_ID}:distribution/{tid(TP + 'test-bucket-policy')}"
    )
    [policy] = pulumi_mocks.created(R.BUCKET_POLICY)
    assert policy.inputs["bucket"] == tid(TP + "test-bucket")
    assert json.loads(policy.inputs["policy"]) == {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "AllowCloudFrontServicePrincipal",
                "Effect": "Allow",
                "Principal": {"Service": "cloudfront.amazonaws.com"},
                "Action": "s3:GetObject",
                "Resource": f"arn:aws:s3:::{tn(TP + 'test-bucket')}/*",
                "Condition": {"StringEquals": {"AWS:SourceArn": distribution_arn}},
            }
        ],
    }


def test_cloudfront_distribution_raises_without_dns(mock_s3_bucket_no_dns):
    distribution = CloudFrontDistribution(
        name="test-no-dns",
        bucket=mock_s3_bucket_no_dns,
        custom_domain="cdn-nodns.example.com",
    )

    with pytest.raises(DnsProviderNotConfiguredError, match="DNS not configured"):
        _ = distribution.resources


def test_custom_domain_acm_uses_us_east_1_provider(
    pulumi_mocks, app_context_with_dns_eu_west, component_registry
):
    """Test that ACM certificate is created with a us-east-1 provider for CloudFront.

    CloudFront requires ACM certificates to be in us-east-1 regardless of the
    region used for other components.
    """
    bucket = Bucket(name="test-bucket")
    distribution = CloudFrontDistribution(
        name="test-cf-acm",
        bucket=bucket,
        custom_domain="cdn.example.com",
    )

    @pulumi.runtime.test
    def deploy():
        return [bucket.resources, distribution.resources]

    deploy()

    providers = pulumi_mocks.created(R.AWS_PROVIDER)
    assert {p.name: p.inputs["region"] for p in providers} == {
        "stelvio-aws": "eu-west-1",
        "stelvio-aws-us-east-1": "us-east-1",
    }
    [cert] = pulumi_mocks.created(R.CERTIFICATE)
    assert cert.provider == provider_urn("stelvio-aws-us-east-1")
    [validation] = pulumi_mocks.created(R.CERTIFICATE_VALIDATION)
    assert validation.provider == provider_urn("stelvio-aws-us-east-1")


def test_custom_domain_acm_skips_provider_when_already_us_east_1(
    pulumi_mocks, app_context_with_dns, component_registry
):
    """Test that no redundant us-east-1 provider is created when region is already us-east-1.

    When the user's configured region is us-east-1, CloudFront ACM certificates
    can use the default provider — no explicit provider is needed.
    """
    bucket = Bucket(name="test-bucket")
    distribution = CloudFrontDistribution(
        name="test-cf-acm-skip",
        bucket=bucket,
        custom_domain="cdn.example.com",
    )

    @pulumi.runtime.test
    def deploy():
        return [bucket.resources, distribution.resources]

    deploy()

    providers = pulumi_mocks.created(R.AWS_PROVIDER)
    assert {p.name: p.inputs["region"] for p in providers} == {"stelvio-aws": "us-east-1"}
    [cert] = pulumi_mocks.created(R.CERTIFICATE)
    assert cert.provider == provider_urn("stelvio-aws")
    [validation] = pulumi_mocks.created(R.CERTIFICATE_VALIDATION)
    assert validation.provider == provider_urn("stelvio-aws")


_CUSTOM_DOMAIN_COMPONENTS = [
    pytest.param(
        lambda domain: CloudFrontDistribution("cdn", Bucket("b"), custom_domain=domain),
        id="cloudfront",
    ),
    pytest.param(
        lambda domain: Router(
            "router",
            routes=[Route(path_pattern="/", component=Bucket("rb"))],
            custom_domain=domain,
        ),
        id="router",
    ),
    pytest.param(
        lambda domain: S3StaticWebsite("site", custom_domain=domain), id="static-website"
    ),
]


@pytest.mark.parametrize("build", _CUSTOM_DOMAIN_COMPONENTS)
@pytest.mark.parametrize(
    ("domain", "error", "match"),
    [
        (
            "my site.example.com",
            ValueError,
            "custom_domain labels may contain only letters, numbers, and hyphens",
        ),
        ("   ", ValueError, "custom_domain cannot be empty"),
        ("localhost", ValueError, "custom_domain must include at least one dot"),
        (
            "*.com",
            ValueError,
            "custom_domain wildcard must cover a domain with a dot, like *.example.com",
        ),
        (123, TypeError, "custom_domain must be a string"),
    ],
)
def test_custom_domain_rejected_at_definition(build, domain, error, match, pulumi_mocks):
    with pytest.raises(error, match=f"^{re.escape(match)}$"):
        build(domain)


@pytest.mark.parametrize("build", _CUSTOM_DOMAIN_COMPONENTS)
@pytest.mark.parametrize("domain", ["", None, "*.example.com", "app.example.com"])
def test_custom_domain_accepted(build, domain, pulumi_mocks):
    assert build(domain).custom_domain == domain


@pytest.mark.parametrize("build", _CUSTOM_DOMAIN_COMPONENTS)
@pytest.mark.parametrize("domain", [None, "*.example.com"])
def test_url_is_the_distribution_domain_without_a_single_host(
    build, domain, pulumi_mocks, app_context_with_dns, registered_outputs
):
    # `*.example.com` names no single host to open.
    component = build(domain)
    urls = []

    @pulumi.runtime.test
    def deploy():
        _ = component.resources
        return registered_outputs[component]["url"].apply(urls.append)

    deploy()

    [distribution] = pulumi_mocks.created(R.DISTRIBUTION)
    assert urls == [f"https://{tid(distribution.name)}.cloudfront.net"]


@pytest.mark.parametrize("build", _CUSTOM_DOMAIN_COMPONENTS)
def test_empty_custom_domain_deploys_without_a_domain(
    build, pulumi_mocks, app_context_with_dns, mock_dns
):
    @pulumi.runtime.test
    def deploy():
        return build("").resources

    deploy()

    pulumi_mocks.assert_no_res(R.CERTIFICATE, R.CERTIFICATE_VALIDATION)
    assert mock_dns.created_records == []
    [distribution] = pulumi_mocks.created(R.DISTRIBUTION)
    assert "aliases" not in distribution.inputs
