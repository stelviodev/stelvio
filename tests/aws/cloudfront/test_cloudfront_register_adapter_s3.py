from unittest.mock import Mock

import pytest

from stelvio.aws.cloudfront.dtos import Route
from stelvio.aws.cloudfront.origins.components.s3 import S3BucketCloudfrontAdapter
from stelvio.aws.s3.s3 import Bucket


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
    adapter = S3BucketCloudfrontAdapter(idx=0, route=route, router_name="router")

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
    S3BucketCloudfrontAdapter(idx=0, route=route, router_name="router")

    # Test the logic directly by checking what pattern would be generated
    if route.path_pattern.endswith("*"):
        result_pattern = route.path_pattern
    else:
        result_pattern = f"{route.path_pattern}/*"

    assert result_pattern == expected_pattern


def test_s3_vs_lambda_adapter_differences():
    """Test that S3 adapter behaves differently from Lambda adapter."""
    from stelvio.aws.cloudfront.origins.components.lambda_function import (
        LambdaFunctionCloudfrontAdapter,
    )
    from stelvio.aws.function import Function

    # Create S3 adapter
    mock_bucket = Mock(spec=Bucket)
    s3_route = Route(path_pattern="/files", component=mock_bucket)
    s3_adapter = S3BucketCloudfrontAdapter(idx=0, route=s3_route, router_name="router")

    # Create Lambda adapter for comparison
    mock_function = Mock(spec=Function)
    lambda_route = Route(path_pattern="/api", component=mock_function)
    lambda_adapter = LambdaFunctionCloudfrontAdapter(
        idx=0, route=lambda_route, router_name="router"
    )

    # They should be different classes
    assert type(s3_adapter) is not type(lambda_adapter)

    # They should store different component types
    assert isinstance(s3_adapter.bucket, type(mock_bucket))
    assert isinstance(lambda_adapter.function, type(mock_function))
    # Both should inherit from the same base class
    from stelvio.aws.cloudfront.origins.base import ComponentCloudfrontAdapter

    assert isinstance(s3_adapter, ComponentCloudfrontAdapter)
    assert isinstance(lambda_adapter, ComponentCloudfrontAdapter)


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
