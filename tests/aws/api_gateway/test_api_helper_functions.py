import json
from hashlib import sha256

import pytest

from stelvio.aws.api_gateway.config import _ApiRoute, path_to_resource_name
from stelvio.aws.api_gateway.deployment import _calculate_route_config_hash
from stelvio.aws.api_gateway.routing import (
    _create_route_map,
    _create_routing_file,
    _get_group_config_map,
    _get_handler_key_for_trigger,
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
    assert len(grouped) == 2
    assert "users" in grouped
    assert "orders" in grouped
    assert len(grouped["users"]) == 2
    assert len(grouped["orders"]) == 1


def test_group_routes_by_lambda_folder_based():
    """Test grouping routes with folder-based lambdas."""
    routes = [
        _ApiRoute("GET", "/users", FunctionConfig(handler="users::handler.index")),
        _ApiRoute("POST", "/users", FunctionConfig(handler="users::handler.create")),
        _ApiRoute("GET", "/orders", FunctionConfig(handler="orders::handler.index")),
    ]

    grouped = _group_routes_by_lambda(routes)
    assert len(grouped) == 2
    assert "users" in grouped
    assert "orders" in grouped
    assert len(grouped["users"]) == 2
    assert len(grouped["orders"]) == 1


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
    assert len(grouped) == 4
    assert set(grouped.keys()) == {"users", "users_process", "report", "orders"}
    assert len(grouped["users"]) == 2
    assert len(grouped["users_process"]) == 1
    assert len(grouped["report"]) == 1
    assert len(grouped["orders"]) == 2


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
    assert len(grouped) == 1
    assert "api_handlers" in grouped
    assert len(grouped["api_handlers"]) == 2


def test_get_group_config_map_no_conflicts():
    """Test that _get_group_config_map works with no configuration conflicts."""
    routes = [
        _ApiRoute("GET", "/users", FunctionConfig(handler="users.index", memory=256)),
        _ApiRoute("POST", "/users", FunctionConfig(handler="users.create")),
    ]

    grouped = _group_routes_by_lambda(routes)
    config_map = _get_group_config_map(grouped)

    assert len(config_map) == 1
    assert "users" in config_map
    # First route used as config since it has non-default values
    assert config_map["users"] == routes[0]


def test_get_group_config_map_with_conflicts():
    """Test that _get_group_config_map raises when there are configuration conflicts."""
    # Also tested in test_api_route_conflicts
    routes = [
        _ApiRoute("GET", "/users", FunctionConfig(handler="users.index", memory=256)),
        _ApiRoute("POST", "/users", FunctionConfig(handler="users.create", timeout=30)),
    ]

    grouped = _group_routes_by_lambda(routes)

    with pytest.raises(
        ValueError, match="Multiple routes trying to configure the same lambda function"
    ):
        _get_group_config_map(grouped)


@pytest.mark.parametrize(
    ("routes", "expected_map"),
    [
        # Single file function case
        (
            [
                _ApiRoute("GET", "/users", FunctionConfig(handler="users.index")),
                _ApiRoute("POST", "/users", FunctionConfig(handler="users.create")),
                _ApiRoute(["PUT", "PATCH"], "/users/{id}", FunctionConfig(handler="users.update")),
            ],
            {
                "GET /users": ("users", "index"),
                "POST /users": ("users", "create"),
                "PUT /users/{id}": ("users", "update"),
                "PATCH /users/{id}": ("users", "update"),
            },
        ),
        # Folder based function case
        (
            [
                _ApiRoute(
                    "GET",
                    "/users",
                    FunctionConfig(folder="functions/users", handler="handler.index"),
                ),
                _ApiRoute(
                    "POST",
                    "/users",
                    FunctionConfig(folder="functions/users", handler="handler.create"),
                ),
                _ApiRoute(
                    ["PUT", "PATCH"],
                    "/users/{id}",
                    FunctionConfig(folder="functions/users", handler="handler.update"),
                ),
            ],
            {
                "GET /users": ("handler", "index"),
                "POST /users": ("handler", "create"),
                "PUT /users/{id}": ("handler", "update"),
                "PATCH /users/{id}": ("handler", "update"),
            },
        ),
    ],
)
def test_create_route_map(routes, expected_map):
    route_map = _create_route_map(routes)
    assert route_map == expected_map


@pytest.mark.parametrize(
    "routes",
    [
        [_ApiRoute("GET", "/users", FunctionConfig(handler="users.index"))],
        [
            _ApiRoute("GET", "/users", FunctionConfig(handler="users.handler")),
            _ApiRoute("POST", "/users", FunctionConfig(handler="users.handler")),
        ],
    ],
    ids=["single_route", "same_handler_for_multiple_routes"],
)
def test_create_routing_file_returns_none_(routes):
    routing_file = _create_routing_file(routes, routes[0])
    assert routing_file is None


# Standard parts of the routing handler file
HANDLER_START = [
    "# stlv_routing_handler.py",
    "# Auto-generated file - do not edit manually",
    "",
    "from typing import Any",
]


HANDLER_END = [
    "\n\nimport json",
    "",
    "def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:",
    '    method = event["httpMethod"]',
    '    resource = event["resource"]',
    '    route_key = f"{method} {resource}"',
    "",
    "    func = ROUTES.get(route_key)",
    "    if not func:",
    "        return {",
    '            "statusCode": 500,',
    '            "headers": {"Content-Type": "application/json"},',
    '            "body": json.dumps({',
    '                "error": "Route not found",',
    '                "message": f"No handler for route: {route_key}"',
    "            })",
    "        }",
    "    return func(event, context)",
    "",
]


@pytest.mark.parametrize(
    ("routes", "expected_imports_and_routes"),
    [
        # Test case 1: Multiple handlers from same module
        (
            [
                _ApiRoute("GET", "/users", FunctionConfig(handler="users.index")),
                _ApiRoute("POST", "/users", FunctionConfig(handler="users.create")),
            ],
            [
                "from users import index, create",
                "\n\nROUTES = {",
                '    "GET /users": index,',
                '    "POST /users": create,',
                "}",
            ],
        ),
        # Test case 2: Multiple handlers from different modules
        (
            [
                _ApiRoute("GET", "/users", FunctionConfig(handler="users.index")),
                _ApiRoute("POST", "/users", FunctionConfig(handler="users.create")),
                _ApiRoute("GET", "/orders", FunctionConfig(handler="orders.index")),
            ],
            [
                "from users import index, create",
                "from orders import index as index_orders",
                "\n\nROUTES = {",
                '    "GET /users": index,',
                '    "POST /users": create,',
                '    "GET /orders": index_orders,',
                "}",
            ],
        ),
    ],
)
def test_create_routing_file(routes, expected_imports_and_routes):
    """
    Test that _create_routing_file generates correct content for different route configurations.
    """
    routing_file = _create_routing_file(routes, routes[0])
    assert routing_file is not None

    expected = "\n".join(HANDLER_START + expected_imports_and_routes + HANDLER_END)
    assert routing_file == expected


# --- Tests for _calculate_route_config_hash ---


def test_calculate_route_config_hash_empty():
    """Test hash calculation for an empty list of routes."""
    routes = []
    hash_val = _calculate_route_config_hash(routes)
    assert isinstance(hash_val, str)
    assert len(hash_val) == 64
    assert hash_val == "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"


def test_calculate_route_config_hash_single_route():
    """Test hash calculation for a single route."""
    routes = [_ApiRoute("GET", "/users", FunctionConfig(handler="users.index"))]
    hash_val = _calculate_route_config_hash(routes)
    assert hash_val == "58c2a4149435ef3755376f7c22a27eefc2d20b4e9ad5085008c26abb09334767"


def test_calculate_route_config_hash_multiple_routes():
    """Test hash calculation for multiple routes."""
    routes = [
        _ApiRoute("GET", "/users", FunctionConfig(handler="users.index")),
        _ApiRoute("POST", "/users", FunctionConfig(handler="users.create")),
        _ApiRoute("GET", "/orders", FunctionConfig(handler="orders.index")),
    ]
    hash_val = _calculate_route_config_hash(routes)
    assert hash_val == "36623ac75ce7342c53be294c2b06e77ff89c2429b06257cc0a18b2b7b095ab7c"


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
    assert hash1 == "07a25781ab9229c7891dc8d52a35c628d3a90d9ea6f0748d204ec1e0937169a2"


def test_calculate_route_config_hash_method_order_independent():
    """Test that the order of methods within a route does not affect the hash."""
    routes1 = [_ApiRoute(["GET", "POST"], "/users", FunctionConfig(handler="users.handler"))]
    routes2 = [_ApiRoute(["POST", "GET"], "/users", FunctionConfig(handler="users.handler"))]
    hash1 = _calculate_route_config_hash(routes1)
    hash2 = _calculate_route_config_hash(routes2)
    assert hash1 == hash2
    assert hash1 == "f5c9e8d31ac3092f9d546b887cf93729ab7c3148f24e94af19acf165c0779c86"


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
        }
    ]
    api_config_str_instance = json.dumps(routes_instance_simulated, sort_keys=True)
    hash_instance = sha256(api_config_str_instance.encode()).hexdigest()

    assert hash_str != hash_folder
    assert hash_str != hash_instance
    assert hash_folder != hash_instance

    # Check specific hashes for regression
    assert hash_str == "a01118b98b6a05a85bd873b8d361bc2b7b158395fdd1a856c770a1f445ebb20a"
    assert hash_folder == "e09ab7b83c74337a58a58a0bf216a229a243bb278f8e7224ebcb054ff5f6dbb5"
    assert hash_instance == "0567bf693f5757f49adab7ec34a715749d133f31b9b5a668f2c7317483e2f9d1"


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

    assert key_str == "Config:handler:users.index"
    assert key_folder == "Config:folder:users_folder"
    assert key_instance == "Function:users-function"
