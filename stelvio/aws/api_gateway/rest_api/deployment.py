import json
from hashlib import sha256
from typing import TYPE_CHECKING, Literal

from pulumi import Output

from stelvio.aws.api_gateway.rest_api.config import _ApiRoute, _Authorizer
from stelvio.aws.cors import cors_config_key
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
) -> "dict | Literal['IAM', False] | None":
    """The authorizer's whole config, not just its name: API Gateway applies a changed TTL,
    identity source or user pool to a stage only on a new deployment."""
    if isinstance(auth, _Authorizer):
        return {
            "name": auth.name,
            "function": auth.handler_key,
            "user_pools": auth.user_pools,  # may hold Outputs (a UserPool's arn)
            "identity_source": auth.identity_source,
            "ttl": auth.ttl,
        }
    return auth  # None, False, "IAM" serialize as-is


def _get_cors_key(cors_config: "CorsConfig | None") -> dict | None:
    """Gets a serializable representation of CORS config."""
    return cors_config_key(cors_config)


def _calculate_deployment_hash(
    routes: list[_ApiRoute],
    default_auth: "_Authorizer | Literal['IAM'] | None" = None,
    cors_config: "CorsConfig | None" = None,
) -> Output[str]:
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

    return Output.from_input(config).apply(
        lambda resolved: sha256(json.dumps(resolved, sort_keys=True).encode()).hexdigest()
    )
