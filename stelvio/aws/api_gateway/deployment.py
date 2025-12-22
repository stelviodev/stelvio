import json
from collections.abc import Sequence
from dataclasses import asdict
from hashlib import sha256
from typing import Literal

import pulumi
from pulumi import Input, ResourceOptions
from pulumi_aws.apigateway import Deployment, Resource, RestApi

from stelvio import context
from stelvio.aws.api_gateway.config import _ApiRoute, _Authorizer
from stelvio.aws.cors import CorsConfig
from stelvio.aws.function import Function
from stelvio.aws.function.config import FunctionConfig


def _get_handler_key_for_trigger(handler: Function | FunctionConfig) -> str:
    """Gets a consistent string key representing the handler for trigger calculation."""
    if isinstance(handler, Function):
        # Use the logical name of the Function component
        return f"Function:{handler.name}"
    # Must be FunctionConfig
    return f"Config:{handler.full_handler_path}"


def _calculate_route_config_hash(
    routes: list[_ApiRoute], cors_config: CorsConfig | None = None
) -> str:
    """Calculates a stable hash based on the API route configuration."""

    def _get_auth_key(auth: "_Authorizer | Literal['IAM', False] | None") -> str | None:
        if auth is None or auth is False:
            return None
        if auth == "IAM":
            return "IAM"
        if isinstance(auth, _Authorizer):
            # Create a stable dict representation
            auth_dict = {
                "name": auth.name,
                "type": "token"
                if auth.token_function
                else "request"
                if auth.request_function
                else "cognito",
                "identity_source": str(auth.identity_source),
                "ttl": auth.ttl,
            }
            if auth.token_function:
                auth_dict["token_function"] = auth.token_function.name
            if auth.request_function:
                auth_dict["request_function"] = auth.request_function.name
            if auth.user_pools:
                auth_dict["user_pools"] = sorted(auth.user_pools)

            return json.dumps(auth_dict, sort_keys=True)
        return str(auth)

    # Create a stable representation of the routes for hashing
    # Sort routes by path, then by sorted methods string to ensure consistency
    sorted_routes_config = sorted(
        [
            {
                "path": route.path,
                "methods": sorted(route.methods),  # Sort methods for consistency
                "handler_key": _get_handler_key_for_trigger(route.handler),
                "auth": _get_auth_key(route.auth),
                "cognito_scopes": sorted(route.cognito_scopes) if route.cognito_scopes else None,
            }
            for route in routes
        ],
        key=lambda r: (r["path"], ",".join(r["methods"])),
    )

    config_to_hash = {
        "routes": sorted_routes_config,
        "cors": asdict(cors_config) if cors_config else None,
    }

    api_config_str = json.dumps(config_to_hash, sort_keys=True)
    return sha256(api_config_str.encode()).hexdigest()


def _create_deployment(
    api: RestApi,
    api_name: str,
    routes: list[_ApiRoute],  # Add routes parameter
    depends_on: Input[Sequence[Input[Resource]] | Resource] | None = None,
    cors_config: CorsConfig | None = None,
) -> Deployment:
    """Creates the API deployment, triggering redeployment based on route changes."""

    trigger_hash = _calculate_route_config_hash(routes, cors_config)
    pulumi.log.debug(f"API '{api_name}' deployment trigger hash based on routes: {trigger_hash}")

    return Deployment(
        context().prefix(f"{api_name}-deployment"),
        rest_api=api.id,
        # Trigger new deployment only when API route config changes
        triggers={"configuration_hash": trigger_hash},
        # Ensure deployment happens after all resources/methods/integrations are created
        opts=ResourceOptions(depends_on=depends_on),
    )
