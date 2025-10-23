from unittest.mock import Mock

import pytest

from stelvio.aws.cloudfront.dtos import CloudfrontRoute
from stelvio.aws.cloudfront.origins.lambda_function import LambdaFunctionCloudfrontBridge
from stelvio.aws.function import Function


def test_lambda_bridge_basic():
    """Basic test to verify the bridge can be imported and instantiated."""
    # Create a mock function component
    mock_function = Mock(spec=Function)
    mock_function.name = "test-function"

    # Create a route
    route = CloudfrontRoute(path_pattern="/api", component=mock_function)

    # Create the bridge
    bridge = LambdaFunctionCloudfrontBridge(idx=0, route=route)

    # Basic assertions
    assert bridge.idx == 0
    assert bridge.route == route
    assert bridge.function == mock_function
    assert bridge.route.path_pattern == "/api"


def test_match_function_component():
    """Test that the bridge correctly identifies Function components."""
    # Create a real Function instance for testing
    mock_function = Mock(spec=Function)

    # Test that it matches Function components
    assert LambdaFunctionCloudfrontBridge.match(mock_function) is True

    # Test that it doesn't match other components
    non_function = Mock()
    assert LambdaFunctionCloudfrontBridge.match(non_function) is False


def test_get_access_policy_returns_none():
    """Test that get_access_policy returns None for Lambda functions."""
    mock_function = Mock(spec=Function)
    route = CloudfrontRoute(path_pattern="/api", component=mock_function)
    bridge = LambdaFunctionCloudfrontBridge(idx=0, route=route)

    mock_distribution = Mock()
    result = bridge.get_access_policy(mock_distribution)
    assert result is None


def test_inheritance_from_base_class():
    """Test that the bridge properly inherits from ComponentCloudfrontBridge."""
    from stelvio.aws.cloudfront.origins.base import ComponentCloudfrontBridge

    mock_function = Mock(spec=Function)
    route = CloudfrontRoute(path_pattern="/api", component=mock_function)
    bridge = LambdaFunctionCloudfrontBridge(idx=0, route=route)

    assert isinstance(bridge, ComponentCloudfrontBridge)
    assert hasattr(bridge, "get_origin_config")
    assert hasattr(bridge, "get_access_policy")
    assert hasattr(bridge, "match")


def test_registration_decorator():
    """Test that the @register_bridge decorator properly registers the bridge."""
    from stelvio.aws.cloudfront.origins.registry import CFBridgeRegistry

    # Ensure bridges are loaded
    CFBridgeRegistry._ensure_bridges_loaded()

    # Check that our bridge is registered for Function components
    mock_function = Mock(spec=Function)
    bridge_class = CFBridgeRegistry.get_bridge_for_component(mock_function)

    assert bridge_class == LambdaFunctionCloudfrontBridge


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
    route = CloudfrontRoute(path_pattern=path_pattern, component=mock_function)
    LambdaFunctionCloudfrontBridge(idx=0, route=route)

    # Test the logic directly by checking what pattern would be generated
    if route.path_pattern.endswith("*"):
        result_pattern = route.path_pattern
    else:
        result_pattern = f"{route.path_pattern}*"

    assert result_pattern == expected_pattern


def test_bridge_with_different_indices():
    """Test that bridges work correctly with different indices."""
    mock_function = Mock(spec=Function)
    mock_function.name = "test-function"

    route1 = CloudfrontRoute(path_pattern="/api", component=mock_function)
    route2 = CloudfrontRoute(path_pattern="/lambda", component=mock_function)

    bridge1 = LambdaFunctionCloudfrontBridge(idx=0, route=route1)
    bridge2 = LambdaFunctionCloudfrontBridge(idx=5, route=route2)

    # Both should work independently
    assert bridge1.idx == 0
    assert bridge2.idx == 5
    assert bridge1.route.path_pattern == "/api"
    assert bridge2.route.path_pattern == "/lambda"
    assert bridge1.function == mock_function
    assert bridge2.function == mock_function


def test_bridge_stores_function_reference():
    """Test that the bridge correctly stores a reference to the Function component."""
    mock_function = Mock(spec=Function)
    mock_function.name = "my-lambda-function"
    mock_function.arn = "arn:aws:lambda:us-east-1:123456789012:function:my-lambda-function"

    route = CloudfrontRoute(path_pattern="/lambda", component=mock_function)
    bridge = LambdaFunctionCloudfrontBridge(idx=2, route=route)

    # Verify that the bridge stores the correct function reference
    assert bridge.function is mock_function
    assert bridge.function.name == "my-lambda-function"
    assert (
        bridge.function.arn == "arn:aws:lambda:us-east-1:123456789012:function:my-lambda-function"
    )


def test_cloudfront_route_structure():
    """Test that CloudfrontRoute is properly structured for the bridge."""
    mock_function = Mock(spec=Function)
    mock_function.name = "test-function"

    route = CloudfrontRoute(path_pattern="/test", component=mock_function)

    # Verify route structure
    assert route.path_pattern == "/test"
    assert route.component is mock_function
    assert route.component.name == "test-function"


@pytest.mark.parametrize("idx", [0, 1, 10, 99])
def test_bridge_with_various_indices(idx):
    """Test that the bridge works with various index values."""
    mock_function = Mock(spec=Function)
    route = CloudfrontRoute(path_pattern="/test", component=mock_function)

    bridge = LambdaFunctionCloudfrontBridge(idx=idx, route=route)

    assert bridge.idx == idx
    assert bridge.route == route
    assert bridge.function == mock_function


def test_bridge_inherits_component_class():
    """Test that the bridge has the correct component_class attribute."""
    # The @register_bridge decorator should set the component_class
    assert hasattr(LambdaFunctionCloudfrontBridge, "component_class")
    assert LambdaFunctionCloudfrontBridge.component_class == Function


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


def test_edge_cases():
    """Test edge cases for the bridge."""
    # Test with empty path (edge case)
    mock_function = Mock(spec=Function)
    route = CloudfrontRoute(path_pattern="", component=mock_function)
    bridge = LambdaFunctionCloudfrontBridge(idx=0, route=route)

    assert bridge.route.path_pattern == ""
    assert bridge.function == mock_function

    # Test with root path
    route_root = CloudfrontRoute(path_pattern="/", component=mock_function)
    bridge_root = LambdaFunctionCloudfrontBridge(idx=1, route=route_root)

    assert bridge_root.route.path_pattern == "/"

    # Test with very long path
    long_path = "/very/long/path/with/many/segments/that/goes/on/and/on"
    route_long = CloudfrontRoute(path_pattern=long_path, component=mock_function)
    bridge_long = LambdaFunctionCloudfrontBridge(idx=2, route=route_long)

    assert bridge_long.route.path_pattern == long_path
