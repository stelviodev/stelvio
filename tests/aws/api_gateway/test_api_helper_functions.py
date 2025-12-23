import pytest

from stelvio.aws.api_gateway.config import _ApiRoute, _Authorizer, path_to_resource_name
from stelvio.aws.api_gateway.deployment import _calculate_deployment_hash
from stelvio.aws.api_gateway.routing import _get_group_config_map, _group_routes_by_lambda
from stelvio.aws.cors import CorsConfig
from stelvio.aws.function import Function, FunctionConfig


def assert_single_route(
    group: list[_ApiRoute], expected_path: str, expected_methods: list[str]
) -> None:
    """Verify a group contains exactly one route with expected path and methods."""
    assert len(group) == 1
    assert group[0].path == expected_path
    assert group[0].methods == expected_methods


# --- Path utilities ---


def test_path_to_resource_name():
    """Path parts are joined with dashes and special chars are normalized."""
    assert path_to_resource_name([]) == "root"
    assert path_to_resource_name(["users"]) == "users"
    assert path_to_resource_name(["users", "{id}"]) == "users-id"
    assert path_to_resource_name(["users", "{id}", "orders"]) == "users-id-orders"
    assert path_to_resource_name(["users", "{proxy+}"]) == "users-proxyplus"
    assert (
        path_to_resource_name(["a", "very", "long", "path", "with", "many", "segments"])
        == "a-very-long-path-with-many-segments"
    )


def test_routes_grouped_by_handler_identifier():
    """Routes are grouped by their handler's full path identifier."""
    routes = [
        # Single file handlers
        _ApiRoute("GET", "/users", FunctionConfig(handler="users.index")),
        _ApiRoute("POST", "/users", FunctionConfig(handler="users.create")),
        # Folder-based handler (:: syntax)
        _ApiRoute("GET", "/orders", FunctionConfig(handler="orders::handler.list")),
        # Explicit folder config
        _ApiRoute("GET", "/reports", FunctionConfig(handler="handler.run", folder="reports")),
        # Function instance uses its name
        _ApiRoute("GET", "/health", Function("health-check", handler="health.check")),
    ]

    grouped = _group_routes_by_lambda(routes)

    assert set(grouped.keys()) == {
        "users.index",
        "users.create",
        "orders/handler.list",
        "reports/handler.run",
        "health-check",
    }

    # Verify each group has exactly 1 route with correct path and method
    assert_single_route(grouped["users.index"], "/users", ["GET"])
    assert_single_route(grouped["users.create"], "/users", ["POST"])
    assert_single_route(grouped["orders/handler.list"], "/orders", ["GET"])
    assert_single_route(grouped["reports/handler.run"], "/reports", ["GET"])
    assert_single_route(grouped["health-check"], "/health", ["GET"])


def test_multiple_routes_same_handler_grouped_together():
    """Routes sharing a handler are collected under one group."""
    routes = [
        _ApiRoute("GET", "/users", FunctionConfig(handler="users.handler")),
        _ApiRoute("POST", "/users", FunctionConfig(handler="users.handler")),
        _ApiRoute("DELETE", "/users/{id}", FunctionConfig(handler="users.handler")),
    ]

    grouped = _group_routes_by_lambda(routes)

    assert set(grouped.keys()) == {"users.handler"}
    assert len(grouped["users.handler"]) == 3

    paths = {r.path for r in grouped["users.handler"]}
    methods = {m for r in grouped["users.handler"] for m in r.methods}
    assert paths == {"/users", "/users/{id}"}
    assert methods == {"GET", "POST", "DELETE"}


def test_config_map_returns_representative_route_per_handler():
    """Each handler group gets one route as its config source - prefers non-default config."""
    routes = [
        _ApiRoute("GET", "/users", FunctionConfig(handler="users.handler", memory=256)),
        _ApiRoute("POST", "/users", FunctionConfig(handler="users.handler")),
        _ApiRoute("GET", "/orders", FunctionConfig(handler="orders.handler")),
    ]

    grouped = _group_routes_by_lambda(routes)
    config_map = _get_group_config_map(grouped)

    assert set(config_map.keys()) == {"users.handler", "orders.handler"}
    # Route with non-default config (memory=256) is selected - verify it's the GET route
    selected = config_map["users.handler"]
    assert selected.path == "/users"
    assert selected.methods == ["GET"]
    assert selected.handler.memory == 256


def test_multi_method_routes_grouped_correctly():
    """Routes with multiple methods are grouped and all methods preserved."""
    routes = [
        _ApiRoute(["GET", "POST"], "/users", FunctionConfig(handler="users.handler")),
        _ApiRoute("DELETE", "/users/{id}", FunctionConfig(handler="users.handler")),
    ]

    grouped = _group_routes_by_lambda(routes)

    assert set(grouped.keys()) == {"users.handler"}
    assert len(grouped["users.handler"]) == 2

    all_methods = {m for r in grouped["users.handler"] for m in r.methods}
    assert all_methods == {"GET", "POST", "DELETE"}


def test_config_map_rejects_conflicting_lambda_configs():
    """Error when multiple routes configure the same lambda differently."""
    routes = [
        _ApiRoute("GET", "/users", FunctionConfig(handler="users.handler", memory=256)),
        _ApiRoute("POST", "/users", FunctionConfig(handler="users.handler", timeout=30)),
    ]

    grouped = _group_routes_by_lambda(routes)

    with pytest.raises(ValueError, match="Multiple routes trying to configure the same lambda"):
        _get_group_config_map(grouped)


# --- Deployment hash properties ---


def test_deployment_hash_is_stable_and_valid():
    """Hash is deterministic and order-independent."""
    routes = [
        _ApiRoute("GET", "/users", FunctionConfig(handler="users.index")),
        _ApiRoute("POST", "/orders", FunctionConfig(handler="orders.create")),
    ]
    routes_reversed = list(reversed(routes))

    expected = "62d682fdbd91152e171d93257d25dc5f75e28c4f94fd6e541f446a2c5db85c11"

    assert _calculate_deployment_hash(routes) == expected
    assert _calculate_deployment_hash(routes) == expected  # Deterministic
    assert _calculate_deployment_hash(routes_reversed) == expected  # Order independent


def test_empty_routes_produces_valid_hash():
    """Empty routes list produces valid hash for edge case handling."""
    expected = "1e5a619cf6aa7d38991315940eb4576f6a4d3fe335c58961b5b116ca4256d2f9"
    assert _calculate_deployment_hash([]) == expected


def test_method_order_does_not_affect_hash():
    """Route with methods [GET, POST] hashes same as [POST, GET]."""
    route1 = _ApiRoute(["GET", "POST"], "/users", FunctionConfig(handler="users.handler"))
    route2 = _ApiRoute(["POST", "GET"], "/users", FunctionConfig(handler="users.handler"))

    assert _calculate_deployment_hash([route1]) == _calculate_deployment_hash([route2])


def test_hash_changes_when_path_method_or_handler_changes():
    """Any route definition change triggers new deployment."""
    base = [_ApiRoute("GET", "/users", FunctionConfig(handler="users.index"))]
    base_hash = _calculate_deployment_hash(base)

    # Different path
    assert base_hash != _calculate_deployment_hash(
        [_ApiRoute("GET", "/customers", FunctionConfig(handler="users.index"))]
    )
    # Different method
    assert base_hash != _calculate_deployment_hash(
        [_ApiRoute("POST", "/users", FunctionConfig(handler="users.index"))]
    )
    # Different handler
    assert base_hash != _calculate_deployment_hash(
        [_ApiRoute("GET", "/users", FunctionConfig(handler="users.list"))]
    )
    # Different handler type (folder vs file)
    assert base_hash != _calculate_deployment_hash(
        [_ApiRoute("GET", "/users", FunctionConfig(handler="handler.index", folder="users"))]
    )


def test_hash_changes_when_auth_config_changes():
    """Auth changes trigger redeployment."""
    authorizer = _Authorizer(name="my-auth")
    route_no_auth = _ApiRoute("GET", "/users", FunctionConfig(handler="users.index"))
    route_with_auth = _ApiRoute(
        "GET", "/users", FunctionConfig(handler="users.index"), auth=authorizer
    )
    route_with_iam = _ApiRoute("GET", "/users", FunctionConfig(handler="users.index"), auth="IAM")

    hashes = {
        _calculate_deployment_hash([route_no_auth]),
        _calculate_deployment_hash([route_with_auth]),
        _calculate_deployment_hash([route_with_iam]),
    }
    assert len(hashes) == 3  # All different


def test_different_authorizer_names_produce_different_hashes():
    """Authorizers are distinguished by name."""
    route1 = _ApiRoute(
        "GET", "/users", FunctionConfig(handler="users.index"), auth=_Authorizer(name="auth-one")
    )
    route2 = _ApiRoute(
        "GET", "/users", FunctionConfig(handler="users.index"), auth=_Authorizer(name="auth-two")
    )

    assert _calculate_deployment_hash([route1]) != _calculate_deployment_hash([route2])


def test_authorizer_internal_config_does_not_affect_hash():
    """Only authorizer name matters - TTL/pools are handled by Pulumi separately."""
    auth1 = _Authorizer(name="my-auth", user_pools=["pool-1"], ttl=300)
    auth2 = _Authorizer(name="my-auth", user_pools=["pool-2"], ttl=600)

    route1 = _ApiRoute("GET", "/users", FunctionConfig(handler="users.index"), auth=auth1)
    route2 = _ApiRoute("GET", "/users", FunctionConfig(handler="users.index"), auth=auth2)

    assert _calculate_deployment_hash([route1]) == _calculate_deployment_hash([route2])


def test_default_auth_affects_routes_that_inherit_it():
    """Routes without explicit auth inherit default_auth in hash calculation."""
    authorizer = _Authorizer(name="default-auth")
    route = _ApiRoute("GET", "/users", FunctionConfig(handler="users.index"))

    hash_no_default = _calculate_deployment_hash([route], default_auth=None)
    hash_with_default = _calculate_deployment_hash([route], default_auth=authorizer)
    hash_with_iam = _calculate_deployment_hash([route], default_auth="IAM")

    assert len({hash_no_default, hash_with_default, hash_with_iam}) == 3


def test_route_auth_overrides_default_auth():
    """Route with explicit auth ignores default_auth changes."""
    route = _ApiRoute(
        "GET", "/users", FunctionConfig(handler="users.index"), auth=_Authorizer(name="route-auth")
    )

    hash_no_default = _calculate_deployment_hash([route], default_auth=None)
    hash_with_default = _calculate_deployment_hash(
        [route], default_auth=_Authorizer(name="default-auth")
    )

    assert hash_no_default == hash_with_default


def test_auth_false_opts_out_of_default():
    """Route with auth=False ignores default_auth."""
    route = _ApiRoute("GET", "/users", FunctionConfig(handler="users.index"), auth=False)

    hash_no_default = _calculate_deployment_hash([route], default_auth=None)
    hash_with_default = _calculate_deployment_hash(
        [route], default_auth=_Authorizer(name="default-auth")
    )

    assert hash_no_default == hash_with_default


def test_cognito_scopes_affect_hash():
    """Different scopes trigger redeployment."""
    authorizer = _Authorizer(name="cognito-auth", user_pools=["pool"])

    route_no_scopes = _ApiRoute(
        "GET", "/users", FunctionConfig(handler="users.index"), auth=authorizer
    )
    route_with_scopes = _ApiRoute(
        "GET",
        "/users",
        FunctionConfig(handler="users.index"),
        auth=authorizer,
        cognito_scopes=["read:users"],
    )
    route_different_scopes = _ApiRoute(
        "GET",
        "/users",
        FunctionConfig(handler="users.index"),
        auth=authorizer,
        cognito_scopes=["write:users"],
    )

    hashes = {
        _calculate_deployment_hash([route_no_scopes]),
        _calculate_deployment_hash([route_with_scopes]),
        _calculate_deployment_hash([route_different_scopes]),
    }
    assert len(hashes) == 3


def test_cognito_scopes_order_independent():
    """Scope order doesn't affect hash."""
    authorizer = _Authorizer(name="auth", user_pools=["pool"])

    route1 = _ApiRoute(
        "GET",
        "/users",
        FunctionConfig(handler="users.index"),
        auth=authorizer,
        cognito_scopes=["read:users", "write:users"],
    )
    route2 = _ApiRoute(
        "GET",
        "/users",
        FunctionConfig(handler="users.index"),
        auth=authorizer,
        cognito_scopes=["write:users", "read:users"],
    )

    assert _calculate_deployment_hash([route1]) == _calculate_deployment_hash([route2])


def test_empty_cognito_scopes_same_as_none():
    """Empty list and None are equivalent for scopes."""
    authorizer = _Authorizer(name="auth", user_pools=["pool"])

    route_none = _ApiRoute(
        "GET",
        "/users",
        FunctionConfig(handler="users.index"),
        auth=authorizer,
        cognito_scopes=None,
    )
    route_empty = _ApiRoute(
        "GET", "/users", FunctionConfig(handler="users.index"), auth=authorizer, cognito_scopes=[]
    )

    assert _calculate_deployment_hash([route_none]) == _calculate_deployment_hash([route_empty])


def test_cognito_scopes_rejected_for_non_cognito_auth():
    """Scopes only valid with Cognito authorizers, even if empty."""
    with pytest.raises(ValueError, match="cognito_scopes only works with Cognito"):
        _ApiRoute(
            "GET",
            "/users",
            FunctionConfig(handler="users.index"),
            auth="IAM",
            cognito_scopes=["read:users"],
        )

    with pytest.raises(ValueError, match="cognito_scopes only works with Cognito"):
        _ApiRoute(
            "GET",
            "/users",
            FunctionConfig(handler="users.index"),
            auth=_Authorizer(name="lambda-auth"),  # No user_pools = not Cognito
            cognito_scopes=["read:users"],
        )


def test_cors_config_affects_hash():
    """CORS changes trigger redeployment."""
    routes = [_ApiRoute("GET", "/users", FunctionConfig(handler="users.index"))]

    hash_no_cors = _calculate_deployment_hash(routes, cors_config=None)
    hash_with_cors = _calculate_deployment_hash(routes, cors_config=CorsConfig(allow_origins="*"))
    hash_different_origin = _calculate_deployment_hash(
        routes, cors_config=CorsConfig(allow_origins="https://example.com")
    )

    assert len({hash_no_cors, hash_with_cors, hash_different_origin}) == 3


def test_cors_field_changes_affect_hash():
    """Each CORS field change triggers redeployment."""
    routes = [_ApiRoute("GET", "/users", FunctionConfig(handler="users.index"))]
    base = CorsConfig(allow_origins="https://example.com")

    base_hash = _calculate_deployment_hash(routes, cors_config=base)

    # credentials
    assert base_hash != _calculate_deployment_hash(
        routes, cors_config=CorsConfig(allow_origins="https://example.com", allow_credentials=True)
    )
    # max_age
    assert base_hash != _calculate_deployment_hash(
        routes, cors_config=CorsConfig(allow_origins="https://example.com", max_age=600)
    )
    # expose_headers
    assert base_hash != _calculate_deployment_hash(
        routes,
        cors_config=CorsConfig(allow_origins="https://example.com", expose_headers=["X-Custom"]),
    )


def test_cors_list_order_independent():
    """Order of items in CORS list fields doesn't affect hash."""
    routes = [_ApiRoute("GET", "/users", FunctionConfig(handler="users.index"))]

    # Test all list fields: origins, methods, headers, expose_headers
    cors1 = CorsConfig(
        allow_origins=["https://a.com", "https://b.com"],
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "Authorization"],
        expose_headers=["X-A", "X-B"],
    )
    cors2 = CorsConfig(
        allow_origins=["https://b.com", "https://a.com"],
        allow_methods=["POST", "GET"],
        allow_headers=["Authorization", "Content-Type"],
        expose_headers=["X-B", "X-A"],
    )

    assert _calculate_deployment_hash(routes, cors_config=cors1) == _calculate_deployment_hash(
        routes, cors_config=cors2
    )
