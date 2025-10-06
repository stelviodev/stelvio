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
            key = (
                route.handler.folder
                if route.handler.folder
                else extract_key(route.handler.handler)
            )

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


def _create_routing_file(routes: list[_ApiRoute], config_route: _ApiRoute) -> str | None:
    if isinstance(config_route.handler, Function) or len(routes) == 1:
        return None
    route_map = _create_route_map(routes)
    # If all routes points to same handler that means user is handling routing
    # so no need to generate the file
    if len(set(route_map.values())) > 1:
        return _generate_handler_file_content(route_map)
    return None


def _generate_handler_file_content(route_map: dict[str, tuple[str, str]]) -> str:
    # Track function names and their sources
    seen_funcs: dict = {}  # func_name -> file
    func_aliases = {}  # (file, func) -> alias to use

    # Group by file for imports and detect duplicates
    file_funcs = defaultdict(list)
    for file, func in route_map.values():
        # Check if this function name is already used by a different file
        if func in seen_funcs and seen_funcs[func] != file:
            # Create alias for this duplicate
            alias = f"{func}_{file.replace('/', '_').replace('.', '_')}"
            func_aliases[(file, func)] = alias
        else:
            seen_funcs[func] = file

        if func not in file_funcs[file]:  # Avoid duplicates in imports
            file_funcs[file].append(func)

    # Generate imports section
    imports = [
        "# stlv_routing_handler.py",
        "# Auto-generated file - do not edit manually",
        "",
        "from typing import Any",
    ]

    # Create import statements
    for file, funcs in file_funcs.items():
        import_parts = []
        for func in funcs:
            if (file, func) in func_aliases:
                import_parts.append(f"{func} as {func_aliases[(file, func)]}")
            else:
                import_parts.append(func)
        imports.append(f"from {file} import {', '.join(import_parts)}")

    imports.extend(["", ""])

    # Generate routes dictionary
    routes_lines = ["ROUTES = {"]
    for route_key, (file, func) in route_map.items():
        # Use alias if one exists, otherwise use the function name
        func_name = func_aliases.get((file, func), func)
        routes_lines.append(f'    "{route_key}": {func_name},')
    routes_lines.append("}")
    routes_lines.append("")
    routes_lines.append("")

    # Add the standard handler function
    handler_func = [
        "import json",
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

    # Combine all sections
    content = imports + routes_lines + handler_func
    return "\n".join(content)
