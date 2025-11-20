"""CORS support for API Gateway REST APIs.

This module creates:
1. OPTIONS methods with MOCK integration for preflight requests
2. Gateway responses with CORS headers for error responses (4XX/5XX)
"""

from pulumi import Output
from pulumi_aws.apigateway import (
    Integration,
    IntegrationResponse,
    Method,
    MethodResponse,
    Resource,
    RestApi,
)
from pulumi_aws.apigateway import Response as GatewayResponse

from stelvio import context
from stelvio.aws.api_gateway.config import CorsConfig, _ApiRoute, path_to_resource_name
from stelvio.component import safe_name


def create_cors_gateway_responses(
    rest_api: RestApi, cors_config: CorsConfig, api_name: str
) -> list[GatewayResponse]:
    """Create gateway responses with CORS headers for error responses.

    Gateway responses handle errors that occur before reaching Lambda (auth failures,
    rate limits, etc.). Without CORS headers on these responses, browsers block them
    with CORS errors instead of showing the actual error.

    Args:
        rest_api: The REST API to add gateway responses to
        cors_config: CORS configuration with allow_origins, expose_headers, etc.
        api_name: Name of the API (for unique resource naming)

    Returns:
        List of GatewayResponse resources for deployment dependencies
    """
    response_types = ["DEFAULT_4XX", "DEFAULT_5XX"]
    gateway_responses = []

    # Build response parameters (CORS headers)
    # Values must be wrapped in single quotes: "'*'" or "'value1,value2'"
    response_parameters = {}

    # Access-Control-Allow-Origin (required)
    origin_value = _format_cors_header_value(cors_config.allow_origins)
    response_parameters["gatewayresponse.header.Access-Control-Allow-Origin"] = f"'{origin_value}'"

    # Access-Control-Expose-Headers (optional)
    if cors_config.expose_headers:
        expose_value = _format_cors_header_value(cors_config.expose_headers)
        response_parameters["gatewayresponse.header.Access-Control-Expose-Headers"] = (
            f"'{expose_value}'"
        )

    # Access-Control-Allow-Credentials (optional, only if true)
    if cors_config.allow_credentials:
        response_parameters["gatewayresponse.header.Access-Control-Allow-Credentials"] = "'true'"

    # Create gateway response for each response type
    for response_type in response_types:
        gateway_response = GatewayResponse(
            safe_name(
                context().prefix(),
                f"{api_name}-gateway-response-cors-{response_type.lower()}",
                128,
            ),
            rest_api_id=rest_api.id,
            response_type=response_type,
            response_parameters=response_parameters,
        )
        gateway_responses.append(gateway_response)

    return gateway_responses


def create_cors_options_methods(
    rest_api: RestApi,
    routes: list[_ApiRoute],
    cors_config: CorsConfig,
    resources: dict[str, Resource],
    api_name: str,
) -> list[tuple[Method, MethodResponse, Integration, IntegrationResponse]]:
    """Create OPTIONS methods for CORS preflight requests.

    When browsers make cross-origin requests with custom headers, they first send
    a preflight OPTIONS request. This creates MOCK integration OPTIONS methods that
    respond immediately with CORS headers (no Lambda invocation needed).

    Args:
        rest_api: The REST API to add OPTIONS methods to
        routes: List of API routes to extract paths from
        cors_config: CORS configuration with allowed methods, headers, etc.
        resources: Dict of path → Resource (reuses existing resources)
        api_name: Name of the API (for unique resource naming)

    Returns:
        List of tuples (Method, MethodResponse, Integration, IntegrationResponse) for deployment
        dependencies
    """
    # Get one route per unique path
    unique_routes = {route.path: route for route in routes}.values()

    return [
        _create_options_method(
            rest_api,
            # Get resource ID using route's path_parts property
            resources["/".join(r.path_parts)].id if r.path_parts else rest_api.root_resource_id,
            # Pass path_parts for resource naming
            r.path_parts,
            # Build CORS response headers for this path
            _build_cors_response_headers(cors_config, r.path, routes),
            api_name,
        )
        for r in unique_routes
    ]


def _create_options_method(
    rest_api: RestApi,
    resource_id: Output[str],
    path_parts: list[str],
    response_headers: dict[str, str],
    api_name: str,
) -> tuple[Method, MethodResponse, Integration, IntegrationResponse]:
    """Create a single OPTIONS method with MOCK integration for a path.

    Creates:
    1. Method (OPTIONS)
    2. MethodResponse (200 with CORS headers)
    3. Integration (MOCK type - no backend)
    4. IntegrationResponse (maps to MethodResponse)

    Args:
        rest_api: The REST API
        resource_id: Resource ID for this path
        path_parts: Path parts for resource naming
        response_headers: CORS response headers to return
        api_name: Name of the API (for unique resource naming)

    Returns:
        Tuple of (Method, MethodResponse, Integration, IntegrationResponse)
        for deployment dependencies
    """
    # Create resource name for Pulumi resources
    resource_name_part = path_to_resource_name(path_parts) if path_parts else "root"

    # Create OPTIONS method (no authorization for preflight)
    method = Method(
        safe_name(context().prefix(), f"{api_name}-method-OPTIONS-{resource_name_part}", 128),
        rest_api=rest_api.id,
        resource_id=resource_id,
        http_method="OPTIONS",
        authorization="NONE",
    )

    # Method response (what the method returns)
    method_response = MethodResponse(
        safe_name(
            context().prefix(), f"{api_name}-method-response-OPTIONS-{resource_name_part}", 128
        ),
        rest_api=rest_api.id,
        resource_id=resource_id,
        http_method=method.http_method,
        status_code="200",
        response_parameters={f"method.response.header.{key}": False for key in response_headers},
    )

    # MOCK integration (no backend, API Gateway responds directly)
    integration = Integration(
        safe_name(context().prefix(), f"{api_name}-integration-OPTIONS-{resource_name_part}", 128),
        rest_api=rest_api.id,
        resource_id=resource_id,
        http_method=method.http_method,
        type="MOCK",
        request_templates={"application/json": '{"statusCode": 200}'},
    )

    # Integration response (maps integration to method response with header values)
    integration_response = IntegrationResponse(
        safe_name(
            context().prefix(),
            f"{api_name}-integration-response-OPTIONS-{resource_name_part}",
            128,
        ),
        rest_api=rest_api.id,
        resource_id=resource_id,
        http_method=method.http_method,
        status_code=method_response.status_code,
        response_parameters={
            f"method.response.header.{key}": f"'{value}'"
            for key, value in response_headers.items()
        },
    )

    return method, method_response, integration, integration_response


def _build_cors_response_headers(
    cors_config: CorsConfig, path: str, routes: list[_ApiRoute]
) -> dict[str, str]:
    """Build CORS response headers for OPTIONS method.

    Args:
        cors_config: CORS configuration
        path: The path this OPTIONS method handles
        routes: All routes (to determine allowed methods)

    Returns:
        Dict of header name → header value (without quotes)
    """
    headers = {"Access-Control-Allow-Origin": _format_cors_header_value(cors_config.allow_origins)}

    # Access-Control-Allow-Methods (methods used on this path + OPTIONS)
    allowed_methods = _get_allowed_methods_for_path(path, routes, cors_config)
    headers["Access-Control-Allow-Methods"] = ",".join(sorted(allowed_methods))

    # Access-Control-Allow-Headers
    headers["Access-Control-Allow-Headers"] = _format_cors_header_value(cors_config.allow_headers)

    # Access-Control-Max-Age (optional)
    if cors_config.max_age is not None:
        headers["Access-Control-Max-Age"] = str(cors_config.max_age)

    # Access-Control-Allow-Credentials (optional)
    if cors_config.allow_credentials:
        headers["Access-Control-Allow-Credentials"] = "true"

    return headers


def _get_allowed_methods_for_path(
    path: str, routes: list[_ApiRoute], cors_config: CorsConfig
) -> set[str]:
    """Get allowed HTTP methods for a specific path.

    If cors_config.allow_methods is "*", returns all standard methods.
    Otherwise, returns intersection of route methods and configured methods.
    Always includes OPTIONS.

    Args:
        path: The path to check
        routes: All API routes
        cors_config: CORS configuration

    Returns:
        Set of allowed HTTP method strings (e.g., {"GET", "POST", "OPTIONS"})
    """
    standard_methods = {"DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"}

    # Collect methods from routes
    methods_raw = {method for route in routes if route.path == path for method in route.methods}
    route_methods = standard_methods.copy() if "ANY" in methods_raw else methods_raw

    # Wildcard means all standard methods
    if "*" in cors_config.allow_methods:
        return standard_methods

    # Get configured methods
    methods_list = (
        cors_config.allow_methods
        if isinstance(cors_config.allow_methods, list)
        else [cors_config.allow_methods]
    )
    configured = {m.upper() for m in methods_list}

    return configured.intersection(route_methods) | {"OPTIONS"}


def _format_cors_header_value(value: str | list[str]) -> str:
    if isinstance(value, str):
        return value
    return ",".join(value)
