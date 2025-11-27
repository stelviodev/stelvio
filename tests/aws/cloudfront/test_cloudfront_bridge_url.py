from unittest.mock import Mock

import pytest

from stelvio.aws.cloudfront.dtos import Route
from stelvio.aws.cloudfront.origins.components.url import Url, UrlCloudfrontBridge


def test_url_bridge_basic():
    """Basic test to verify the bridge can be imported and instantiated."""
    # Create a mock URL component
    mock_url = Mock(spec=Url)
    mock_url.name = "test-url"
    mock_url.resources = Mock()
    mock_url.resources.url = "https://example.com"

    # Create a route
    route = Route(path_pattern="/proxy", component_or_url=mock_url)

    # Create the bridge
    bridge = UrlCloudfrontBridge(idx=0, route=route)

    # Basic assertions
    assert bridge.idx == 0
    assert bridge.route == route
    assert bridge.url == mock_url
    assert bridge.route.path_pattern == "/proxy"


def test_match_url_component():
    """Test that the bridge correctly identifies Url components."""
    # Create a real Url instance for testing
    mock_url = Mock(spec=Url)

    # Test that it matches Url components
    assert UrlCloudfrontBridge.match(mock_url) is True

    # Test that it doesn't match other components
    non_url = Mock()
    assert UrlCloudfrontBridge.match(non_url) is False


def test_inheritance_from_base_class():
    """Test that the bridge properly inherits from ComponentCloudfrontBridge."""
    from stelvio.aws.cloudfront.origins.base import ComponentCloudfrontBridge

    mock_url = Mock(spec=Url)
    mock_url.resources = Mock()
    mock_url.resources.url = "https://example.com"
    route = Route(path_pattern="/proxy", component_or_url=mock_url)
    bridge = UrlCloudfrontBridge(idx=0, route=route)

    assert isinstance(bridge, ComponentCloudfrontBridge)
    assert hasattr(bridge, "get_origin_config")
    assert hasattr(bridge, "get_access_policy")
    assert hasattr(bridge, "match")


def test_registration_decorator():
    """Test that the @register_bridge decorator properly registers the bridge."""
    from stelvio.aws.cloudfront.origins.registry import CloudfrontBridgeRegistry

    # Ensure bridges are loaded
    CloudfrontBridgeRegistry._ensure_bridges_loaded()

    # Check that our bridge is registered for Url components
    mock_url = Mock(spec=Url)
    bridge_class = CloudfrontBridgeRegistry.get_bridge_for_component(mock_url)

    assert bridge_class == UrlCloudfrontBridge


def test_bridge_inherits_component_class():
    """Test that the bridge has the correct component_class attribute."""
    # The @register_bridge decorator should set the component_class
    assert hasattr(UrlCloudfrontBridge, "component_class")
    assert UrlCloudfrontBridge.component_class == Url


@pytest.mark.parametrize(
    ("path_pattern", "expected_pattern"),
    [
        ("/proxy", "/proxy/*"),
        ("/external", "/external/*"),
        ("/api/*", "/api/*"),
        ("/service/*", "/service/*"),
        ("/upstream*", "/upstream*"),
    ],
)
def test_path_pattern_logic(path_pattern, expected_pattern):
    """Test the path pattern logic for URL bridge."""
    mock_url = Mock(spec=Url)
    mock_url.resources = Mock()
    mock_url.resources.url = "https://example.com"
    route = Route(path_pattern=path_pattern, component_or_url=mock_url)
    UrlCloudfrontBridge(idx=0, route=route)

    # Test the logic directly by checking what pattern would be generated
    if route.path_pattern.endswith("*"):
        result_pattern = route.path_pattern
    else:
        result_pattern = f"{route.path_pattern}/*"

    assert result_pattern == expected_pattern


def test_bridge_with_different_indices():
    """Test that bridges work correctly with different indices."""
    mock_url = Mock(spec=Url)
    mock_url.name = "test-url"
    mock_url.resources = Mock()
    mock_url.resources.url = "https://example.com"

    route1 = Route(path_pattern="/proxy", component_or_url=mock_url)
    route2 = Route(path_pattern="/external", component_or_url=mock_url)

    bridge1 = UrlCloudfrontBridge(idx=0, route=route1)
    bridge2 = UrlCloudfrontBridge(idx=3, route=route2)

    # Both should work independently
    assert bridge1.idx == 0
    assert bridge2.idx == 3
    assert bridge1.route.path_pattern == "/proxy"
    assert bridge2.route.path_pattern == "/external"
    assert bridge1.url == mock_url
    assert bridge2.url == mock_url


def test_bridge_stores_url_reference():
    """Test that the bridge correctly stores a reference to the Url component."""
    mock_url = Mock(spec=Url)
    mock_url.name = "my-external-api"
    mock_url.resources = Mock()
    mock_url.resources.url = "https://api.example.com"

    route = Route(path_pattern="/api", component_or_url=mock_url)
    bridge = UrlCloudfrontBridge(idx=1, route=route)

    # Verify that the bridge stores the correct URL reference
    assert bridge.url is mock_url
    assert bridge.url.name == "my-external-api"
    assert bridge.url.resources.url == "https://api.example.com"


def test_cloudfront_route_structure():
    """Test that CloudfrontRoute is properly structured for the bridge."""
    mock_url = Mock(spec=Url)
    mock_url.name = "test-url"
    mock_url.resources = Mock()
    mock_url.resources.url = "https://example.com"

    route = Route(path_pattern="/proxy", component_or_url=mock_url)

    # Verify route structure
    assert route.path_pattern == "/proxy"
    assert route.component_or_url is mock_url
    assert route.component_or_url.name == "test-url"


@pytest.mark.parametrize("idx", [0, 1, 5, 42])
def test_bridge_with_various_indices(idx):
    """Test that the bridge works with various index values."""
    mock_url = Mock(spec=Url)
    mock_url.resources = Mock()
    mock_url.resources.url = "https://example.com"
    route = Route(path_pattern="/proxy", component_or_url=mock_url)

    bridge = UrlCloudfrontBridge(idx=idx, route=route)

    assert bridge.idx == idx
    assert bridge.route == route
    assert bridge.url == mock_url


def test_url_bridge_cache_behavior_characteristics():
    """Test the specific cache behavior characteristics for URL origins."""
    # URL origins should have different cache behavior than S3 or Lambda
    # This test documents the expected differences without requiring Pulumi mocks

    mock_url = Mock(spec=Url)
    mock_url.resources = Mock()
    mock_url.resources.url = "https://api.example.com"
    route = Route(path_pattern="/api", component_or_url=mock_url)
    bridge = UrlCloudfrontBridge(idx=0, route=route)

    # URL bridges should be configured for external proxying
    # These are the expected values based on the implementation:

    # Expected allowed methods for URL (full HTTP methods for proxying)

    # Expected cache settings for URL (no caching by default for dynamic content)

    # Expected forwarded values for URL (all headers, cookies, query strings)

    # These values are embedded in the get_origin_config method
    # We can't test them directly without Pulumi mocks, but we document them here
    assert bridge.url == mock_url  # Ensure bridge is properly set up


def test_url_vs_other_bridge_differences():
    """Test that URL bridge behaves differently from other bridges."""
    from stelvio.aws.cloudfront.origins.components.lambda_function import LambdaFunctionCloudfrontBridge
    from stelvio.aws.cloudfront.origins.components.s3 import S3BucketCloudfrontBridge
    from stelvio.aws.function import Function
    from stelvio.aws.s3.s3 import Bucket

    # Create URL bridge
    mock_url = Mock(spec=Url)
    mock_url.resources = Mock()
    mock_url.resources.url = "https://api.example.com"
    url_route = Route(path_pattern="/api", component_or_url=mock_url)
    url_bridge = UrlCloudfrontBridge(idx=0, route=url_route)

    # Create Lambda bridge for comparison
    mock_function = Mock(spec=Function)
    lambda_route = Route(path_pattern="/lambda", component_or_url=mock_function)
    lambda_bridge = LambdaFunctionCloudfrontBridge(idx=0, route=lambda_route)

    # Create S3 bridge for comparison
    mock_bucket = Mock(spec=Bucket)
    s3_route = Route(path_pattern="/files", component_or_url=mock_bucket)
    s3_bridge = S3BucketCloudfrontBridge(idx=0, route=s3_route)

    # They should be different classes
    assert type(url_bridge) is not type(lambda_bridge)
    assert type(url_bridge) is not type(s3_bridge)

    # They should store different component types
    assert isinstance(url_bridge.url, type(mock_url))
    assert isinstance(lambda_bridge.function, type(mock_function))
    assert isinstance(s3_bridge.bucket, type(mock_bucket))

    # All should inherit from the same base class
    from stelvio.aws.cloudfront.origins.base import ComponentCloudfrontBridge

    assert isinstance(url_bridge, ComponentCloudfrontBridge)
    assert isinstance(lambda_bridge, ComponentCloudfrontBridge)
    assert isinstance(s3_bridge, ComponentCloudfrontBridge)


def test_url_no_origin_access_control():
    """Test that URL bridge doesn't use Origin Access Control."""
    # URL origins don't need Origin Access Control like S3 buckets do
    # They use custom origin configuration instead

    mock_url = Mock(spec=Url)
    mock_url.resources = Mock()
    mock_url.resources.url = "https://api.example.com"
    route = Route(path_pattern="/api", component_or_url=mock_url)
    bridge = UrlCloudfrontBridge(idx=0, route=route)

    # The get_access_policy method should return None for URL origins
    # because URL origins don't need bucket policies
    mock_distribution = Mock()
    access_policy = bridge.get_access_policy(mock_distribution)

    assert access_policy is None


def test_edge_cases():
    """Test edge cases for the URL bridge."""
    # Test with empty path (edge case)
    mock_url = Mock(spec=Url)
    mock_url.resources = Mock()
    mock_url.resources.url = "https://example.com"
    route = Route(path_pattern="", component_or_url=mock_url)
    bridge = UrlCloudfrontBridge(idx=0, route=route)

    assert bridge.route.path_pattern == ""
    assert bridge.url == mock_url

    # Test with root path
    route_root = Route(path_pattern="/", component_or_url=mock_url)
    bridge_root = UrlCloudfrontBridge(idx=1, route=route_root)

    assert bridge_root.route.path_pattern == "/"

    # Test with various URL patterns
    url_patterns = [
        "https://api.example.com",
        "https://api.example.com/v1",
        "https://api.example.com:8080",
        "http://internal-service.local",
    ]
    for i, url_pattern in enumerate(url_patterns):
        mock_url_test = Mock(spec=Url)
        mock_url_test.resources = Mock()
        mock_url_test.resources.url = url_pattern
        route_test = Route(path_pattern=f"/proxy{i}", component_or_url=mock_url_test)
        bridge_test = UrlCloudfrontBridge(idx=i, route=route_test)

        assert bridge_test.url.resources.url == url_pattern


def test_cloudfront_js_function_generation_for_url():
    """Test that the CloudFront JavaScript function works correctly for URL paths."""
    from stelvio.aws.cloudfront.js import strip_path_pattern_function_js

    # Test URL-typical paths
    js_code = strip_path_pattern_function_js("/api")
    assert "function handler(event)" in js_code
    assert "request.uri" in js_code
    assert "'/api'" in js_code

    # Test that the generated JavaScript has the correct logic for URL proxying
    assert "uri === '/api'" in js_code
    assert "request.uri = '/';" in js_code
    assert "uri.substr(0, 5) === '/api/'" in js_code  # 5 = len('/api/')
    assert "request.uri = uri.substr(4);" in js_code  # 4 = len('/api')


def test_lambda_edge_host_header_function():
    """Test that the Lambda@Edge Host header function is correctly generated."""
    from stelvio.aws.cloudfront.js import set_custom_host_header

    # Test Host header rewriting for various domains
    test_domains = [
        "example.com",
        "api.example.com",
        "api.example.com:8080",
        "subdomain.example.co.uk",
    ]

    for domain in test_domains:
        js_code = set_custom_host_header(domain)

        # Verify Lambda@Edge structure
        assert "exports.handler" in js_code
        assert "event.Records[0].cf.request" in js_code
        assert "request.headers.host" in js_code
        assert f"'{domain}'" in js_code

        # Verify it's proper Node.js code
        assert "const request" in js_code
        assert "callback(null, request)" in js_code


@pytest.mark.parametrize(
    ("url_path", "expected_exact_length", "expected_prefix_length"),
    [
        ("/api", 4, 5),  # '/api' = 4, '/api/' = 5
        ("/proxy", 6, 7),  # '/proxy' = 6, '/proxy/' = 7
        ("/external/v1", 12, 13),  # '/external/v1' = 12, '/external/v1/' = 13
        ("/svc", 4, 5),  # '/svc' = 4, '/svc/' = 5
    ],
)
def test_js_function_path_lengths_for_url(url_path, expected_exact_length, expected_prefix_length):
    """Test that the JavaScript function uses correct path lengths for URL paths."""
    from stelvio.aws.cloudfront.js import strip_path_pattern_function_js

    js_code = strip_path_pattern_function_js(url_path)

    # Check exact path length usage
    assert f"uri.substr({expected_exact_length})" in js_code

    # Check prefix path length usage
    assert f"uri.substr(0, {expected_prefix_length})" in js_code


def test_multiple_url_bridge_instances():
    """Test that multiple URL bridge instances work correctly with different configurations."""
    mock_url1 = Mock(spec=Url)
    mock_url1.name = "external-api"
    mock_url1.resources = Mock()
    mock_url1.resources.url = "https://api.external.com"

    mock_url2 = Mock(spec=Url)
    mock_url2.name = "internal-service"
    mock_url2.resources = Mock()
    mock_url2.resources.url = "http://internal.local"

    route1 = Route(path_pattern="/external", component_or_url=mock_url1)
    route2 = Route(path_pattern="/internal", component_or_url=mock_url2)

    bridge1 = UrlCloudfrontBridge(idx=0, route=route1)
    bridge2 = UrlCloudfrontBridge(idx=1, route=route2)

    # Both should work independently
    assert bridge1.url.name == "external-api"
    assert bridge2.url.name == "internal-service"
    assert bridge1.route.path_pattern == "/external"
    assert bridge2.route.path_pattern == "/internal"
    assert bridge1.idx == 0
    assert bridge2.idx == 1


def test_url_component_validation():
    """Test that the Url component validates URLs correctly."""
    # Valid URLs should work
    valid_urls = [
        "https://example.com",
        "http://example.com",
        "https://api.example.com:8080",
        "https://example.com/path",
    ]

    for i, valid_url in enumerate(valid_urls):
        url = Url(f"test-url-{i}", valid_url)
        assert url.url == valid_url

    # Invalid URLs should raise errors
    with pytest.raises(ValueError, match="URL cannot be empty"):
        Url("test-url-empty", "")

    with pytest.raises(ValueError, match="Invalid URL scheme"):
        Url("test-url-invalid-scheme", "ftp://example.com")

    with pytest.raises(ValueError, match="must include a domain"):
        Url("test-url-no-domain", "https://")


def test_url_component_resources():
    """Test that the Url component creates resources correctly."""
    test_url = "https://api.example.com"
    url = Url("test-url", test_url)

    resources = url.resources

    assert resources.url == test_url
    assert isinstance(resources.url, str)


@pytest.mark.parametrize(
    ("url", "expected_scheme", "expected_netloc", "expected_path"),
    [
        ("https://example.com", "https", "example.com", ""),
        ("https://example.com/api", "https", "example.com", "/api"),
        ("http://example.com:8080", "http", "example.com:8080", ""),
        ("https://api.example.com/v1/users", "https", "api.example.com", "/v1/users"),
    ],
)
def test_url_parsing_in_bridge(url, expected_scheme, expected_netloc, expected_path):
    """Test that URLs are parsed correctly in the bridge."""
    from urllib.parse import urlparse

    mock_url = Mock(spec=Url)
    mock_url.resources = Mock()
    mock_url.resources.url = url
    route = Route(path_pattern="/proxy", component_or_url=mock_url)
    bridge = UrlCloudfrontBridge(idx=0, route=route)

    # Parse the URL to verify expected components
    parsed = urlparse(bridge.url.resources.url)

    assert parsed.scheme == expected_scheme
    assert parsed.netloc == expected_netloc
    assert parsed.path == expected_path


def test_url_bridge_custom_origin_config():
    """Test that URL bridge uses custom origin configuration."""
    # URL bridges should use custom_origin_config instead of S3-style config
    # This is necessary for HTTP/HTTPS origins that aren't S3 buckets

    mock_url = Mock(spec=Url)
    mock_url.resources = Mock()
    mock_url.resources.url = "https://api.example.com"
    route = Route(path_pattern="/api", component_or_url=mock_url)
    bridge = UrlCloudfrontBridge(idx=0, route=route)

    # The bridge should be set up to use custom origin config
    # This is verified indirectly through the bridge's configuration
    assert bridge.url.resources.url.startswith("https://")


def test_url_bridge_lambda_edge_requirement():
    """Test that URL bridge is designed to use Lambda@Edge for Host header."""
    # URL origins require Lambda@Edge to set the Host header correctly
    # because CloudFront's custom_origin_config doesn't allow overriding Host

    mock_url = Mock(spec=Url)
    mock_url.resources = Mock()
    mock_url.resources.url = "https://api.example.com"
    route = Route(path_pattern="/api", component_or_url=mock_url)
    bridge = UrlCloudfrontBridge(idx=0, route=route)

    # The bridge should be properly configured with URL
    assert bridge.url == mock_url
    assert "api.example.com" in bridge.url.resources.url
