from typing import Protocol

from stelvio.aws.function import Function, FunctionConfig


class RouteWithHandler(Protocol):
    path: str
    handler: FunctionConfig | Function


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
    *,
    multiple_configs_message: str = "Multiple routes try to configure the same Lambda function",
) -> dict[str, RouteT]:
    def get_handler_config(routes: list[RouteT]) -> RouteT:
        config_routes = [
            route
            for route in routes
            if isinstance(route.handler, FunctionConfig) and not route.handler.has_only_defaults
        ]
        if len(config_routes) > 1:
            paths = [route.path for route in config_routes]
            raise ValueError(f"{multiple_configs_message}: {', '.join(paths)}")
        return config_routes[0] if config_routes else routes[0]

    return {key: get_handler_config(routes) for key, routes in grouped_routes.items()}
