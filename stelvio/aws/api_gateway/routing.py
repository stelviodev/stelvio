from collections import defaultdict

from stelvio.aws.api_gateway.config import _ApiRoute
from stelvio.aws.function import Function, FunctionConfig


def _get_handler_key_for_trigger(handler: Function | FunctionConfig) -> str:
    """Gets a consistent string key representing the handler for trigger calculation."""
    if isinstance(handler, Function):
        # Use the logical name of the Function component
        return f"Function:{handler.name}"
    # Must be FunctionConfig
    if handler.folder:
        return f"Config:folder:{handler.folder}"
    # Use the handler string itself (e.g., "path.to.module.func")
    return f"Config:handler:{handler.handler}"


def _group_routes_by_lambda(routes: list[_ApiRoute]) -> dict[str, list[_ApiRoute]]:
    def extract_key(handler_str: str) -> str:
        parts = handler_str.split("::")
        return parts[0] if len(parts) > 1 else handler_str.split(".")[0]

    grouped_routes = {}
    # Having both a folder-based lambda and single-file lambda with the same base name
    # (e.g., functions/user/ and functions/user.py) would cause conflicts.
    # This isn't possible anyway since dots aren't allowed in handler names.
    for route in routes:
        if isinstance(route.handler, Function):
            key = route.handler.name
        else:  # Must be FunctionConfig due to _validate_handler
            # key = (
            #     route.handler.folder
            #     if route.handler.folder
            #     else extract_key(route.handler.handler)
            # )
            key = route.handler.handler_full_qualifier

        grouped_routes.setdefault(key, []).append(route)

    return grouped_routes


def _get_group_config_map(grouped_routes: dict[str, list[_ApiRoute]]) -> dict[str, _ApiRoute]:
    def get_handler_config(routes: list[_ApiRoute]) -> _ApiRoute:
        config_routes = [
            route
            for route in routes
            if isinstance(route.handler, FunctionConfig) and not route.handler.has_only_defaults
        ]
        if len(config_routes) > 1:
            paths = [r.path for r in config_routes]
            raise ValueError(
                f"Multiple routes trying to configure the same lambda function: {', '.join(paths)}"
            )
        return config_routes[0] if config_routes else routes[0]

    return {key: get_handler_config(routes) for key, routes in grouped_routes.items()}


def _create_route_map(routes: list[_ApiRoute]) -> dict[str, tuple[str, str]]:
    return {
        f"{method} {r.path}": (r.handler.local_handler_file_path, r.handler.handler_function_name)
        for r in routes
        for method in r.methods
    }

