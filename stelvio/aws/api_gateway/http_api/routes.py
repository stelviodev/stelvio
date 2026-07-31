from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from stelvio.aws.api_gateway.methods import normalize_method, validate_method_input
from stelvio.aws.api_gateway.rest_api.constants import (
    ROUTE_MAX_LENGTH,
    ROUTE_MAX_PARAMS,
    HTTPMethod,
    HTTPMethodInput,
    HTTPMethodLiteral,
)
from stelvio.aws.api_gateway.validators import _validate_path_param
from stelvio.aws.function import Function, FunctionConfig

if TYPE_CHECKING:
    from stelvio.aws.api_gateway.http_api.authorizers import _HttpAuthorizer


STAGE_NAME_MAX_LENGTH = 128


def validate_stage_name(stage_name: str) -> None:
    if len(stage_name) > STAGE_NAME_MAX_LENGTH:
        raise ValueError(f"Stage name must be at most {STAGE_NAME_MAX_LENGTH} characters")
    if stage_name.startswith("$"):
        if stage_name != "$default":
            raise ValueError(
                f"Stage name starting with '$' must be exactly '$default', got {stage_name!r}"
            )
        return
    if not re.match(r"^[a-zA-Z0-9_-]+$", stage_name):
        raise ValueError(
            f"Stage name must contain only alphanumerics, hyphens, and underscores, "
            f"got {stage_name!r}"
        )


def _validate_path_for_http_api(path: str) -> None:
    """Validate a route path — allows $default in addition to normal paths."""
    if path == "$default":
        return  # Valid only with ANY method — checked at route-creation time

    if not path.startswith("/"):
        raise ValueError("Path must start with '/'")
    if len(path) > ROUTE_MAX_LENGTH:
        raise ValueError("Path too long")
    if "{}" in path:
        raise ValueError("Empty path parameters not allowed")

    params = re.findall(r"{([^}]+)}", path)
    if len(params) > ROUTE_MAX_PARAMS:
        raise ValueError("Maximum of 10 path parameters allowed")
    if re.search(r"}{", path):
        raise ValueError("Adjacent path parameters not allowed")
    if len(params) != len(set(params)):
        raise ValueError("Duplicate path parameters not allowed")
    for param in params:
        _validate_path_param(path, param)


def _validate_method_for_http_api(
    method: HTTPMethodInput,
    path: str,
) -> None:
    """Validate method(s) for HTTP API routes."""
    validate_method_input(
        method,
        allow_any_in_list_message="ANY and * are not allowed in a method list",
    )
    if path == "$default" and (isinstance(method, list) or normalize_method(method) != "ANY"):
        raise ValueError("$default path is only valid with method ANY (or *)")


def route_key(method: str | HTTPMethodLiteral | HTTPMethod, path: str) -> str:
    """Return the AWS route key string for a given method+path."""
    if path == "$default":
        return "$default"
    return f"{normalize_method(method)} {path}"


@dataclass(frozen=True)
class _HttpRoute:
    """A single HTTP API route specification."""

    method: HTTPMethodInput
    path: str
    handler: FunctionConfig | Function
    auth: _HttpAuthorizer | Literal["IAM", False] | None = None
    jwt_scopes: list[str] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.handler, FunctionConfig | Function):
            raise TypeError(
                f"Handler must be FunctionConfig or Function, got {type(self.handler).__name__}"
            )
        _validate_path_for_http_api(self.path)
        _validate_method_for_http_api(self.method, self.path)

    @property
    def methods(self) -> list[str]:
        if isinstance(self.method, list):
            return [normalize_method(m) for m in self.method]
        return [normalize_method(self.method)]

    @property
    def route_keys(self) -> list[str]:
        return [route_key(m, self.path) for m in self.methods]
