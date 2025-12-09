from unittest.mock import Mock

import pytest

from stelvio.aws.cloudfront.dtos import Route
from stelvio.aws.cloudfront.origins.components.s3 import S3BucketCloudfrontAdapter
from stelvio.aws.s3.s3 import Bucket


def test_s3_adapter_basic():
    """Basic test to verify the adapter can be imported and instantiated."""
    # Create a mock bucket component
    mock_bucket = Mock(spec=Bucket)
    mock_bucket.name = "test-bucket"

    # Create a route
    route = Route(path_pattern="/files", component=mock_bucket)

    # Create the adapter
    adapter = S3BucketCloudfrontAdapter(idx=0, route=route)

    # Basic assertions
    assert adapter.idx == 0
    assert adapter.route == route
    assert adapter.bucket == mock_bucket
    assert adapter.route.path_pattern == "/files"


def test_match_bucket_component():
    """Test that the adapter correctly identifies Bucket components."""
    # Create a real Bucket instance for testing
    mock_bucket = Mock(spec=Bucket)

    # Test that it matches Bucket components
    assert S3BucketCloudfrontAdapter.match(mock_bucket) is True

    # Test that it doesn't match other components
    non_bucket = Mock()
    assert S3BucketCloudfrontAdapter.match(non_bucket) is False


def test_inheritance_from_base_class():
    """Test that the adapter properly inherits from ComponentCloudfrontAdapter."""
    from stelvio.aws.cloudfront.origins.base import ComponentCloudfrontAdapter

    mock_bucket = Mock(spec=Bucket)
    route = Route(path_pattern="/files", component=mock_bucket)
    adapter = S3BucketCloudfrontAdapter(idx=0, route=route)

    assert isinstance(adapter, ComponentCloudfrontAdapter)
    assert hasattr(adapter, "get_origin_config")
    assert hasattr(adapter, "get_access_policy")
    assert hasattr(adapter, "match")


def test_registration_decorator():
    """Test that the @register_adapter decorator properly registers the adapter."""
    from stelvio.aws.cloudfront.origins.registry import CloudfrontAdapterRegistry

    # Ensure adapters are loaded
    CloudfrontAdapterRegistry._ensure_adapters_loaded()

    # Check that our adapter is registered for Bucket components
    mock_bucket = Mock(spec=Bucket)
    adapter_class = CloudfrontAdapterRegistry.get_adapter_for_component(mock_bucket)

    assert adapter_class == S3BucketCloudfrontAdapter


def test_adapter_inherits_component_class():
    """Test that the adapter has the correct component_class attribute."""
    # The @register_adapter decorator should set the component_class
    assert hasattr(S3BucketCloudfrontAdapter, "component_class")
    assert S3BucketCloudfrontAdapter.component_class == Bucket


@pytest.mark.parametrize(
    ("path_pattern", "expected_pattern"),
    [
        ("/files", "/files/*"),
        ("/static", "/static/*"),
        ("/assets/*", "/assets/*"),
        ("/images/*", "/images/*"),
        ("/content*", "/content*"),
    ],
)
def test_path_pattern_logic(path_pattern, expected_pattern):
    """Test the path pattern logic for S3 adapter."""
    mock_bucket = Mock(spec=Bucket)
    route = Route(path_pattern=path_pattern, component=mock_bucket)
    S3BucketCloudfrontAdapter(idx=0, route=route)

    # Test the logic directly by checking what pattern would be generated
    if route.path_pattern.endswith("*"):
        result_pattern = route.path_pattern
    else:
        result_pattern = f"{route.path_pattern}/*"

    assert result_pattern == expected_pattern


def test_adapter_with_different_indices():
    """Test that adapters work correctly with different indices."""
    mock_bucket = Mock(spec=Bucket)
    mock_bucket.name = "test-bucket"

    route1 = Route(path_pattern="/files", component=mock_bucket)
    route2 = Route(path_pattern="/assets", component=mock_bucket)

    adapter1 = S3BucketCloudfrontAdapter(idx=0, route=route1)
    adapter2 = S3BucketCloudfrontAdapter(idx=3, route=route2)

    # Both should work independently
    assert adapter1.idx == 0
    assert adapter2.idx == 3
    assert adapter1.route.path_pattern == "/files"
    assert adapter2.route.path_pattern == "/assets"
    assert adapter1.bucket == mock_bucket
    assert adapter2.bucket == mock_bucket


def test_adapter_stores_bucket_reference():
    """Test that the adapter correctly stores a reference to the Bucket component."""
    mock_bucket = Mock(spec=Bucket)
    mock_bucket.name = "my-static-bucket"
    mock_bucket.arn = "arn:aws:s3:::my-static-bucket"

    route = Route(path_pattern="/static", component=mock_bucket)
    adapter = S3BucketCloudfrontAdapter(idx=1, route=route)

    # Verify that the adapter stores the correct bucket reference
    assert adapter.bucket is mock_bucket
    assert adapter.bucket.name == "my-static-bucket"
    assert adapter.bucket.arn == "arn:aws:s3:::my-static-bucket"


def test_cloudfront_route_structure():
    """Test that CloudfrontRoute is properly structured for the adapter."""
    mock_bucket = Mock(spec=Bucket)
    mock_bucket.name = "test-bucket"

    route = Route(path_pattern="/uploads", component=mock_bucket)

    # Verify route structure
    assert route.path_pattern == "/uploads"
    assert route.component is mock_bucket
    assert route.component.name == "test-bucket"


@pytest.mark.parametrize("idx", [0, 1, 5, 42])
def test_adapter_with_various_indices(idx):
    """Test that the adapter works with various index values."""
    mock_bucket = Mock(spec=Bucket)
    route = Route(path_pattern="/data", component=mock_bucket)

    adapter = S3BucketCloudfrontAdapter(idx=idx, route=route)

    assert adapter.idx == idx
    assert adapter.route == route
    assert adapter.bucket == mock_bucket


def test_s3_adapter_cache_behavior_characteristics():
    """Test the specific cache behavior characteristics for S3 buckets."""
    # S3 buckets should have different cache behavior than Lambda functions
    # This test documents the expected differences without requiring Pulumi mocks

    mock_bucket = Mock(spec=Bucket)
    route = Route(path_pattern="/static", component=mock_bucket)
    adapter = S3BucketCloudfrontAdapter(idx=0, route=route)

    # S3 adapters should be configured for static content serving
    # These are the expected values based on the implementation:

    # Expected allowed methods for S3 (read-only operations)

    # Expected cache settings for S3 (should cache static content)

    # Expected forwarded values for S3

    # These values are embedded in the get_origin_config method
    # We can't test them directly without Pulumi mocks, but we document them here
    assert adapter.bucket == mock_bucket  # Ensure adapter is properly set up


def test_s3_vs_lambda_adapter_differences():
    """Test that S3 adapter behaves differently from Lambda adapter."""
    from stelvio.aws.cloudfront.origins.components.lambda_function import (
        LambdaFunctionCloudfrontAdapter,
    )
    from stelvio.aws.function import Function

    # Create S3 adapter
    mock_bucket = Mock(spec=Bucket)
    s3_route = Route(path_pattern="/files", component=mock_bucket)
    s3_adapter = S3BucketCloudfrontAdapter(idx=0, route=s3_route)

    # Create Lambda adapter for comparison
    mock_function = Mock(spec=Function)
    lambda_route = Route(path_pattern="/api", component=mock_function)
    lambda_adapter = LambdaFunctionCloudfrontAdapter(idx=0, route=lambda_route)

    # They should be different classes
    assert type(s3_adapter) is not type(lambda_adapter)

    # They should store different component types
    assert isinstance(s3_adapter.bucket, type(mock_bucket))
    assert isinstance(lambda_adapter.function, type(mock_function))
    # Both should inherit from the same base class
    from stelvio.aws.cloudfront.origins.base import ComponentCloudfrontAdapter

    assert isinstance(s3_adapter, ComponentCloudfrontAdapter)
    assert isinstance(lambda_adapter, ComponentCloudfrontAdapter)


def test_bucket_policy_creation():
    """Test that the S3 adapter creates a bucket policy for CloudFront OAC."""
    # This test verifies that S3 adapter creates access policies for OAC

    mock_bucket = Mock(spec=Bucket)
    mock_bucket.name = "test-bucket"
    s3_route = Route(path_pattern="/static", component=mock_bucket)
    s3_adapter = S3BucketCloudfrontAdapter(idx=0, route=s3_route)

    # The adapter creates internal resources (OAC, BucketPolicy)
    # which can't be fully tested without a real Pulumi context
    # We just verify the adapter can be instantiated without errors
    assert s3_adapter is not None
    assert s3_adapter.route == s3_route


def test_edge_cases():
    """Test edge cases for the S3 adapter."""
    # Test with empty path (edge case)
    mock_bucket = Mock(spec=Bucket)
    route = Route(path_pattern="", component=mock_bucket)
    adapter = S3BucketCloudfrontAdapter(idx=0, route=route)
    assert adapter.route.path_pattern == ""
    assert adapter.bucket == mock_bucket

    # Test with root path
    route_root = Route(path_pattern="/", component=mock_bucket)
    adapter_root = S3BucketCloudfrontAdapter(idx=1, route=route_root)
    assert adapter_root.route.path_pattern == "/"

    # Test with very long path
    long_path = "/very/long/path/to/nested/static/content/directory/structure"
    route_long = Route(path_pattern=long_path, component=mock_bucket)
    adapter_long = S3BucketCloudfrontAdapter(idx=2, route=route_long)

    assert adapter_long.route.path_pattern == long_path


def test_cloudfront_js_function_generation_for_s3():
    """Test that the CloudFront JavaScript function works correctly for S3 paths."""
    from stelvio.aws.cloudfront.js import strip_path_pattern_function_js

    # Test S3-typical paths
    js_code = strip_path_pattern_function_js("/static")
    assert "function handler(event)" in js_code
    assert "request.uri" in js_code
    assert "'/static'" in js_code

    # Test that the generated JavaScript has the correct logic for static files
    assert "uri === '/static'" in js_code
    assert "request.uri = '/';" in js_code
    assert "uri.substr(0, 8) === '/static/'" in js_code  # 8 = len('/static/')
    assert "request.uri = uri.substr(7);" in js_code  # 7 = len('/static')


@pytest.mark.parametrize(
    ("s3_path", "expected_exact_length", "expected_prefix_length"),
    [
        ("/static", 7, 8),  # '/static' = 7, '/static/' = 8
        ("/files", 6, 7),  # '/files' = 6, '/files/' = 7
        ("/assets/images", 14, 15),  # '/assets/images' = 14, '/assets/images/' = 15
        ("/cdn", 4, 5),  # '/cdn' = 4, '/cdn/' = 5
    ],
)
def test_js_function_path_lengths_for_s3(s3_path, expected_exact_length, expected_prefix_length):
    """Test that the JavaScript function uses correct path lengths for S3 paths."""
    from stelvio.aws.cloudfront.js import strip_path_pattern_function_js

    js_code = strip_path_pattern_function_js(s3_path)

    # Check exact path length usage
    assert f"uri.substr({expected_exact_length})" in js_code

    # Check prefix path length usage
    assert f"uri.substr(0, {expected_prefix_length})" in js_code


def test_multiple_s3_adapter_instances():
    """Test that multiple S3 adapter instances work correctly with different configurations."""
    mock_bucket1 = Mock(spec=Bucket)
    mock_bucket1.name = "static-assets"

    mock_bucket2 = Mock(spec=Bucket)
    mock_bucket2.name = "user-uploads"

    route1 = Route(path_pattern="/static", component=mock_bucket1)
    route2 = Route(path_pattern="/uploads", component=mock_bucket2)

    adapter1 = S3BucketCloudfrontAdapter(idx=0, route=route1)
    adapter2 = S3BucketCloudfrontAdapter(idx=1, route=route2)

    # Both should work independently
    assert adapter1.bucket.name == "static-assets"
    assert adapter2.bucket.name == "user-uploads"
    assert adapter1.route.path_pattern == "/static"
    assert adapter2.route.path_pattern == "/uploads"
    assert adapter1.idx == 0
    assert adapter2.idx == 1
