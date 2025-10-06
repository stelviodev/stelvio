import pytest

from stelvio.aws.api_gateway import Api
from stelvio.aws.function import Function, FunctionConfig


def test_api_route_basic():
    """Test that a basic route can be added."""
    api = Api("test-api")
    api.route("GET", "/users", "users.handler")
    assert len(api._routes) == 1
    route = api._routes[0]
    assert route.methods == ["GET"]
    assert route.path == "/users"
    assert isinstance(route.handler, FunctionConfig)
    assert route.handler.handler == "users.handler"


def test_api_route_with_function():
    """Test that a route can be added with a Function instance."""
    api = Api("test-api")
    fn = Function("users-function", handler="users.handler")
    api.route("GET", "/users", fn)
    assert len(api._routes) == 1
    route = api._routes[0]
    assert route.handler == fn


def test_api_route_with_function_config():
    """Test that a route can be added with a FunctionConfig."""
    api = Api("test-api")
    config = FunctionConfig(handler="users.handler", memory=256)
    api.route("GET", "/users", config)
    assert len(api._routes) == 1
    route = api._routes[0]
    assert isinstance(route.handler, FunctionConfig)
    assert route.handler.memory == 256


def test_api_route_with_config_dict():
    """Test that a route can be added with a config dictionary."""
    api = Api("test-api")
    config = {"handler": "users.handler", "memory": 256}
    api.route("GET", "/users", config)
    assert len(api._routes) == 1
    route = api._routes[0]
    assert isinstance(route.handler, FunctionConfig)
    assert route.handler.handler == "users.handler"
    assert route.handler.memory == 256


@pytest.mark.parametrize(
    ("handler", "opts", "expected_error"),
    [
        # Missing handler in both places
        (
            None,
            {},
            "Missing handler configuration: when handler argument is None, 'handler' option must "
            "be provided",
        ),
        # Handler in both places
        (
            "users.index",
            {"handler": "users.other"},
            "Ambiguous handler configuration: handler is specified both as positional argument "
            "and in options",
        ),
        # Complete config with additional options
        (
            {"handler": "users.index"},
            {"memory": 256},
            "Invalid configuration: cannot combine complete handler configuration with additional "
            "options",
        ),
        (
            FunctionConfig(handler="users.index"),
            {"memory": 256},
            "Invalid configuration: cannot combine complete handler configuration with additional "
            "options",
        ),
        (
            Function("test-1", handler="users.index"),
            {"memory": 256},
            "Invalid configuration: cannot combine complete handler configuration with additional "
            "options",
        ),
    ],
)
def test_api_create_route_validation(handler, opts, expected_error):
    """Test validation in _create_route static method."""
    api = Api("test-api")
    with pytest.raises(ValueError, match=expected_error):
        api._create_route("GET", "/users", handler, opts)


@pytest.mark.parametrize(
    ("handler", "expected_type", "expected_handler"),
    [
        # String handler converted to FunctionConfig
        ("users.index", FunctionConfig, "users.index"),
        # Dict converted to FunctionConfig
        ({"handler": "users.index"}, FunctionConfig, "users.index"),
        # FunctionConfig stays FunctionConfig
        (FunctionConfig(handler="users.index"), FunctionConfig, "users.index"),
        # Function instance stays Function
        (Function("test", handler="users.index"), Function, "users.index"),
    ],
)
def test_api_create_route_handler_types(handler, expected_type, expected_handler):
    """Test that _create_route handles different handler types correctly."""
    api = Api("test-api")
    route = api._create_route("GET", "/users", handler, {})
    assert isinstance(route.handler, expected_type)
    if isinstance(route.handler, Function):
        assert route.handler.config.handler == expected_handler
    else:  # Must be FunctionConfig
        assert route.handler.handler == expected_handler


def test_api_create_route_with_opts():
    """Test that _create_route correctly combines handler with options."""
    api = Api("test-api")
    route = api._create_route("GET", "/users", "users.index", {"memory": 256})
    assert isinstance(route.handler, FunctionConfig)
    assert route.handler.handler == "users.index"
    assert route.handler.memory == 256


@pytest.mark.parametrize(
    ("first_route", "second_route"),
    [
        # Same file, both trying to configure
        (
            ("GET", "/users", {"handler": "users.index", "memory": 256}),
            ("POST", "/users", {"handler": "users.index", "timeout": 30}),
        ),
        # Using FunctionConfig instead of dict
        (
            ("GET", "/users", FunctionConfig(handler="users.index", memory=256)),
            ("POST", "/users", FunctionConfig(handler="users.index", timeout=30)),
        ),
        # Same folder, both trying to configure
        (
            ("GET", "/users", {"handler": "users::handler.index", "memory": 256}),
            ("POST", "/users", {"handler": "users::handler.create", "timeout": 30}),
        ),
        # Using FunctionConfig instead of dict
        (
            ("GET", "/users", FunctionConfig(handler="users::handler.index", memory=256)),
            ("POST", "/users", FunctionConfig(handler="users::handler.create", timeout=30)),
        ),
    ],
)
def test_api_route_conflicts(first_route, second_route):
    """Test that only one route can configure a shared function."""
    api = Api("test-api")
    api.route(first_route[0], first_route[1], first_route[2])
    api.route(second_route[0], second_route[1], second_route[2])

    # The actual check is in _get_group_config_map during _create_resource.
    # Let's simulate that here.
    # Maybe we should check during route()?
    from stelvio.aws.api_gateway.routing import _get_group_config_map, _group_routes_by_lambda

    grouped_routes = _group_routes_by_lambda(api._routes)
    # This should raise when we try to process the API routes
    with pytest.raises(
        ValueError, match="Multiple routes trying to configure the same lambda function"
    ):
        _get_group_config_map(grouped_routes)


@pytest.mark.parametrize(
    ("first_method", "second_method", "should_conflict"),
    [
        # Exact same method - should conflict
        ("GET", "GET", True),
        # Different methods - should not conflict
        ("GET", "POST", False),
        # List of methods with overlap - should conflict
        (["GET", "POST"], "GET", True),
        # List of methods without overlap - should not conflict
        (["GET", "POST"], "PUT", False),
        # ANY method should conflict with everything
        ("ANY", "GET", True),
        ("GET", "ANY", True),
        # Wildcard (*) method should conflict with everything
        ("*", "POST", True),
        ("DELETE", "*", True),
    ],
)
def test_route_method_path_conflicts(first_method, second_method, should_conflict):
    """Test that routes with the same path and overlapping methods conflict."""
    api = Api("test-api")

    # Add the first route
    api.route(first_method, "/users", "users.handler")

    if should_conflict:
        # If methods overlap, adding the second route should raise a conflict error
        with pytest.raises(ValueError, match="Route conflict"):
            api.route(second_method, "/users", "users.handler2")
    else:
        # If methods don't overlap, adding the second route should succeed
        api.route(second_method, "/users", "users.handler2")

        # Verify both routes were added
        assert len(api._routes) == 2
