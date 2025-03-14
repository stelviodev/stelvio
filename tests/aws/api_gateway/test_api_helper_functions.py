import pytest

from stelvio.aws.api_gateway import (
    Api,
    _ApiRoute,
    _create_route_map,
    _create_routing_file,
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
        result = Api.path_to_resource_name(path_parts)
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
    """Test that _create_routing_file generates correct content for different route configurations."""
    routing_file = _create_routing_file(routes, routes[0])
    assert routing_file is not None

    expected = "\n".join(HANDLER_START + expected_imports_and_routes + HANDLER_END)
    assert routing_file == expected
