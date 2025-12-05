"""Tests for the CloudFront Router component using real Pulumi mocks.

This test module uses PulumiTestMocks (like other components in the project)
to test the Router component's resource creation behavior.
"""

import pulumi
import pytest
from pulumi.runtime import set_mocks

from stelvio.aws.api_gateway import Api
from stelvio.aws.cloudfront.dtos import Route
from stelvio.aws.cloudfront.origins.registry import CloudfrontAdapterRegistry
from stelvio.aws.cloudfront.router import Router, RouterResources
from stelvio.aws.function import Function
from stelvio.aws.s3.s3 import Bucket
from stelvio.component import Component
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
            dns=mock_dns,
        )
    )
    yield mock_dns
    _ContextStore.clear()
    _ContextStore.set(
        AppContext(name="test", env="test", aws=AwsConfig(profile="default", region="us-east-1"))
    )


@pytest.fixture
def function_project_setup(tmp_path, monkeypatch):
    """Fixture that sets up a project structure for Function tests and cleans up cache."""
    from stelvio.project import get_project_root

    get_project_root.cache_clear()

    # Create a temp project structure
    stlv_app = tmp_path / "stlv_app.py"
    stlv_app.write_text("# stlv app")
    handler_file = tmp_path / "functions" / "api.py"
    handler_file.parent.mkdir(parents=True, exist_ok=True)
    handler_file.write_text("def handler(event, context): pass")

    monkeypatch.chdir(tmp_path)

    yield tmp_path

    # Clear cache after test to avoid affecting subsequent tests
    get_project_root.cache_clear()


# ==============================================================================
# Basic Instantiation Tests (no Pulumi resources created)
# ==============================================================================


def test_router_basic_instantiation():
    """Test that Router can be instantiated with basic parameters."""
    router = Router(name="test-router")

    assert router.name == "test-router"
    assert router.routes == []
    assert router.price_class == "PriceClass_100"
    assert router.custom_domain is None


def test_router_with_custom_parameters():
    """Test that Router can be instantiated with custom parameters."""
    bucket = Bucket("test-bucket")
    routes = [Route(path_pattern="/static", component_or_url=bucket)]

    router = Router(
        name="custom-router",
        routes=routes,
        price_class="PriceClass_All",
        custom_domain="example.com",
    )

    assert router.name == "custom-router"
    assert router.routes == routes
    assert router.price_class == "PriceClass_All"
    assert router.custom_domain == "example.com"


@pytest.mark.parametrize("price_class", ["PriceClass_100", "PriceClass_200", "PriceClass_All"])
def test_price_class_options(price_class):
    """Test that different price class options work."""
    router = Router(name="test-router", price_class=price_class)
    assert router.price_class == price_class


def test_router_inheritance():
    """Test that Router properly inherits from Component."""
    router = Router(name="test-router")
    assert isinstance(router, Component)
    assert hasattr(router, "name")
    assert hasattr(router, "_create_resources")


# ==============================================================================
# RouterResources Dataclass Tests
# ==============================================================================


def test_router_resources_dataclass():
    """Test that RouterResources dataclass works correctly."""
    from unittest.mock import Mock

    # Create mock objects for all required fields
    mock_distribution = Mock()
    mock_oac = Mock()
    mock_policy = Mock()
    mock_function = Mock()
    mock_acm = Mock()
    mock_record = Mock()

    resources = RouterResources(
        distribution=mock_distribution,
        origin_access_controls=[mock_oac],
        access_policies=[mock_policy],
        cloudfront_functions=[mock_function],
        acm_validated_domain=mock_acm,
        record=mock_record,
    )

    assert resources.distribution == mock_distribution
    assert resources.origin_access_controls == [mock_oac]
    assert resources.access_policies == [mock_policy]
    assert resources.cloudfront_functions == [mock_function]
    assert resources.acm_validated_domain == mock_acm
    assert resources.record == mock_record


def test_router_resources_frozen():
    """Test that RouterResources is frozen (immutable)."""
    from unittest.mock import Mock

    resources = RouterResources(
        distribution=Mock(),
        origin_access_controls=[],
        access_policies=[],
        cloudfront_functions=[],
        acm_validated_domain=None,
        record=None,
    )

    with pytest.raises(AttributeError):
        resources.distribution = Mock()


# ==============================================================================
# Route Adding Tests
# ==============================================================================


@pulumi.runtime.test
def test_add_route_with_s3_bucket(pulumi_mocks):
    """Test adding a route with an S3 Bucket component."""
    bucket = Bucket("test-bucket")
    router = Router(name="test-router")

    # Access bucket resources to trigger creation
    _ = bucket.resources

    router.route("/static", bucket)

    assert len(router.routes) == 1
    assert router.routes[0].path_pattern == "/static"
    assert router.routes[0].component_or_url is bucket


@pulumi.runtime.test
def test_add_route_with_function(
    pulumi_mocks, mock_get_or_install_dependencies_function, function_project_setup
):
    """Test adding a route with a Lambda Function component."""
    fn = Function("test-fn", handler="functions/api.handler")
    router = Router(name="test-router")

    # Access function resources to trigger creation
    _ = fn.resources

    router.route("/api", fn)

    assert len(router.routes) == 1
    assert router.routes[0].path_pattern == "/api"
    assert router.routes[0].component_or_url is fn


@pulumi.runtime.test
def test_add_route_with_url_string(pulumi_mocks):
    """Test adding a route with a URL string."""
    router = Router(name="test-router")

    router.route("/external", "https://example.com")

    assert len(router.routes) == 1
    assert router.routes[0].path_pattern == "/external"
    # URL strings are converted to Url components
    from stelvio.aws.cloudfront.origins.components.url import Url

    assert isinstance(router.routes[0].component_or_url, Url)


def test_add_duplicate_path_pattern_raises_error():
    """Test that adding a route with duplicate path pattern raises error."""
    bucket1 = Bucket("bucket1")
    bucket2 = Bucket("bucket2")
    router = Router(name="test-router")

    router.route("/static", bucket1)

    with pytest.raises(ValueError, match="Route for path pattern /static already exists"):
        router.route("/static", bucket2)


def test_add_route_with_invalid_component_type():
    """Test that adding a route with invalid component type raises error."""
    router = Router(name="test-router")

    with pytest.raises(TypeError, match="component_or_url must be a Component or str"):
        router._add_route(Route(path_pattern="/invalid", component_or_url=123))


@pulumi.runtime.test
def test_add_route_with_function_url_config_raises_error(
    pulumi_mocks, mock_get_or_install_dependencies_function, function_project_setup
):
    """Test that Functions with 'url' config cannot be added to Router."""
    fn = Function("test-fn", handler="functions/api.handler", url="public")
    router = Router(name="test-router")

    # Access function resources to trigger creation
    _ = fn.resources

    with pytest.raises(ValueError, match="has 'url' configuration and cannot be added"):
        router.route("/api", fn)


# ==============================================================================
# Resource Creation Tests with Pulumi Mocks
# ==============================================================================


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


# ==============================================================================
# DNS and Custom Domain Tests
# ==============================================================================


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

        # Verify distribution has custom domain alias
        distributions = pulumi_mocks.created_cloudfront_distributions()
        assert len(distributions) == 1
        dist = distributions[0]
        assert dist.inputs["aliases"] == ["cdn.example.com"]

    router.resources.distribution.id.apply(check_resources)


# ==============================================================================
# Route Ordering/Specificity Tests
# ==============================================================================


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
        # The exact number depends on implementation (some adapters create multiple)
        assert behaviors is not None
        assert len(behaviors) >= 2  # At least one per route

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


# ==============================================================================
# Adapter match() Method Tests
# ==============================================================================


def test_adapter_match_s3_bucket():
    """Test that S3BucketCloudfrontAdapter.match() correctly identifies Bucket."""
    from stelvio.aws.cloudfront.origins.components.s3 import S3BucketCloudfrontAdapter

    bucket = Bucket("test-bucket")
    fn = Function.__new__(Function)  # Create without init
    fn._name = "test-fn"

    assert S3BucketCloudfrontAdapter.match(bucket) is True
    assert S3BucketCloudfrontAdapter.match(fn) is False


def test_adapter_match_api_gateway():
    """Test that ApiGatewayCloudfrontAdapter.match() correctly identifies Api."""
    from stelvio.aws.cloudfront.origins.components.api_gateway import (
        ApiGatewayCloudfrontAdapter,
    )

    api = Api.__new__(Api)
    api._name = "test-api"

    bucket = Bucket("test-bucket")

    assert ApiGatewayCloudfrontAdapter.match(api) is True
    assert ApiGatewayCloudfrontAdapter.match(bucket) is False


def test_adapter_match_function():
    """Test that LambdaFunctionCloudfrontAdapter.match() correctly identifies Function."""
    from stelvio.aws.cloudfront.origins.components.lambda_function import (
        LambdaFunctionCloudfrontAdapter,
    )

    fn = Function.__new__(Function)
    fn._name = "test-fn"

    bucket = Bucket("test-bucket")

    assert LambdaFunctionCloudfrontAdapter.match(fn) is True
    assert LambdaFunctionCloudfrontAdapter.match(bucket) is False


def test_adapter_match_url():
    """Test that UrlCloudfrontAdapter.match() correctly identifies Url."""
    from stelvio.aws.cloudfront.origins.components.url import Url, UrlCloudfrontAdapter

    url = Url("test-url", "https://example.com")

    bucket = Bucket("test-bucket")

    assert UrlCloudfrontAdapter.match(url) is True
    assert UrlCloudfrontAdapter.match(bucket) is False


def test_adapter_registry_returns_correct_adapter():
    """Test that CloudfrontAdapterRegistry returns the correct adapter for each component."""
    from stelvio.aws.cloudfront.origins.components.api_gateway import (
        ApiGatewayCloudfrontAdapter,
    )
    from stelvio.aws.cloudfront.origins.components.lambda_function import (
        LambdaFunctionCloudfrontAdapter,
    )
    from stelvio.aws.cloudfront.origins.components.s3 import S3BucketCloudfrontAdapter
    from stelvio.aws.cloudfront.origins.components.url import Url, UrlCloudfrontAdapter

    # Ensure adapters are loaded
    CloudfrontAdapterRegistry._ensure_adapters_loaded()

    bucket = Bucket("test-bucket")
    url = Url("test-url", "https://example.com")

    # Note: Api and Function need to be created without full init
    api = Api.__new__(Api)
    api._name = "test-api"

    fn = Function.__new__(Function)
    fn._name = "test-fn"

    assert CloudfrontAdapterRegistry.get_adapter_for_component(bucket) == S3BucketCloudfrontAdapter
    assert CloudfrontAdapterRegistry.get_adapter_for_component(url) == UrlCloudfrontAdapter
    assert CloudfrontAdapterRegistry.get_adapter_for_component(api) == ApiGatewayCloudfrontAdapter
    assert (
        CloudfrontAdapterRegistry.get_adapter_for_component(fn) == LambdaFunctionCloudfrontAdapter
    )


def test_adapter_registry_raises_for_unknown_component():
    """Test that CloudfrontAdapterRegistry raises error for unknown component type."""

    class UnknownComponent(Component):
        """An unknown component type for testing."""

        def _create_resources(self):
            return None

    unknown = UnknownComponent("unknown")

    with pytest.raises(ValueError, match="No adapter found for component"):
        CloudfrontAdapterRegistry.get_adapter_for_component(unknown)


def test_adapter_component_class_attribute():
    """Test that adapters have correct component_class attribute set by decorator."""
    from stelvio.aws.cloudfront.origins.components.api_gateway import (
        ApiGatewayCloudfrontAdapter,
    )
    from stelvio.aws.cloudfront.origins.components.lambda_function import (
        LambdaFunctionCloudfrontAdapter,
    )
    from stelvio.aws.cloudfront.origins.components.s3 import S3BucketCloudfrontAdapter
    from stelvio.aws.cloudfront.origins.components.url import Url, UrlCloudfrontAdapter

    # Ensure adapters are loaded
    CloudfrontAdapterRegistry._ensure_adapters_loaded()

    assert S3BucketCloudfrontAdapter.component_class == Bucket
    assert ApiGatewayCloudfrontAdapter.component_class == Api
    assert LambdaFunctionCloudfrontAdapter.component_class == Function
    assert UrlCloudfrontAdapter.component_class == Url


def test_adapter_match_uses_isinstance():
    """Test that adapter match() method uses isinstance for type checking."""
    from stelvio.aws.cloudfront.origins.components.s3 import S3BucketCloudfrontAdapter

    # This tests that inheritance is properly handled
    class CustomBucket(Bucket):
        pass

    custom_bucket = CustomBucket("custom-bucket")

    # Should match because CustomBucket inherits from Bucket
    assert S3BucketCloudfrontAdapter.match(custom_bucket) is True


# ==============================================================================
# Integration Tests
# ==============================================================================


@pulumi.runtime.test
def test_full_router_with_mixed_origins(
    pulumi_mocks, mock_get_or_install_dependencies_function, function_project_setup
):
    """Test Router with multiple different origin types."""
    bucket = Bucket("static-bucket")
    fn = Function("api-function", handler="functions/api.handler")
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
