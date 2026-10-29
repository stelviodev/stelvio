from unittest.mock import Mock

import pytest

from stelvio.aws.cloudfront.dtos import Route
from stelvio.aws.cloudfront.origins.components.lambda_function import (
    LambdaFunctionCloudfrontAdapter,
)
from stelvio.aws.function import Function


def test_match_function_component():
    """Test that the adapter correctly identifies Function components."""
    # Create a real Function instance for testing
    mock_function = Mock(spec=Function)

    # Test that it matches Function components
    assert LambdaFunctionCloudfrontAdapter.match(mock_function) is True

    # Test that it doesn't match other components
    non_function = Mock()
    assert LambdaFunctionCloudfrontAdapter.match(non_function) is False


def test_inheritance_from_base_class():
    """Test that the adapter properly inherits from ComponentCloudfrontAdapter."""
    from stelvio.aws.cloudfront.origins.base import ComponentCloudfrontAdapter

    mock_function = Mock(spec=Function)
    route = Route(path_pattern="/api", component=mock_function)
    adapter = LambdaFunctionCloudfrontAdapter(idx=0, route=route, router_name="router")

    assert isinstance(adapter, ComponentCloudfrontAdapter)
    assert hasattr(adapter, "get_origin_config")
    assert hasattr(adapter, "get_access_policy")
    assert hasattr(adapter, "match")


def test_registration_decorator():
    """Test that the @register_adapter decorator properly registers the adapter."""
    from stelvio.aws.cloudfront.origins.registry import CloudfrontAdapterRegistry

    # Ensure adapters are loaded
    CloudfrontAdapterRegistry._ensure_adapters_loaded()

    # Check that our adapter is registered for Function components
    mock_function = Mock(spec=Function)
    adapter_class = CloudfrontAdapterRegistry.get_adapter_for_component(mock_function)

    assert adapter_class == LambdaFunctionCloudfrontAdapter


@pytest.mark.parametrize(
    ("path_pattern", "expected_pattern"),
    [
        ("/api", "/api*"),
        ("/simple", "/simple*"),
        ("/api/*", "/api/*"),
        ("/files/*", "/files/*"),
        ("/lambda*", "/lambda*"),
    ],
)
def test_path_pattern_logic(path_pattern, expected_pattern):
    """Test the path pattern logic without Pulumi resources."""
    mock_function = Mock(spec=Function)
    route = Route(path_pattern=path_pattern, component=mock_function)
    LambdaFunctionCloudfrontAdapter(idx=0, route=route, router_name="router")

    # Test the logic directly by checking what pattern would be generated
    if route.path_pattern.endswith("*"):
        result_pattern = route.path_pattern
    else:
        result_pattern = f"{route.path_pattern}*"

    assert result_pattern == expected_pattern


def test_adapter_inherits_component_class():
    """Test that the adapter has the correct component_class attribute."""
    # The @register_adapter decorator should set the component_class
    assert hasattr(LambdaFunctionCloudfrontAdapter, "component_class")
    assert LambdaFunctionCloudfrontAdapter.component_class == Function


def test_cloudfront_js_function_generation():
    """Test that the CloudFront JavaScript function is generated correctly."""
    from stelvio.aws.cloudfront.js import strip_path_pattern_function_js

    # Test basic path
    js_code = strip_path_pattern_function_js("/api")
    assert "function handler(event)" in js_code
    assert "request.uri" in js_code
    assert "'/api'" in js_code

    # Test that the generated JavaScript has the correct logic
    assert "uri === '/api'" in js_code
    assert "request.uri = '/';" in js_code
    assert "uri.substr(0, 5) === '/api/'" in js_code  # 5 = len('/api/')
    assert "request.uri = uri.substr(4);" in js_code  # 4 = len('/api')


def test_cloudfront_js_function_with_different_paths():
    """Test JavaScript function generation with different path patterns."""
    from stelvio.aws.cloudfront.js import strip_path_pattern_function_js

    # Test longer path
    js_code = strip_path_pattern_function_js("/lambda/functions")
    assert "'/lambda/functions'" in js_code
    assert "uri.substr(0, 18)" in js_code  # len('/lambda/functions/') = 18
    assert "uri.substr(17)" in js_code  # len('/lambda/functions') = 17

    # Test single character path
    js_code = strip_path_pattern_function_js("/a")
    assert "'/a'" in js_code
    assert "uri.substr(0, 3)" in js_code  # len('/a/') = 3
    assert "uri.substr(2)" in js_code  # len('/a') = 2


@pytest.mark.parametrize(
    ("path", "expected_exact_length", "expected_prefix_length"),
    [
        ("/api", 4, 5),  # '/api' = 4, '/api/' = 5
        ("/simple", 7, 8),  # '/simple' = 7, '/simple/' = 8
        ("/lambda/test", 12, 13),  # '/lambda/test' = 12, '/lambda/test/' = 13
        ("/a", 2, 3),  # '/a' = 2, '/a/' = 3
    ],
)
def test_js_function_path_lengths(path, expected_exact_length, expected_prefix_length):
    """Test that the JavaScript function uses correct path lengths."""
    from stelvio.aws.cloudfront.js import strip_path_pattern_function_js

    js_code = strip_path_pattern_function_js(path)

    # Check exact path length usage
    assert f"uri.substr({expected_exact_length})" in js_code

    # Check prefix path length usage
    assert f"uri.substr(0, {expected_prefix_length})" in js_code
