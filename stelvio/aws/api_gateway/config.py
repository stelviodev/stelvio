import re
from dataclasses import dataclass
from typing import Literal, TypedDict, final

from stelvio.aws.api_gateway.constants import (
    ROUTE_MAX_LENGTH,
    ROUTE_MAX_PARAMS,
    ApiEndpointType,
    HTTPMethod,
    HTTPMethodInput,
    HTTPMethodLiteral,
)
from stelvio.aws.function import Function, FunctionConfig


def _validate_cors_field(value: str | list[str], field_name: str) -> None:
    """Validate CORS field (origins, methods, headers) for common patterns.

    Rejects:
    - Empty strings or empty lists
    - Wildcard '*' in a list (must be a string)
    - Non-string items in lists
    """
    if isinstance(value, str):
        if not value:
            raise ValueError(f"{field_name} string cannot be empty")
    elif isinstance(value, list):
        if not value:
            raise ValueError(f"{field_name} list cannot be empty")
        if "*" in value:
            raise ValueError(
                f"Wildcard '*' must be a string, not in a list. Use {field_name}='*' instead"
            )
        for item in value:
            if not isinstance(item, str) or not item:
                raise ValueError(f"Each {field_name} value must be a non-empty string")
    else:
        raise TypeError(f"{field_name} must be a string or list of strings")


class CorsConfigDict(TypedDict, total=False):
    allow_origins: str | list[str]
    allow_methods: str | list[str]
    allow_headers: str | list[str]
    allow_credentials: bool
    max_age: int | None
    expose_headers: list[str] | None


@dataclass(frozen=True, kw_only=True)
class CorsConfig:
    """CORS configuration for API Gateway.

    Note: REST API v1 only supports single origin (string). Multiple origins (list)
    are supported for HTTP API v2. Validation occurs at the API component level.
    """

    allow_origins: str | list[str] = "*"
    allow_methods: str | list[str] = "*"
    allow_headers: str | list[str] = "*"
    allow_credentials: bool = False
    max_age: int | None = None
    expose_headers: list[str] | None = None

    def __post_init__(self) -> None:
        # Validate allow_origins
        _validate_cors_field(self.allow_origins, "allow_origins")
        if self.allow_credentials and self.allow_origins == "*":
            raise ValueError("allow_credentials=True requires specific origins, cannot use '*'")

        # Validate allow_methods
        self._validate_methods()

        # Validate allow_headers
        _validate_cors_field(self.allow_headers, "allow_headers")

        # Validate max_age
        if self.max_age is not None and self.max_age < 0:
            raise ValueError("max_age must be non-negative")

        # Validate expose_headers
        if self.expose_headers is not None:
            if not self.expose_headers:
                raise ValueError("expose_headers list cannot be empty when specified")
            for header in self.expose_headers:
                if not isinstance(header, str) or not header:
                    raise ValueError("Each expose_headers value must be a non-empty string")

    def _validate_methods(self) -> None:
        valid_methods = {"DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT", "*"}
        _validate_cors_field(self.allow_methods, "allow_methods")
        if isinstance(self.allow_methods, str):
            if self.allow_methods.upper() not in valid_methods:
                raise ValueError(
                    f"Invalid HTTP method '{self.allow_methods}'. Valid: "
                    f"{', '.join(sorted(valid_methods - {'*'}))}, or '*' for all"
                )
        elif isinstance(self.allow_methods, list):
            for method in self.allow_methods:
                if method.upper() not in valid_methods:
                    raise ValueError(
                        f"Invalid HTTP method '{method}'. Valid: "
                        f"{', '.join(sorted(valid_methods - {'*'}))}, or '*' for all"
                    )


class ApiConfigDict(TypedDict, total=False):
    domain_name: str
    stage_name: str
    endpoint_type: ApiEndpointType
    cors: bool | CorsConfig | CorsConfigDict | None


@dataclass(frozen=True, kw_only=True)
class ApiConfig:
    domain_name: str | None = None
    stage_name: str | None = None
    endpoint_type: ApiEndpointType | None = None
    cors: bool | CorsConfig | CorsConfigDict | None = None

    def __post_init__(self) -> None:
        if self.domain_name is not None:
            if not isinstance(self.domain_name, str):
                raise TypeError("Domain name must be a string")
            if not self.domain_name.strip():
                raise ValueError("Domain name cannot be empty")

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

    @property
    def normalized_cors(self) -> CorsConfig | None:
        """Normalize CORS configuration to CorsConfig or None.

        Converts:
        - True → CorsConfig with permissive defaults (allow_origins="*", allow_headers="*",
            allow_methods="*")
        - CorsConfig → returns as-is
        - dict (CorsConfigDict) → CorsConfig(**dict) with validation
        - False or None → None (CORS disabled)
        """
        if self.cors is True:
            return CorsConfig(
                allow_origins="*",
                allow_headers="*",
                allow_methods="*",
            )
        if isinstance(self.cors, CorsConfig):
            return self.cors
        if isinstance(self.cors, dict):
            return CorsConfig(**self.cors)
        return None


@final
@dataclass(frozen=True)
class _ApiRoute:
    method: HTTPMethodInput
    path: str
    handler: FunctionConfig | Function
    auth: "_Authorizer | Literal['IAM', False] | None" = None

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

    This is a config holder, not a Pulumi Component. The Api class creates
    the actual Pulumi authorizer resources in _create_resources().

    Not exported - users get instances via Api.add_*_authorizer() methods.
    """

    name: str
    # One of these is set based on type:
    token_function: Function | None = None
    request_function: Function | None = None
    user_pools: list[str] | None = None
    # Type-specific config (normalized in add_*_authorizer methods):
    # TOKEN: single string, REQUEST: list of strings (normalized), COGNITO: None
    identity_source: str | list[str] | None = None
    ttl: int = 300
