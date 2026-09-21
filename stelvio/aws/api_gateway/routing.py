import re
from typing import Protocol

from stelvio.aws.function import Function, FunctionConfig
from stelvio.component import child_label


class RouteWithHandler(Protocol):
    path: str
    handler: FunctionConfig | Function


def fn_name_from_key(api_name: str, key: str) -> str:
    safe = key.replace("/", "-").replace(".", "_").replace("::", "-")
    return f"{api_name}-{safe}"


def group_routes_by_handler[RouteT: RouteWithHandler](
    routes: list[RouteT],
) -> dict[str, list[RouteT]]:
    grouped_routes = {}
    # Having both a folder-based lambda and single-file lambda with the same base name
    # (e.g., functions/user/ and functions/user.py) would cause conflicts.
    # This isn't possible anyway since dots aren't allowed in handler names.
    for route in routes:
        if isinstance(route.handler, Function):
            key = route.handler.name
        else:
            key = route.handler.full_handler_path

        grouped_routes.setdefault(key, []).append(route)

    return grouped_routes


def get_group_config_map[RouteT: RouteWithHandler](
    grouped_routes: dict[str, list[RouteT]],
) -> dict[str, RouteT]:
    def get_handler_config(routes: list[RouteT]) -> RouteT:
        config_routes = [
            route
            for route in routes
            if isinstance(route.handler, FunctionConfig) and not route.handler.has_only_defaults
        ]
        if len(config_routes) > 1:
            paths = [route.path for route in config_routes]
            raise ValueError(
                f"Multiple routes try to configure the same Lambda function: {', '.join(paths)}"
            )
        return config_routes[0] if config_routes else routes[0]

    return {key: get_handler_config(routes) for key, routes in grouped_routes.items()}


_KIND_PREFIX = re.compile(r"^(?:route|integration|authorizer|auth-permission|permission)-")


@child_label("HttpApi")
@child_label("WebsocketApi")
def _v2_api_child_label(name: str) -> str:
    """`route-GET /users/{id}` -> `GET /users/{id}`, `auth-permission-jwt` -> `jwt`.

    The type label already says route, integration or permission.
    """
    return _KIND_PREFIX.sub("", name)
