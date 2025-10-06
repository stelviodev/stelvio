import re

import pytest

from stelvio.aws.api_gateway import HTTPMethod
from stelvio.aws.api_gateway.config import _ApiRoute
from stelvio.aws.function import Function, FunctionConfig


@pytest.mark.parametrize(
    ("path", "expected_error"),
    [
        ("", "Path must start with '/'"),
        ("/" + "x" * 8192, "Path too long"),
        ("/users/{}/orders", "Empty path parameters not allowed"),
        ("/".join(f"/{{{i}}}" for i in range(11)), "Maximum of 10 path parameters allowed"),
        ("/users/{id}{name}", "Adjacent path parameters not allowed"),
        ("/users/{id}/orders/{id}", "Duplicate path parameters not allowed"),
        ("/users/{123-id}", "Invalid parameter name: 123-id"),
        ("/users/{proxy+}/orders", "Greedy parameter must be at the end of the path"),
        ("/users/{path+}", re.escape("Only {proxy+} is supported for greedy paths")),
    ],
)
def test_api_route_path_validation(path, expected_error):
    """Test various path validation scenarios."""
    with pytest.raises(ValueError, match=expected_error):
        _ApiRoute("GET", path, FunctionConfig(handler="handler.main"))


@pytest.mark.parametrize(
    ("method", "expected_error"),
    [
        ("INVALID", "Invalid HTTP method: INVALID"),
        (["GET", "INVALID"], "Invalid HTTP method: INVALID"),
        (["GET", "ANY"], re.escape("ANY and * not allowed in method list")),
        (["GET", "*"], re.escape("ANY and * not allowed in method list")),
        ([], "Method list cannot be empty"),
    ],
)
def test_api_route_invalid_methods(method, expected_error):
    """Test that invalid HTTP methods raise ValueError."""
    with pytest.raises(ValueError, match=expected_error):
        _ApiRoute(method, "/users", FunctionConfig(handler="handler.main"))


@pytest.mark.parametrize(
    ("method", "expected_error"),
    [
        ([123], "Invalid method type in list: <class 'int'>"),
        ([[str]], "Invalid method type in list: <class 'list'>"),
        ([3.14], "Invalid method type in list: <class 'float'>"),
    ],
)
def test_api_route_invalid_method_type(method, expected_error):
    """Test that invalid HTTP methods raise ValueError."""
    with pytest.raises(TypeError, match=expected_error):
        _ApiRoute(method, "/users", FunctionConfig(handler="handler.main"))


@pytest.mark.parametrize(
    ("method", "expected_methods"),
    [
        # Single string methods (case insensitive)
        ("GET", ["GET"]),
        ("get", ["GET"]),
        ("POST", ["POST"]),
        ("post", ["POST"]),
        ("PUT", ["PUT"]),
        ("PATCH", ["PATCH"]),
        ("DELETE", ["DELETE"]),
        ("HEAD", ["HEAD"]),
        ("OPTIONS", ["OPTIONS"]),
        # ANY/*
        ("ANY", ["ANY"]),
        ("*", ["ANY"]),
        # Single HTTPMethod enum
        (HTTPMethod.GET, ["GET"]),
        (HTTPMethod.POST, ["POST"]),
        (HTTPMethod.PUT, ["PUT"]),
        (HTTPMethod.PATCH, ["PATCH"]),
        (HTTPMethod.DELETE, ["DELETE"]),
        (HTTPMethod.HEAD, ["HEAD"]),
        (HTTPMethod.OPTIONS, ["OPTIONS"]),
        (HTTPMethod.ANY, ["ANY"]),
        # Multiple methods - strings
        (["GET", "POST"], ["GET", "POST"]),
        (["get", "post"], ["GET", "POST"]),
        (["GET", "POST", "PUT"], ["GET", "POST", "PUT"]),
        (["get", "POST", "Put"], ["GET", "POST", "PUT"]),
        # Multiple methods - enums
        ([HTTPMethod.GET, HTTPMethod.POST], ["GET", "POST"]),
        ([HTTPMethod.GET, HTTPMethod.POST, HTTPMethod.PUT], ["GET", "POST", "PUT"]),
        # Mixed string and enum
        (["GET", HTTPMethod.POST], ["GET", "POST"]),
        ([HTTPMethod.GET, "post", HTTPMethod.PUT], ["GET", "POST", "PUT"]),
    ],
)
def test_api_route_methods(method, expected_methods):
    """Test that HTTP methods are normalized correctly in all valid combinations."""
    route = _ApiRoute(method, "/users", FunctionConfig(handler="handler.main"))
    assert route.methods == expected_methods


@pytest.mark.parametrize(
    ("handler", "expected_type"),
    [
        # FunctionConfig
        (FunctionConfig(handler="users.handler"), FunctionConfig),
        # Function instance
        (Function("test-2", handler="users.handler"), Function),
    ],
)
def test_api_route_valid_handler_configurations(handler, expected_type):
    """Check we accept only FunctionConfig or Function as handler."""
    route = _ApiRoute("GET", "/users", handler)
    assert isinstance(route.handler, expected_type)
    assert route.handler == handler


def test_api_route_invalid_handler_type():
    """Test that invalid handler types are rejected."""
    invalid_handlers = [
        "string_handler",  # String (should be processed by _create_route, not directly)
        {"handler": "dict_handler"},  # Dict (should be converted to FunctionConfig)
        123,  # Integer
        None,  # None
        [],  # List
    ]

    for handler in invalid_handlers:
        with pytest.raises(TypeError, match="Handler must be FunctionConfig or Function"):
            _ApiRoute("GET", "/users", handler)


@pytest.mark.parametrize(
    ("path", "expected_parts"),
    [
        # Basic paths
        ("/users", ["users"]),
        ("/users/", ["users"]),
        ("/users/orders", ["users", "orders"]),
        # Paths with parameters
        ("/users/{id}", ["users", "{id}"]),
        ("/users/{id}/orders", ["users", "{id}", "orders"]),
        # Path with greedy parameter
        ("/users/{proxy+}", ["users", "{proxy+}"]),
        # Root path
        ("/", []),
    ],
)
def test_api_route_path_parts(path, expected_parts):
    """Test that path_parts property correctly parses and filters path segments."""
    route = _ApiRoute("GET", path, FunctionConfig(handler="handler.main"))
    assert route.path_parts == expected_parts
