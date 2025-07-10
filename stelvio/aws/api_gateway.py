import json
import logging
import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from functools import cache
from hashlib import sha256
from typing import Literal, Unpack, final

import pulumi
from pulumi import Input, Output, ResourceOptions, StringAsset
from pulumi_aws import get_caller_identity, get_region
from pulumi_aws.apigateway import (
    Account,
    Deployment,
    Integration,
    Method,
    Resource,
    RestApi,
    Stage,
)
from pulumi_aws.iam import (
    GetPolicyDocumentStatementArgs,
    GetPolicyDocumentStatementPrincipalArgs,
    Role,
    get_policy_document,
)
from pulumi_aws.lambda_ import Permission

from stelvio import context
from stelvio.aws.function import (
    Function,
    FunctionAssetsRegistry,
    FunctionConfig,
    FunctionConfigDict,
)
from stelvio.component import Component

logger = logging.getLogger(__name__)

ROUTE_MAX_PARAMS = 10

ROUTE_MAX_LENGTH = 8192

HTTP_METHODS = Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


@dataclass(frozen=True)
class ApiResources:
    rest_api: RestApi
    deployment: Deployment
    stage: Stage


API_GATEWAY_LOGS_POLICY = (
    "arn:aws:iam::aws:policy/service-role/AmazonAPIGatewayPushToCloudWatchLogs"
)


# These are methods supported by api gateway
class HTTPMethod(Enum):
    GET = "GET"
    POST = "POST"
    PUT = "PUT"
    DELETE = "DELETE"
    PATCH = "PATCH"
    HEAD = "HEAD"
    OPTIONS = "OPTIONS"
    ANY = "ANY"


HTTPMethodLiteral = Literal["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS", "ANY", "*"]

type HTTPMethodInput = (
    str | HTTPMethodLiteral | HTTPMethod | list[str | HTTPMethodLiteral | HTTPMethod]
)


def _validate_single_method(method: str | HTTPMethod) -> None:
    # Convert to string if it's enum
    if isinstance(method, HTTPMethod):
        method = method.value
    method_upper_case = method.upper()
    # Handle ANY and * as synonyms
    if method_upper_case in ("ANY", "*"):
        return

    # Check against enum values
    valid_methods = {m.value for m in HTTPMethod if m != HTTPMethod.ANY}
    if method_upper_case not in valid_methods:
        raise ValueError(f"Invalid HTTP method: {method}")


def normalize_method(method: str | HTTPMethodLiteral | HTTPMethod) -> str:
    if isinstance(method, HTTPMethod):
        return method.value
    return method.upper() if method != "*" else HTTPMethod.ANY.value


@final
@dataclass(frozen=True)
class _ApiRoute:
    method: HTTPMethodInput
    path: str
    handler: FunctionConfig | Function

    def __post_init__(self) -> None:
        # https://docs.aws.amazon.com/apigateway/latest/developerguide/limits.html
        self._validate_handler()
        self._validate_path()
        self._validate_method()

    def _validate_handler(self) -> None:
        if not isinstance(self.handler, FunctionConfig | Function):
            raise TypeError(
                f"Handler must be FunctionConfig or Function, got {type(self.handler).__name__}"
            )

    def _validate_path(self) -> None:
        # Basic validation
        if not self.path.startswith("/"):
            raise ValueError("Path must start with '/'")

        if len(self.path) > ROUTE_MAX_LENGTH:
            raise ValueError("Path too long")

        if "{}" in self.path:
            raise ValueError("Empty path parameters not allowed")

        # Parameter validation
        params = re.findall(r"{([^}]+)}", self.path)

        if len(params) > ROUTE_MAX_PARAMS:
            raise ValueError("Maximum of 10 path parameters allowed")

        if re.search(r"}{", self.path):
            raise ValueError("Adjacent path parameters not allowed")

        if len(params) != len(set(params)):
            raise ValueError("Duplicate path parameters not allowed")

        # Individual parameter validation
        for param in params:
            self._validate_parameter(self.path, param)

    def _validate_parameter(self, path: str, param: str) -> None:
        # Greedy path parameter handling
        if param.endswith("+"):
            if param != "proxy+":
                raise ValueError("Only {proxy+} is supported for greedy paths")

            param_position = path.index(f"{{{param}}}")
            if param_position != len(path) - len(f"{{{param}}}"):
                raise ValueError("Greedy parameter must be at the end of the path")
            return

        # Regular parameter name validation
        if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", param):
            raise ValueError(f"Invalid parameter name: {param}")

    def _validate_method(self) -> None:
        if isinstance(self.method, str | HTTPMethod):
            _validate_single_method(self.method)
        elif isinstance(self.method, list):
            if not self.method:  # empty check
                raise ValueError("Method list cannot be empty")
            for m in self.method:
                if not isinstance(m, str | HTTPMethod):
                    raise TypeError(f"Invalid method type in list: {type(m)}")
                if isinstance(m, HTTPMethod) and m == HTTPMethod.ANY:
                    raise ValueError("ANY not allowed in method list")
                if isinstance(m, str) and m in ("ANY", "*"):
                    raise ValueError("ANY and * not allowed in method list")
                _validate_single_method(m)
        else:
            raise TypeError(
                f"Method must be string, HTTPMethod, or list of them, got {type(self.method)}"
            )

    @property
    def methods(self) -> list[str]:
        if isinstance(self.method, list):
            return [normalize_method(m) for m in self.method]
        return [normalize_method(self.method)]

    @property
    def path_parts(self) -> list[str]:
        """Get the parts of the path as a list, filtering out empty segments."""
        return [p for p in self.path.split("/") if p]


@final
class Api(Component[ApiResources]):
    _routes: list[_ApiRoute]

    def __init__(self, name: str):
        self._routes = []
        super().__init__(name)

    @property
    def invoke_url(self) -> Output[str]:
        """Get the invoke URL for this API."""
        return self.resources.stage.invoke_url

    @property
    def api_arn(self) -> Output[str]:
        """Get the ARN for this API."""
        return self.resources.rest_api.arn

    def route(
        self,
        http_method: HTTPMethodInput,
        path: str,
        handler: str | FunctionConfig | FunctionConfigDict | Function | None = None,
        /,
        **opts: Unpack[FunctionConfigDict],
    ) -> None:
        """Add a route to the API.

        The route handler can be specified in three ways:
        1. As a complete configuration object (FunctionConfig, FunctionConfigDict, or Function)
        2. As a handler path string with optional FunctionConfigDict fields as kwargs
        3. As FunctionConfigDict fields passed directly as keyword arguments

        Args:
            http_method: HTTP method(s) to handle. Can be:
                - String ("GET", "POST", etc.)
                - HTTPMethod enum value
                - List of methods for multiple method support
                - "ANY" or "*" to handle all methods
            path: URL path for the route
            handler: Route handler specification. Can be:
                - Function handler path as string
                - Complete FunctionConfig object
                - FunctionConfigDict dictionary
                - Function instance
                - None (if handler is specified in opts)
            **opts: Additional FunctionConfigDict fields when using handler path

        Raises:
            ValueError: If the configuration is ambiguous or incomplete
            TypeError: If handler is of invalid type
            ValueError: If a route with the same path and method already exists

        Examples:
            # Single method
            api.route("GET", "/users", "users.index", memory=128)
            api.route(HTTPMethod.GET, "/users", "users.index")

            # Multiple methods
            api.route(["GET", "POST"], "/users", "users.handle")

            # All methods
            api.route("ANY", "/users", "users.handle")

            # Configuration examples
            api.route("GET", "/users", {"handler": "users.index", "memory": 128})
            api.route("GET", "/users", handler="users.index", memory=128)

        """
        # Create the route object
        api_route = self._create_route(http_method, path, handler, opts)

        # Check for duplicate routes
        for method in api_route.methods:
            for existing_route in self._routes:
                # Skip routes with different paths
                if path != existing_route.path:
                    continue
                if (  # Route conflict occurs when:
                    method in existing_route.methods  # Direct method match
                    or method in ("ANY", "*")  # Current route uses ANY
                    or any(
                        m in ("ANY", "*") for m in existing_route.methods
                    )  # Existing route uses ANY
                ):
                    raise ValueError(
                        f"Route conflict: {method} {path} conflicts with existing route."
                    )

        # Add the route if no conflicts found
        self._routes.append(api_route)

    @staticmethod
    def _create_route(
        http_method: HTTPMethodInput,
        path: str,
        handler: str | FunctionConfig | FunctionConfigDict | Function | None,
        opts: dict,
    ) -> _ApiRoute:
        if isinstance(handler, dict | FunctionConfig | Function) and opts:
            raise ValueError(
                "Invalid configuration: cannot combine complete handler "
                "configuration with additional options"
            )

        if isinstance(handler, FunctionConfig | Function):
            return _ApiRoute(http_method, path, handler)

        if isinstance(handler, dict):
            return _ApiRoute(http_method, path, FunctionConfig(**handler))

        if isinstance(handler, str):
            if "handler" in opts:
                raise ValueError(
                    "Ambiguous handler configuration: handler is specified both as positional "
                    "argument and in options"
                )
            return _ApiRoute(http_method, path, FunctionConfig(handler=handler, **opts))

        if handler is None:
            if "handler" not in opts:
                raise ValueError(
                    "Missing handler configuration: when handler argument is None, "
                    "'handler' option must be provided"
                )
            return _ApiRoute(http_method, path, FunctionConfig(**opts))

        raise TypeError(
            f"Invalid handler type: expected str, FunctionConfig, dict, or Function, "
            f"got {type(handler).__name__}"
        )

    @staticmethod
    def path_to_resource_name(path_parts: list[str]) -> str:
        """Convert path parts to a valid resource name.
        Example: ['users', '{id}', 'orders'] -> 'users-id-orders'
        """
        # Remove any curly braces and convert to safe name
        safe_parts = [
            part.replace("{", "").replace("}", "").replace("+", "plus") for part in path_parts
        ]
        # TODO: check of longer than 256? if so cut the beginning or middle?
        return "-".join(safe_parts)

    @staticmethod
    def get_or_create_resource(
        path_parts: list[str], resources: dict[str, Resource], rest_api: RestApi
    ) -> Resource | None:
        if not path_parts:
            return None

        path_key = "/".join(path_parts)
        if path_key in resources:
            return resources[path_key]

        part = path_parts[-1]
        parent_parts = path_parts[:-1]
        parent_resource = (
            Api.get_or_create_resource(parent_parts, resources, rest_api) if parent_parts else None
        )
        parent_id = parent_resource.id if parent_resource else rest_api.root_resource_id
        resource = Resource(
            context().prefix(f"resource-{Api.path_to_resource_name(path_parts)}"),
            rest_api=rest_api.id,
            parent_id=parent_id,
            path_part=part,
        )
        resources[path_key] = resource
        return resource

    def _create_resources(self) -> ApiResources:
        # This is what needs to be done:
        #   1. create rest api
        #   2. for each route:
        #       a. create resource(s)
        #       b. create method(s)
        #       c. create lambda from handler if it doesn't exists (we need to group
        #           routes based on lambda)
        #       d. give lambda resource policy so it can be called by given
        #           gateway/resource/method
        #           https://docs.aws.amazon.com/lambda/latest/dg/access-control-resource-based.html
        #       e. create integration between method and lambda
        #   3. create role for gateway and give it permission to write to cloudwatch
        #   4. create account and give it a role
        #   5. create deployment
        #   6. create stage
        rest_api = RestApi(context().prefix(self.name))

        _create_api_gateway_account_and_role()

        grouped_routes_by_lambda = _group_routes_by_lambda(self._routes)
        group_config_map = _get_group_config_map(grouped_routes_by_lambda)

        resources = {}

        # Create all method-integration pairs in a single comprehension
        method_integration_pairs = [
            pair
            for key, group in grouped_routes_by_lambda.items()
            for pair in self._create_route_resources(
                group,
                rest_api,
                self.get_group_function(key, rest_api, group_config_map[key], group),
                resources,
            )
        ]

        # Flatten the pairs for deployment dependencies
        all_deployment_dependencies = [
            resource for pair in method_integration_pairs for resource in pair
        ]
        deployment = _create_deployment(
            rest_api, self.name, self._routes, all_deployment_dependencies
        )

        stage = Stage(
            context().prefix(f"{self.name}-v1"),
            rest_api=rest_api.id,
            deployment=deployment.id,
            stage_name="v1",
            # xray_tracing_enabled=True,
            access_log_settings={
                "destination_arn": rest_api.name.apply(
                    lambda name: f"arn:aws:logs:{get_region().name}:"
                    f"{get_caller_identity().account_id}"
                    f":log-group:/aws/apigateway/{name}"
                ),
                "format": '{"requestId":"$context.requestId", "ip": "$context.identity.sourceIp", '
                '"caller":"$context.identity.caller", "user":"$context.identity.user",'
                '"requestTime":"$context.requestTime", "httpMethod":'
                '"$context.httpMethod","resourcePath":"$context.resourcePath", '
                '"status":"$context.status","protocol":"$context.protocol", '
                '"responseLength":"$context.responseLength"}',
            },
            variables={"loggingLevel": "INFO"},
        )

        pulumi.export(f"api_{self.name}_arn", rest_api.arn)
        pulumi.export(f"api_{self.name}_invoke_url", stage.invoke_url)
        pulumi.export(f"api_{self.name}_id", rest_api.id)
        pulumi.export(f"api_{self.name}_stage_name", stage.stage_name)

        return ApiResources(rest_api, deployment, stage)

    def _create_method_and_integration(
        self,
        route: _ApiRoute,
        http_method: str,
        resource: Resource,
        rest_api: RestApi,
        function: Function,
    ) -> tuple[Method, Integration]:
        method = Method(
            context().prefix(
                f"method-{http_method}-{self.path_to_resource_name(route.path_parts)}"
            ),
            rest_api=rest_api.id,
            resource_id=resource.id,
            http_method=http_method,
            authorization="NONE",
        )
        integration = Integration(
            context().prefix(
                f"integration-{http_method}-{self.path_to_resource_name(route.path_parts)}"
            ),
            rest_api=rest_api.id,
            resource_id=resource.id,
            http_method=http_method,
            integration_http_method="POST",
            type="AWS_PROXY",
            uri=function.invoke_arn,
        )

        return method, integration

    def _create_route_resources(
        self,
        routes: list[_ApiRoute],
        rest_api: RestApi,
        function: Function,
        resources: dict[str, Resource],
    ) -> list[tuple[Method, Integration]]:
        return [
            # Create method and integration for each route and HTTP method
            self._create_method_and_integration(
                route,
                http_method,
                self.get_or_create_resource(route.path_parts, resources, rest_api),
                rest_api,
                function,
            )
            # For each route and HTTP method
            for route in routes
            for http_method in route.methods
        ]

    def get_group_function(
        self, key: str, rest_api: RestApi, route_with_config: _ApiRoute, routes: list[_ApiRoute]
    ) -> Function:
        if isinstance(route_with_config.handler, Function):
            function = route_with_config.handler
        else:
            # Handler must be FunctionConfig due to validation
            function_config = route_with_config.handler

            # Generate routing file if needed
            routing_file_content = _create_routing_file(routes, route_with_config)

            extra_assets = {}
            if routing_file_content:
                extra_assets["stlv_routing_handler.py"] = StringAsset(routing_file_content)

            # TODO: find better naming strategy, for now use key which is path to func and
            #  replace / with - this will not work if one function used by multiple APIs?? Check!
            function = Function(f"{self.name}-{key.replace('/', '-')}", function_config)
            if extra_assets:
                FunctionAssetsRegistry.add(function, extra_assets)
        Permission(
            context().prefix(f"{function.name}-permission"),
            action="lambda:InvokeFunction",
            function=function.function_name,
            principal="apigateway.amazonaws.com",
            source_arn=rest_api.execution_arn.apply(lambda arn: f"{arn}/*/*"),
        )
        return function


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


def _calculate_route_config_hash(routes: list[_ApiRoute]) -> str:
    """Calculates a stable hash based on the API route configuration."""
    # Create a stable representation of the routes for hashing
    # Sort routes by path, then by sorted methods string to ensure consistency
    sorted_routes_config = sorted(
        [
            {
                "path": route.path,
                "methods": sorted(route.methods),  # Sort methods for consistency
                "handler_key": _get_handler_key_for_trigger(route.handler),
            }
            for route in routes
        ],
        key=lambda r: (r["path"], ",".join(r["methods"])),
    )

    api_config_str = json.dumps(sorted_routes_config, sort_keys=True)
    return sha256(api_config_str.encode()).hexdigest()


def _create_deployment(
    api: RestApi,
    api_name: str,
    routes: list[_ApiRoute],  # Add routes parameter
    depends_on: Input[Sequence[Input[Resource]] | Resource] | None = None,
) -> Deployment:
    """Creates the API deployment, triggering redeployment based on route changes."""

    trigger_hash = _calculate_route_config_hash(routes)
    pulumi.log.debug(f"API '{api_name}' deployment trigger hash based on routes: {trigger_hash}")

    return Deployment(
        context().prefix(f"{api_name}-deployment"),
        rest_api=api.id,
        # Trigger new deployment only when API route config changes
        triggers={"configuration_hash": trigger_hash},
        # Ensure deployment happens after all resources/methods/integrations are created
        opts=ResourceOptions(depends_on=depends_on),
    )


@cache
def _create_api_gateway_account_and_role() -> Account:
    # Get existing account configuration (read-only reference)
    existing_account = Account.get("api-gateway-account-ref", "APIGatewayAccount")

    def handle_existing_role(existing_arn: str) -> Account:
        if existing_arn:
            # Role already configured - return reference, don't create new Account
            logger.info("API Gateway CloudWatch role already configured: %s", existing_arn)
            return existing_account

        # No role configured - create role and Account
        logger.info("No CloudWatch role found, creating Stelvio configuration")
        role = _create_api_gateway_role()

        return Account("api-gateway-account", cloudwatch_role_arn=role.arn)

    return existing_account.cloudwatch_role_arn.apply(handle_existing_role)


API_GATEWAY_ROLE_NAME = "api-gateway-role"


def _create_api_gateway_role() -> Role:
    assume_role_policy = get_policy_document(
        statements=[
            GetPolicyDocumentStatementArgs(
                actions=["sts:AssumeRole"],
                principals=[
                    GetPolicyDocumentStatementPrincipalArgs(
                        identifiers=["apigateway.amazonaws.com"], type="Service"
                    )
                ],
            )
        ]
    )
    return Role(
        API_GATEWAY_ROLE_NAME,
        name="StelvioAPIGatewayPushToCloudWatchLogsRole",
        assume_role_policy=assume_role_policy.json,
        managed_policy_arns=[API_GATEWAY_LOGS_POLICY],
        opts=ResourceOptions(retain_on_delete=True),
    )


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
