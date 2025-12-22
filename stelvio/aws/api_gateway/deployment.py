import json
from collections.abc import Sequence
from hashlib import sha256

import pulumi
from pulumi import Input, ResourceOptions
from pulumi_aws.apigateway import Deployment, Resource, RestApi

from stelvio import context
from stelvio.aws.api_gateway.config import _ApiRoute
from stelvio.aws.function import Function
from stelvio.aws.function.config import FunctionConfig


def _get_handler_key_for_trigger(handler: Function | FunctionConfig) -> str:
    """Gets a consistent string key representing the handler for trigger calculation."""
    if isinstance(handler, Function):
        # Use the logical name of the Function component
        return f"Function:{handler.name}"
    # Must be FunctionConfig
    return f"Config:{handler.full_handler_path}"


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
