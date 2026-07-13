import re
from dataclasses import dataclass
from typing import Literal, TypedDict, final

from pulumi import Input

from stelvio.aws.api_gateway.methods import normalize_method, validate_method_input
from stelvio.aws.api_gateway.rest_api.constants import (
    ROUTE_MAX_LENGTH,
    ROUTE_MAX_PARAMS,
    ApiEndpointType,
    HTTPMethodInput,
)
from stelvio.aws.api_gateway.validators import (
    _validate_path_param,
    validate_api_mapping_key,
    validate_domain_name,
    validate_log_retention_days,
)
from stelvio.aws.cors import CorsConfig, CorsConfigDict, normalize_cors_config
from stelvio.aws.function import Function, FunctionConfig


class RestApiConfigDict(TypedDict, total=False):
    domain_name: str
    base_path: str
    stage_name: str
    endpoint_type: ApiEndpointType
    cors: bool | CorsConfig | CorsConfigDict | None
    access_log_retention_days: int | Literal["forever"]


@dataclass(frozen=True, kw_only=True)
class RestApiConfig:
    domain_name: str | None = None
    base_path: str | None = None
    stage_name: str | None = None
    endpoint_type: ApiEndpointType | None = None
    cors: bool | CorsConfig | CorsConfigDict | None = None
    access_log_retention_days: int | Literal["forever"] = 30

    def __post_init__(self) -> None:
        if self.domain_name is not None:
            validate_domain_name(self.domain_name)
        elif self.base_path is not None:
            raise ValueError("base_path requires domain_name to be set")

        if self.base_path is not None:
            validate_api_mapping_key(self.base_path, field_name="base_path")

        if self.stage_name is not None:
            if not self.stage_name:
                raise ValueError("Stage name cannot be empty")

            if not re.match(r"^[a-zA-Z0-9_-]+$", self.stage_name):
                raise ValueError(
                    "Stage name can only contain alphanumeric characters, hyphens, and underscores"
                )

        if self.endpoint_type is not None and self.endpoint_type not in ("regional", "edge"):
            raise ValueError(
                f"Invalid endpoint type: {self.endpoint_type}. "
                "Only 'regional' and 'edge' are supported."
            )

        validate_log_retention_days(self.access_log_retention_days)

    @property
    def normalized_cors(self) -> CorsConfig | None:
        return normalize_cors_config(self.cors)


@final
@dataclass(frozen=True)
class _ApiRoute:
    method: HTTPMethodInput
    path: str
    handler: FunctionConfig | Function
    auth: "_Authorizer | Literal['IAM', False] | None" = None
    cognito_scopes: list[str] | None = None

    def __post_init__(self) -> None:
        # https://docs.aws.amazon.com/apigateway/latest/developerguide/limits.html
        self._validate_handler()
        self._validate_path()
        self._validate_method()
        self._validate_cognito_scopes()

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
            _validate_path_param(self.path, param)

    def _validate_method(self) -> None:
        validate_method_input(
            self.method,
            allow_any_in_list_message="ANY and * not allowed in method list",
            use_repr_in_invalid_method=False,
        )

    def _validate_cognito_scopes(self) -> None:
        """Validate that cognito_scopes is only used with CognitoAuthorizer."""
        if self.cognito_scopes is None:
            return

        # Early return if it's a Cognito authorizer - all good
        if isinstance(self.auth, _Authorizer) and self.auth.user_pools is not None:
            return

        # Determine auth type for error message
        if isinstance(self.auth, _Authorizer):
            if self.auth.token_function is not None:
                auth_desc = "token authorizer"
            else:  # request_function is not None
                auth_desc = "request authorizer"
        elif self.auth == "IAM":
            auth_desc = "IAM authorization"
        else:  # False or None
            auth_desc = "no authorization"

        raise ValueError(
            f"cognito_scopes only works with Cognito authorizers, but route uses {auth_desc}"
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


def path_to_resource_name(path_parts: list[str]) -> str:
    """Convert path parts to a valid resource name.

    Example: ['users', '{id}', 'orders'] -> 'users-id-orders'

    Strips curly braces and converts special characters to safe names.
    """
    safe_parts = [
        part.replace("{", "").replace("}", "").replace("+", "plus") for part in path_parts
    ]
    return "-".join(safe_parts) or "root"


@dataclass(frozen=True)
class _Authorizer:
    """API Gateway authorizer configuration.

    This is a config holder, not a Pulumi Component. The RestApi class creates
    the actual Pulumi authorizer resources in _create_resources().

    Not exported - users get instances via RestApi.add_*_authorizer() methods.
    """

    name: str
    # One of these is set based on type:
    token_function: Function | None = None
    request_function: Function | None = None
    user_pools: list[Input[str]] | None = None
    # Type-specific config (normalized in add_*_authorizer methods):
    # TOKEN: single string, REQUEST: list of strings (normalized), COGNITO: None
    identity_source: str | list[str] | None = None
    ttl: int = 300
