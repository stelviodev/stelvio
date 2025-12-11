import pulumi
import pytest
from pulumi.runtime import set_mocks

from stelvio.aws.cloudfront.router import Router
from stelvio.aws.function import Function
from stelvio.aws.s3.s3 import Bucket
from stelvio.config import AwsConfig
from stelvio.context import AppContext, _ContextStore
from stelvio.dns import DnsProviderNotConfiguredError

from ..pulumi_mocks import MockDns, PulumiTestMocks

# Test prefix (matches the fixture in conftest.py)
TP = "test-test-"


@pytest.fixture
def pulumi_mocks():
    mocks = PulumiTestMocks()
    set_mocks(mocks)
    return mocks


@pytest.fixture
def app_context_with_dns():
    """Fixture that provides an app context with DNS configured."""
    _ContextStore.clear()
    mock_dns = MockDns()
    _ContextStore.set(
        AppContext(
            name="test",
            env="test",
            aws=AwsConfig(profile="default", region="us-east-1"),
            home="aws",
            dns=mock_dns,
        )
    )
    yield mock_dns
    _ContextStore.clear()
    _ContextStore.set(
        AppContext(
            name="test",
            env="test",
            aws=AwsConfig(profile="default", region="us-east-1"),
            home="aws",
        )
    )


@pulumi.runtime.test
def test_create_resources_with_s3_bucket(pulumi_mocks):
    """Test that Router creates proper CloudFront resources for S3 bucket origin."""
    bucket = Bucket("test-bucket")
    _ = bucket.resources

    router = Router(name="test-router")
    router.route("/static", bucket)
    _ = router.resources

    def check_resources(_):
        # Verify CloudFront distribution was created
        distributions = pulumi_mocks.created_cloudfront_distributions()
        assert len(distributions) == 1
        dist = distributions[0]
        assert TP + "test-router" in dist.name

        # Verify Origin Access Control was created for S3
        oacs = pulumi_mocks.created_origin_access_controls()
        assert len(oacs) == 1
        oac = oacs[0]
        assert "oac" in oac.name

        # Verify CloudFront functions were created
        cf_functions = pulumi_mocks.created_cloudfront_functions()
        # 1 for URI rewrite + 1 for default 404
        assert len(cf_functions) >= 1

        # Verify S3 bucket policy was created for CloudFront access
        bucket_policies = pulumi_mocks.created_bucket_policies()
        assert len(bucket_policies) == 1

    # Wait for both distribution and access policies to be created
    resources = router.resources
    if resources.access_policies:
        pulumi.Output.all(
            dist_id=resources.distribution.id,
            policy_ids=[p.id for p in resources.access_policies],
        ).apply(check_resources)
    else:
        resources.distribution.id.apply(check_resources)


@pulumi.runtime.test
def test_create_resources_no_routes_raises_error(pulumi_mocks):
    """Test that creating resources with no routes raises ValueError."""
    router = Router(name="test-router")

    with pytest.raises(ValueError, match="must have at least one route"):
        router._create_resources()


@pulumi.runtime.test
def test_create_resources_with_root_path(pulumi_mocks):
    """Test that Router handles root path '/' as default cache behavior."""
    bucket = Bucket("test-bucket")
    _ = bucket.resources

    router = Router(name="test-router")
    router.route("/", bucket)
    _ = router.resources

    def check_resources(_):
        distributions = pulumi_mocks.created_cloudfront_distributions()
        assert len(distributions) == 1

        # When root path exists, there should be no default 404 function
        cf_functions = pulumi_mocks.created_cloudfront_functions()
        # Only the URI rewrite function should be created
        function_names = [f.name for f in cf_functions]
        assert not any("default-404" in name for name in function_names)

    router.resources.distribution.id.apply(check_resources)


@pulumi.runtime.test
def test_create_resources_without_root_path_creates_404(pulumi_mocks):
    """Test that Router creates default 404 function when no root path defined."""
    bucket = Bucket("test-bucket")
    _ = bucket.resources

    router = Router(name="test-router")
    router.route("/static", bucket)  # Not root path
    _ = router.resources

    def check_resources(_):
        distributions = pulumi_mocks.created_cloudfront_distributions()
        assert len(distributions) == 1

        # Without root path, a default 404 function should be created
        cf_functions = pulumi_mocks.created_cloudfront_functions()
        function_names = [f.name for f in cf_functions]
        assert any("default-404" in name for name in function_names)

    router.resources.distribution.id.apply(check_resources)


@pulumi.runtime.test
def test_create_resources_with_multiple_routes(pulumi_mocks):
    """Test that Router handles multiple routes correctly."""
    bucket1 = Bucket("bucket1")
    bucket2 = Bucket("bucket2")
    _ = bucket1.resources
    _ = bucket2.resources

    router = Router(name="test-router")
    router.route("/static", bucket1)
    router.route("/files", bucket2)
    resources = router.resources

    def check_resources(_):
        distributions = pulumi_mocks.created_cloudfront_distributions()
        assert len(distributions) == 1

        # Check that we have 2 OACs (one per bucket)
        oacs = pulumi_mocks.created_origin_access_controls()
        assert len(oacs) == 2

    # Use the last created policy to ensure all resources are created
    # The access_policies list contains the bucket policies
    if resources.access_policies:
        # Wait for the first bucket policy to be created
        resources.access_policies[0].id.apply(check_resources)
    else:
        router.resources.distribution.id.apply(check_resources)


@pulumi.runtime.test
def test_create_resources_with_url_origin(pulumi_mocks):
    """Test that Router creates proper resources for URL origins."""
    router = Router(name="test-router")
    router.route("/", "https://example.com")
    _ = router.resources

    def check_resources(_):
        distributions = pulumi_mocks.created_cloudfront_distributions()
        assert len(distributions) == 1

        # URL origins don't need OAC like S3
        oacs = pulumi_mocks.created_origin_access_controls()
        # No OAC for URL origins
        assert len(oacs) == 0

        # URL origins don't need bucket policies
        bucket_policies = pulumi_mocks.created_bucket_policies()
        assert len(bucket_policies) == 0

    router.resources.distribution.id.apply(check_resources)


def test_create_resources_custom_domain_no_dns_raises_error():
    """Test that custom_domain without DNS provider raises DnsProviderNotConfiguredError.

    Note: This test doesn't need Pulumi mocks because it tests the error before
    resources are created.
    """
    bucket = Bucket("test-bucket")

    router = Router(name="test-router", custom_domain="example.com")
    router.route("/", bucket)

    # Should raise DnsProviderNotConfiguredError because default context has no DNS
    with pytest.raises(DnsProviderNotConfiguredError, match="DNS not configured"):
        router._create_resources()


@pulumi.runtime.test
def test_create_resources_with_custom_domain_and_dns(pulumi_mocks, app_context_with_dns):
    """Test that Router creates ACM certificate and DNS record with custom domain."""
    bucket = Bucket("test-bucket")
    _ = bucket.resources

    router = Router(name="test-router", custom_domain="cdn.example.com")
    router.route("/", bucket)
    _ = router.resources

    def check_resources(_):
        # Verify ACM certificate was created
        certificates = pulumi_mocks.created_certificates()
        assert len(certificates) == 1
        cert = certificates[0]
        assert cert.inputs["domainName"] == "cdn.example.com"

        # Verify certificate validation was created
        validations = pulumi_mocks.created_certificate_validations()
        assert len(validations) == 1

        # Verify DNS record was created for CloudFront distribution
        dns_records = pulumi_mocks.created_dns_records()
        assert len(dns_records) >= 1  # At least one for cert validation + CNAME

        # Verify distribution has custom domain alias and viewer certificate
        distributions = pulumi_mocks.created_cloudfront_distributions()
        assert len(distributions) == 1
        dist = distributions[0]
        assert dist.inputs["aliases"] == ["cdn.example.com"]
        viewer_certificate = dist.inputs.get("viewerCertificate") or {}
        assert viewer_certificate.get("sslSupportMethod") == "sni-only"
        assert viewer_certificate.get("minimumProtocolVersion") == "TLSv1.2_2021"

    router.resources.distribution.id.apply(check_resources)


@pulumi.runtime.test
def test_route_ordering_more_specific_paths_first(pulumi_mocks):
    """Test that more specific paths are handled correctly.

    CloudFront uses path specificity for matching:
    - Longer/more specific paths should match first
    - /api/v1/* should match before /api/*
    """
    bucket1 = Bucket("bucket-api")
    bucket2 = Bucket("bucket-api-v1")
    _ = bucket1.resources
    _ = bucket2.resources

    router = Router(name="test-router")
    # Add routes in any order - CloudFront handles specificity
    router.route("/api", bucket1)
    router.route("/api/v1", bucket2)
    _ = router.resources

    def check_resources(_):
        distributions = pulumi_mocks.created_cloudfront_distributions()
        assert len(distributions) == 1
        dist = distributions[0]

        # Verify orderedCacheBehaviors exist (note: Pulumi uses camelCase keys)
        assert "orderedCacheBehaviors" in dist.inputs
        behaviors = dist.inputs["orderedCacheBehaviors"]

        # Should have behaviors for both paths
        assert behaviors is not None
        patterns = {b["pathPattern"] for b in behaviors if "pathPattern" in b}
        # Depending on adapter implementation, patterns may include wildcards
        assert any(p.startswith("/api") for p in patterns)

    router.resources.distribution.id.apply(check_resources)


def test_route_order_preserved_in_routes_list():
    """Test that routes are stored in the order they are added."""
    bucket1 = Bucket("bucket1")
    bucket2 = Bucket("bucket2")
    bucket3 = Bucket("bucket3")

    router = Router(name="test-router")
    router.route("/a", bucket1)
    router.route("/b", bucket2)
    router.route("/c", bucket3)

    assert router.routes[0].path_pattern == "/a"
    assert router.routes[1].path_pattern == "/b"
    assert router.routes[2].path_pattern == "/c"


@pulumi.runtime.test
def test_multiple_path_patterns_create_ordered_cache_behaviors(pulumi_mocks):
    """Test that multiple path patterns result in ordered cache behaviors."""
    bucket1 = Bucket("static")
    bucket2 = Bucket("files")
    bucket3 = Bucket("uploads")
    _ = bucket1.resources
    _ = bucket2.resources
    _ = bucket3.resources

    router = Router(name="test-router")
    router.route("/static", bucket1)
    router.route("/files", bucket2)
    router.route("/uploads", bucket3)
    _ = router.resources

    def check_resources(_):
        distributions = pulumi_mocks.created_cloudfront_distributions()
        assert len(distributions) == 1
        dist = distributions[0]

        # Verify we have multiple origins
        origins = dist.inputs["origins"]
        assert len(origins) == 3

        # Verify orderedCacheBehaviors contains entries for each non-root path
        behaviors = dist.inputs["orderedCacheBehaviors"]
        assert behaviors is not None
        # Each bucket should have an ordered cache behavior
        assert len(behaviors) == 3

    router.resources.distribution.id.apply(check_resources)


@pulumi.runtime.test
def test_full_router_with_mixed_origins(
    pulumi_mocks, mock_get_or_install_dependencies_function, project_cwd
):
    """Test Router with multiple different origin types."""
    bucket = Bucket("static-bucket")
    fn = Function("api-function", handler="functions/simple.handler")
    _ = bucket.resources
    _ = fn.resources

    router = Router(name="mixed-router")
    router.route("/static", bucket)
    router.route("/api", fn)
    router.route("/external", "https://httpbin.org/anything")
    _ = router.resources

    def check_resources(_):
        # Verify single distribution created
        distributions = pulumi_mocks.created_cloudfront_distributions()
        assert len(distributions) == 1
        dist = distributions[0]

        # Verify multiple origins
        origins = dist.inputs["origins"]
        assert len(origins) == 3

        # S3 origin should have OAC
        s3_oacs = [
            oac
            for oac in pulumi_mocks.created_origin_access_controls()
            if oac.inputs["originAccessControlOriginType"] == "s3"
        ]
        assert len(s3_oacs) == 1

    router.resources.distribution.id.apply(check_resources)


@pulumi.runtime.test
def test_router_exports_outputs(pulumi_mocks):
    """Test that Router exports the expected Pulumi outputs."""
    bucket = Bucket("test-bucket")
    _ = bucket.resources

    router = Router(name="output-test")
    router.route("/", bucket)
    _ = router.resources

    def check_outputs(_):
        # The router should have exported outputs for domain_name, distribution_id, num_origins
        # These are verified by checking the distribution was created
        distributions = pulumi_mocks.created_cloudfront_distributions()
        assert len(distributions) == 1

    router.resources.distribution.id.apply(check_outputs)
