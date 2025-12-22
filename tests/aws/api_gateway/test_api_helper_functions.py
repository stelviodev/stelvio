import json
from hashlib import sha256

from stelvio.aws.api_gateway.config import _ApiRoute, path_to_resource_name
from stelvio.aws.api_gateway.deployment import (
    _calculate_route_config_hash,
    _get_handler_key_for_trigger,
)
from stelvio.aws.api_gateway.routing import (
    _get_group_config_map,
    _group_routes_by_lambda,
)
from stelvio.aws.function import Function, FunctionConfig


def test_path_to_resource_name():
    """Test that path_to_resource_name converts path parts correctly."""
    test_cases = [
        (["users"], "users"),
        (["users", "{id}"], "users-id"),
        (["users", "{id}", "orders"], "users-id-orders"),
        (["users", "{proxy+}"], "users-proxyplus"),
        (
            ["a", "very", "long", "path", "with", "many", "segments"],
            "a-very-long-path-with-many-segments",
        ),
    ]

    for path_parts, expected_name in test_cases:
        result = path_to_resource_name(path_parts)
        assert result == expected_name


def test_group_routes_by_lambda_single_file():
    """Test grouping routes with single file lambdas."""
    routes = [
        _ApiRoute("GET", "/users", FunctionConfig(handler="users.index")),
        _ApiRoute("POST", "/users", FunctionConfig(handler="users.create")),
        _ApiRoute("GET", "/orders", FunctionConfig(handler="orders.index")),
    ]

    grouped = _group_routes_by_lambda(routes)
    assert len(grouped) == 3
    assert "users.index" in grouped
    assert "users.create" in grouped
    assert "orders.index" in grouped
    assert len(grouped["users.index"]) == 1
    assert len(grouped["users.create"]) == 1
    assert len(grouped["orders.index"]) == 1


def test_group_routes_by_lambda_folder_based():
    """Test grouping routes with folder-based lambdas."""
    routes = [
        _ApiRoute("GET", "/users", FunctionConfig(handler="users::handler.index")),
        _ApiRoute("POST", "/users", FunctionConfig(handler="users::handler.create")),
        _ApiRoute("GET", "/orders", FunctionConfig(handler="orders::handler.index")),
    ]

    grouped = _group_routes_by_lambda(routes)
    assert len(grouped) == 3
    assert "users/handler.index" in grouped
    assert "users/handler.create" in grouped
    assert "orders/handler.index" in grouped
    assert len(grouped["users/handler.index"]) == 1
    assert len(grouped["users/handler.create"]) == 1
    assert len(grouped["orders/handler.index"]) == 1


def test_group_routes_by_lambda_single_file_and_folder_based():
    """Test grouping routes with mixed lambda types."""
    routes = [
        _ApiRoute("GET", "/users", FunctionConfig(handler="users::users.index")),
        _ApiRoute("POST", "/users", FunctionConfig(handler="users::handler.create")),
        _ApiRoute("PUT", "/users", FunctionConfig(handler="users_process::handler.create")),
        _ApiRoute("GET", "/report", Function("report", handler="orders.index")),
        _ApiRoute("GET", "/orders", FunctionConfig(handler="orders.index")),
        _ApiRoute("POST", "/orders", FunctionConfig(handler="orders.create")),
    ]

    grouped = _group_routes_by_lambda(routes)
    assert len(grouped) == 6
    assert set(grouped.keys()) == {
        "users/users.index",
        "users/handler.create",
        "users_process/handler.create",
        "report",
        "orders.index",
        "orders.create",
    }
    assert len(grouped["users/users.index"]) == 1
    assert len(grouped["users/handler.create"]) == 1
    assert len(grouped["users_process/handler.create"]) == 1
    assert len(grouped["report"]) == 1
    assert len(grouped["orders.index"]) == 1
    assert len(grouped["orders.create"]) == 1


def test_group_routes_by_lambda_with_folder():
    """Test grouping routes with explicit folder."""
    routes = [
        _ApiRoute(
            "GET", "/users", FunctionConfig(handler="handlers/users.index", folder="api_handlers")
        ),
        _ApiRoute(
            "POST",
            "/users",
            FunctionConfig(handler="handlers/users.create", folder="api_handlers"),
        ),
    ]

    grouped = _group_routes_by_lambda(routes)
    assert len(grouped) == 2
    assert "api_handlers/handlers/users.index" in grouped
    assert "api_handlers/handlers/users.create" in grouped
    assert len(grouped["api_handlers/handlers/users.index"]) == 1
    assert len(grouped["api_handlers/handlers/users.create"]) == 1


def test_get_group_config_map_no_conflicts():
    """Test that _get_group_config_map works with no configuration conflicts."""
    routes = [
        _ApiRoute("GET", "/users", FunctionConfig(handler="users.index", memory=256)),
        _ApiRoute("POST", "/users", FunctionConfig(handler="users.create")),
    ]

    grouped = _group_routes_by_lambda(routes)
    config_map = _get_group_config_map(grouped)

    assert len(config_map) == 2
    assert "users.index" in config_map
    assert "users.create" in config_map

    # First route used as config since it has non-default values
    assert config_map["users.index"] == routes[0]
    assert config_map["users.create"] == routes[1]


# --- Tests for _calculate_route_config_hash ---


def test_calculate_route_config_hash_empty():
    """Test hash calculation for an empty list of routes."""
    routes = []
    hash_val = _calculate_route_config_hash(routes)
    assert isinstance(hash_val, str)
    assert len(hash_val) == 64
    assert hash_val == "1e5a619cf6aa7d38991315940eb4576f6a4d3fe335c58961b5b116ca4256d2f9"


def test_calculate_route_config_hash_single_route():
    """Test hash calculation for a single route."""
    routes = [_ApiRoute("GET", "/users", FunctionConfig(handler="users.index"))]
    hash_val = _calculate_route_config_hash(routes)
    assert hash_val == "649276ada701613e74bb6ea6670b940b617d6df255646b9212ed653814aa3247"


def test_calculate_route_config_hash_multiple_routes():
    """Test hash calculation for multiple routes."""
    routes = [
        _ApiRoute("GET", "/users", FunctionConfig(handler="users.index")),
        _ApiRoute("POST", "/users", FunctionConfig(handler="users.create")),
        _ApiRoute("GET", "/orders", FunctionConfig(handler="orders.index")),
    ]
    hash_val = _calculate_route_config_hash(routes)
    assert hash_val == "ea84dbbace6b0136b8c599298e379cdbace718b499b10648acbf83d8c5ab61c0"


def test_calculate_route_config_hash_order_independent():
    """Test that the order of routes does not affect the hash."""
    routes1 = [
        _ApiRoute("GET", "/users", FunctionConfig(handler="users.index")),
        _ApiRoute("POST", "/users", FunctionConfig(handler="users.create")),
    ]
    routes2 = [
        _ApiRoute("POST", "/users", FunctionConfig(handler="users.create")),
        _ApiRoute("GET", "/users", FunctionConfig(handler="users.index")),
    ]
    hash1 = _calculate_route_config_hash(routes1)
    hash2 = _calculate_route_config_hash(routes2)
    assert hash1 == hash2
    assert hash1 == "1e33c8cc87f4fdf6a01db2e614cb41df55fade193370d874eb197b1f44fbe20f"


def test_calculate_route_config_hash_method_order_independent():
    """Test that the order of methods within a route does not affect the hash."""
    routes1 = [_ApiRoute(["GET", "POST"], "/users", FunctionConfig(handler="users.handler"))]
    routes2 = [_ApiRoute(["POST", "GET"], "/users", FunctionConfig(handler="users.handler"))]
    hash1 = _calculate_route_config_hash(routes1)
    hash2 = _calculate_route_config_hash(routes2)
    assert hash1 == hash2
    assert hash1 == "83e8ff72ff0d9568f4214fb35c985fb3621f05bc66a6400ebd1d29402d187d31"


def test_calculate_route_config_hash_handler_types():
    """Test that different handler types produce different hashes."""
    route_str = _ApiRoute("GET", "/data", FunctionConfig(handler="data.process"))
    route_folder = _ApiRoute(
        "GET", "/data", FunctionConfig(handler="handler.process", folder="data_folder")
    )
    # Simulate the key generation for a Function instance directly for this unit test,
    # as mocking the full Function object creation is complex here.
    handler_key_instance = "Function:data-function"

    hash_str = _calculate_route_config_hash([route_str])
    hash_folder = _calculate_route_config_hash([route_folder])

    # Simulate hash calculation with the instance key
    routes_instance_simulated = [
        {
            "path": "/data",
            "methods": ["GET"],
            "handler_key": handler_key_instance,
            "auth": None,
            "cognito_scopes": None,
        }
    ]
    config_to_hash_instance = {"routes": routes_instance_simulated, "cors": None}
    api_config_str_instance = json.dumps(config_to_hash_instance, sort_keys=True)
    hash_instance = sha256(api_config_str_instance.encode()).hexdigest()

    assert hash_str != hash_folder
    assert hash_str != hash_instance
    assert hash_folder != hash_instance

    # Check specific hashes for regression
    assert hash_str == "c0ddb43e10c5c3f5b4309542e1fa51608f71c0c5b936f0a09c66bb43aae6c544"
    assert hash_folder == "0a82817236d0f6c63914b656083fbd937974f4f26c2a0d3121cc6b5334cb3646"
    assert hash_instance == "88a43133fb0c6a39651c641dabe3ad9857cef326b7d5365c0f8ad602a429de5e"


def test_calculate_route_config_hash_changes():
    """Test that changing path, method, or handler changes the hash."""
    base_routes = [_ApiRoute("GET", "/users", FunctionConfig(handler="users.index"))]
    base_hash = _calculate_route_config_hash(base_routes)

    # Change path
    routes_path_change = [_ApiRoute("GET", "/customers", FunctionConfig(handler="users.index"))]
    hash_path_change = _calculate_route_config_hash(routes_path_change)
    assert base_hash != hash_path_change

    # Change method
    routes_method_change = [_ApiRoute("POST", "/users", FunctionConfig(handler="users.index"))]
    hash_method_change = _calculate_route_config_hash(routes_method_change)
    assert base_hash != hash_method_change

    # Change handler string
    routes_handler_change = [_ApiRoute("GET", "/users", FunctionConfig(handler="users.list_all"))]
    hash_handler_change = _calculate_route_config_hash(routes_handler_change)
    assert base_hash != hash_handler_change


# --- Tests for _get_handler_key_for_trigger ---


def test_get_handler_key_for_trigger():
    """Test the helper function that generates keys for the hash calculation."""
    handler_str = FunctionConfig(handler="users.index")
    handler_folder = FunctionConfig(handler="handler.index", folder="users_folder")
    handler_instance = Function("users-function", handler="users.index")

    key_str = _get_handler_key_for_trigger(handler_str)
    key_folder = _get_handler_key_for_trigger(handler_folder)
    key_instance = _get_handler_key_for_trigger(handler_instance)

    assert key_str == "Config:users.index"
    assert key_folder == "Config:users_folder/handler.index"
    assert key_instance == "Function:users-function"
