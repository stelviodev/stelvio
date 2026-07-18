"""Direct unit tests for the `_HttpRoute` dataclass.

`_HttpRoute` is the validation/normalization layer between `HttpApi.route()` and
resource creation. Testing it directly keeps these concerns cheap and focused,
independent of the Pulumi mock runtime.

Note: `jwt_scopes` rejection is enforced at resource-creation time in
`_resolve_auth_for_route`, NOT in `_HttpRoute.__post_init__`, so it is covered by
the behavioral tests in `test_http_api_authorizers.py` (`test_jwt_scopes_with_*`).
"""

import pytest

from stelvio.aws.api_gateway.http_api.routes import _HttpRoute
from stelvio.aws.api_gateway.methods import HTTPMethod
from stelvio.aws.api_gateway.rest_api.constants import ROUTE_MAX_LENGTH, ROUTE_MAX_PARAMS
from stelvio.aws.function import Function, FunctionConfig


@pytest.mark.parametrize(
    ("method", "expected_methods"),
    [
        # Single string (case-insensitive)
        ("GET", ["GET"]),
        ("get", ["GET"]),
        ("POST", ["POST"]),
        ("ANY", ["ANY"]),
        ("*", ["ANY"]),  # * normalizes to ANY
        # Single enum
        (HTTPMethod.GET, ["GET"]),
        (HTTPMethod.ANY, ["ANY"]),
        # Lists: strings, enums, mixed, case-insensitive
        (["GET", "POST"], ["GET", "POST"]),
        (["get", "POST", "Put"], ["GET", "POST", "PUT"]),
        ([HTTPMethod.GET, HTTPMethod.POST], ["GET", "POST"]),
        (["GET", HTTPMethod.POST], ["GET", "POST"]),
    ],
)
def test_http_route_methods_normalization(method, expected_methods):
    route = _HttpRoute(method, "/users", FunctionConfig(handler="handler.main"))
    assert route.methods == expected_methods


@pytest.mark.parametrize(
    ("method", "path", "expected_keys"),
    [
        # Standard route key: "{METHOD} {path}"
        ("GET", "/users", ["GET /users"]),
        ("ANY", "/health", ["ANY /health"]),
        ("*", "/health", ["ANY /health"]),  # * → ANY
        # $default collapses to a single key regardless of method
        ("ANY", "$default", ["$default"]),
        ("*", "$default", ["$default"]),
        # Multi-method expands to one route key per method
        (["GET", "DELETE"], "/users/{id}", ["GET /users/{id}", "DELETE /users/{id}"]),
        (["get", "post"], "/users", ["GET /users", "POST /users"]),
    ],
)
def test_http_route_route_keys(method, path, expected_keys):
    route = _HttpRoute(method, path, FunctionConfig(handler="handler.main"))
    assert route.route_keys == expected_keys


@pytest.mark.parametrize(
    ("auth", "scopes"),
    [
        # jwt_scopes is stored as-is at the dataclass level; rejection happens later.
        (None, ["read:users"]),
        ("IAM", ["read:users"]),
        (None, None),
    ],
)
def test_http_route_jwt_scopes_stored_not_validated(auth, scopes):
    """`_HttpRoute` stores jwt_scopes without validating auth compatibility.

    Compatibility is enforced during resource creation (see `_resolve_auth_for_route`);
    the behavioral tests cover those error paths.
    """
    route = _HttpRoute(
        "GET", "/users", FunctionConfig(handler="handler.main"), auth=auth, jwt_scopes=scopes
    )
    assert route.jwt_scopes == scopes
    assert route.auth == auth


@pytest.mark.parametrize(
    ("method", "path", "expected_error"),
    [
        # Path validation (delegated to _validate_path_for_http_api)
        ("GET", "users", "start with '/'"),  # missing leading slash
        ("GET", "/users/{}", "Empty path parameters"),  # empty param
        ("GET", "/users/{id}{name}", "Adjacent"),  # adjacent params
        ("GET", "/users/{id}/orders/{id}", "Duplicate"),  # duplicate params
        ("GET", "/files/{proxy+}/other", "end of the path"),  # greedy not at end
        ("GET", "/files/{other+}", "Only.*proxy"),  # non-proxy greedy
        ("GET", "/" + "x" * ROUTE_MAX_LENGTH, "Path too long"),
        (
            "GET",
            "/" + "/".join(f"{{param{index}}}" for index in range(ROUTE_MAX_PARAMS + 1)),
            "Maximum of 10 path parameters",
        ),
        # Method validation (delegated to _validate_method_for_http_api)
        ("TRACE", "/users", "Invalid HTTP method"),  # invalid method
        (["GET", "ANY"], "/users", "ANY"),  # ANY in list
        (["GET", "*"], "/users", "ANY"),  # * in list
        ([], "/users", "Method list cannot be empty"),  # empty list
        # $default requires ANY/*
        ("GET", "$default", r"\$default"),
        (["GET", "POST"], "$default", r"\$default"),
    ],
)
def test_http_route_validation_errors(method, path, expected_error):
    with pytest.raises(ValueError, match=expected_error):
        _HttpRoute(method, path, FunctionConfig(handler="handler.main"))


@pytest.mark.parametrize(
    ("method", "expected_error"),
    [
        ([123], "Invalid method type in list"),
        ([[str]], "Invalid method type in list"),
    ],
)
def test_http_route_invalid_method_type(method, expected_error):
    with pytest.raises(TypeError, match=expected_error):
        _HttpRoute(method, "/users", FunctionConfig(handler="handler.main"))


def test_http_route_accepts_maximum_path_length_and_parameters():
    path = "/" + "/".join(f"{{param{index}}}" for index in range(ROUTE_MAX_PARAMS))
    path += "x" * (ROUTE_MAX_LENGTH - len(path))

    route = _HttpRoute("GET", path, FunctionConfig(handler="handler.main"))

    assert route.route_keys == [f"GET {path}"]


@pytest.mark.parametrize(
    "handler",
    [
        "string_handler",  # strings are for route(); _HttpRoute wants FunctionConfig/Function
        {"handler": "dict_handler"},  # dict
        123,  # int
        None,  # None
        [],  # list
    ],
)
def test_http_route_invalid_handler_type(handler):
    with pytest.raises(TypeError, match="Handler must be FunctionConfig or Function"):
        _HttpRoute("GET", "/users", handler)


@pytest.mark.parametrize(
    ("handler", "expected_type"),
    [
        (lambda: Function("test-fn", handler="users.handler"), Function),
    ],
)
def test_http_route_accepts_function_instance(handler, expected_type):
    if callable(handler):
        handler = handler()
    route = _HttpRoute("GET", "/users", handler)
    assert isinstance(route.handler, expected_type)
    assert route.handler is handler


def test_http_route_function_config_handler_preserved():
    config = FunctionConfig(handler="users.handler", memory=256)
    route = _HttpRoute("GET", "/users", config)
    assert route.handler is config
    assert route.handler.memory == 256
