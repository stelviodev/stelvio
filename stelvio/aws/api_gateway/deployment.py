import json
from collections.abc import Sequence
from hashlib import sha256
from typing import TYPE_CHECKING, Literal

import pulumi
from pulumi import Input, ResourceOptions
from pulumi_aws.apigateway import Deployment, Resource, RestApi

from stelvio import context
from stelvio.aws.api_gateway.config import _ApiRoute, _Authorizer
from stelvio.aws.function import Function
from stelvio.aws.function.config import FunctionConfig

if TYPE_CHECKING:
    from stelvio.aws.cors import CorsConfig


def _get_handler_key_for_trigger(handler: Function | FunctionConfig) -> str:
    """Gets a consistent string key representing the handler for trigger calculation."""
    if isinstance(handler, Function):
        return f"Function:{handler.name}"
    return f"Config:{handler.full_handler_path}"


def _get_auth_key(
    auth: "_Authorizer | Literal['IAM', False] | None",
) -> "str | Literal[False] | None":
    """Gets a serializable key for auth config."""
    if isinstance(auth, _Authorizer):
        return f"Authorizer:{auth.name}"
    return auth  # None, False, "IAM" serialize as-is


def _get_cors_key(cors_config: "CorsConfig | None") -> dict | None:
    """Gets a serializable representation of CORS config."""
    if cors_config is None:
        return None

    def sort_if_list(val: str | list[str] | None) -> str | list[str] | None:
        return sorted(val) if isinstance(val, list) else val

    return {
        "allow_origins": sort_if_list(cors_config.allow_origins),
        "allow_methods": sort_if_list(cors_config.allow_methods),
        "allow_headers": sort_if_list(cors_config.allow_headers),
        "allow_credentials": cors_config.allow_credentials,
        "max_age": cors_config.max_age,
        "expose_headers": sort_if_list(cors_config.expose_headers),
    }


def _calculate_deployment_hash(
    routes: list[_ApiRoute],
    default_auth: "_Authorizer | Literal['IAM'] | None" = None,
    cors_config: "CorsConfig | None" = None,
) -> str:
    """Calculates a stable hash for deployment trigger based on API configuration."""

    def get_effective_auth(route: _ApiRoute) -> "_Authorizer | Literal['IAM', False] | None":
        if route.auth is not None:
            return route.auth
        return default_auth

    sorted_routes_config = sorted(
        [
            {
                "path": route.path,
                "methods": sorted(route.methods),
                "handler_key": _get_handler_key_for_trigger(route.handler),
                "auth_key": _get_auth_key(get_effective_auth(route)),
                "cognito_scopes": sorted(route.cognito_scopes) if route.cognito_scopes else None,
            }
            for route in routes
        ],
        key=lambda r: (r["path"], ",".join(r["methods"])),
    )

    config = {
        "routes": sorted_routes_config,
        "cors": _get_cors_key(cors_config),
    }

    return sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()


def _create_deployment(
    api: RestApi,
    api_name: str,
    trigger_hash: str,
    depends_on: Input[Sequence[Input[Resource]] | Resource] | None = None,
) -> Deployment:
    """Creates the API deployment, triggering redeployment based on config changes."""
    pulumi.log.debug(f"API '{api_name}' deployment trigger hash: {trigger_hash}")

    return Deployment(
        context().prefix(f"{api_name}-deployment"),
        rest_api=api.id,
        # Trigger new deployment only when API route config changes
        triggers={"configuration_hash": trigger_hash},
        # Ensure deployment happens after all resources/methods/integrations are created
        opts=ResourceOptions(depends_on=depends_on),
    )
