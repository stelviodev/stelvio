from stelvio.aws.api_gateway.config import _ApiRoute
from stelvio.aws.function import Function, FunctionConfig


def _group_routes_by_lambda(routes: list[_ApiRoute]) -> dict[str, list[_ApiRoute]]:
    grouped_routes = {}
    # Having both a folder-based lambda and single-file lambda with the same base name
    # (e.g., functions/user/ and functions/user.py) would cause conflicts.
    # This isn't possible anyway since dots aren't allowed in handler names.
    for route in routes:
        if isinstance(route.handler, Function):
            key = route.handler.name
        else:  # Must be FunctionConfig due to _validate_handler
            key = route.handler.full_handler_path

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
