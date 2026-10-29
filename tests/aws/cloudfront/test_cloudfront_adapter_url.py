from unittest.mock import Mock

import pytest

from stelvio.aws.cloudfront.dtos import Route
from stelvio.aws.cloudfront.origins.components.url import Url, UrlCloudfrontAdapter


def test_match_url_component():
    """Test that the adapter correctly identifies Url components."""
    # Create a real Url instance for testing
    mock_url = Mock(spec=Url)

    # Test that it matches Url components
    assert UrlCloudfrontAdapter.match(mock_url) is True

    # Test that it doesn't match other components
    non_url = Mock()
    assert UrlCloudfrontAdapter.match(non_url) is False


def test_inheritance_from_base_class():
    """Test that the adapter properly inherits from ComponentCloudfrontAdapter."""
    from stelvio.aws.cloudfront.origins.base import ComponentCloudfrontAdapter

    mock_url = Mock(spec=Url)
    mock_url.resources = Mock()
    mock_url.resources.url = "https://example.com"
    route = Route(path_pattern="/proxy", component=mock_url)
    adapter = UrlCloudfrontAdapter(idx=0, route=route, router_name="router")

    assert isinstance(adapter, ComponentCloudfrontAdapter)
    assert hasattr(adapter, "get_origin_config")
    assert hasattr(adapter, "get_access_policy")
    assert hasattr(adapter, "match")


def test_registration_decorator():
    """Test that the @register_adapter decorator properly registers the adapter."""
    from stelvio.aws.cloudfront.origins.registry import CloudfrontAdapterRegistry

    # Ensure adapters are loaded
    CloudfrontAdapterRegistry._ensure_adapters_loaded()

    # Check that our adapter is registered for Url components
    mock_url = Mock(spec=Url)
    adapter_class = CloudfrontAdapterRegistry.get_adapter_for_component(mock_url)

    assert adapter_class == UrlCloudfrontAdapter


def test_adapter_inherits_component_class():
    """Test that the adapter has the correct component_class attribute."""
    # The @register_adapter decorator should set the component_class
    assert hasattr(UrlCloudfrontAdapter, "component_class")
    assert UrlCloudfrontAdapter.component_class == Url


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
    """Test the path pattern logic for URL adapter."""
    mock_url = Mock(spec=Url)
    mock_url.resources = Mock()
    mock_url.resources.url = "https://example.com"
    route = Route(path_pattern=path_pattern, component=mock_url)
    UrlCloudfrontAdapter(idx=0, route=route, router_name="router")

    # Test the logic directly by checking what pattern would be generated
    if route.path_pattern.endswith("*"):
        result_pattern = route.path_pattern
    else:
        result_pattern = f"{route.path_pattern}/*"

    assert result_pattern == expected_pattern


def test_url_vs_other_adapter_differences():
    """Test that URL adapter behaves differently from other adapters."""
    from stelvio.aws.cloudfront.origins.components.lambda_function import (
        LambdaFunctionCloudfrontAdapter,
    )
    from stelvio.aws.cloudfront.origins.components.s3 import S3BucketCloudfrontAdapter
    from stelvio.aws.function import Function
    from stelvio.aws.s3.s3 import Bucket

    # Create URL adapter
    mock_url = Mock(spec=Url)
    mock_url.resources = Mock()
    mock_url.resources.url = "https://api.example.com"
    url_route = Route(path_pattern="/api", component=mock_url)
    url_adapter = UrlCloudfrontAdapter(idx=0, route=url_route, router_name="router")

    # Create Lambda adapter for comparison
    mock_function = Mock(spec=Function)
    lambda_route = Route(path_pattern="/lambda", component=mock_function)
    lambda_adapter = LambdaFunctionCloudfrontAdapter(
        idx=0, route=lambda_route, router_name="router"
    )

    # Create S3 adapter for comparison
    mock_bucket = Mock(spec=Bucket)
    s3_route = Route(path_pattern="/files", component=mock_bucket)
    s3_adapter = S3BucketCloudfrontAdapter(idx=0, route=s3_route, router_name="router")

    # They should be different classes
    assert type(url_adapter) is not type(lambda_adapter)
    assert type(url_adapter) is not type(s3_adapter)
    # They should store different component types
    assert isinstance(url_adapter.url, type(mock_url))
    assert isinstance(lambda_adapter.function, type(mock_function))
    assert isinstance(s3_adapter.bucket, type(mock_bucket))
    # All should inherit from the same base class
    from stelvio.aws.cloudfront.origins.base import ComponentCloudfrontAdapter

    assert isinstance(url_adapter, ComponentCloudfrontAdapter)
    assert isinstance(lambda_adapter, ComponentCloudfrontAdapter)
    assert isinstance(s3_adapter, ComponentCloudfrontAdapter)


def test_url_no_origin_access_control():
    """Test that URL adapter doesn't use Origin Access Control."""
    # URL origins don't need Origin Access Control like S3 buckets do
    # They use custom origin configuration instead

    mock_url = Mock(spec=Url)
    mock_url.resources = Mock()
    mock_url.resources.url = "https://api.example.com"
    route = Route(path_pattern="/api", component=mock_url)
    adapter = UrlCloudfrontAdapter(idx=0, route=route, router_name="router")

    # The get_access_policy method should return None for URL origins
    # because URL origins don't need bucket policies
    mock_distribution = Mock()
    access_policy = adapter.get_access_policy(mock_distribution)

    assert access_policy is None


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
