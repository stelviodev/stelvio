from __future__ import annotations

import re
import warnings
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from stelvio.aws.api_gateway.constants import (
    ROUTE_MAX_LENGTH,
    ROUTE_MAX_PARAMS,
    HTTPMethod,
    HTTPMethodInput,
    HTTPMethodLiteral,
)
from stelvio.aws.function import Function, FunctionConfig

if TYPE_CHECKING:
    from stelvio.aws.http_api._authorizers import _HttpAuthorizer

# CloudWatch-valid log retention values in days
VALID_LOG_RETENTION_DAYS = {
    1,
    3,
    5,
    7,
    14,
    30,
    60,
    90,
    120,
    150,
    180,
    365,
    400,
    545,
    731,
    1827,
    3653,
}

# v1 → v2 identity-source rewrite table
_V1_TO_V2_REWRITES: dict[str, str] = {}


def _build_v1_v2_rewrites() -> None:
    """Build the v1→v2 identity source rewrite entries for context.* and stageVariables.*"""
    # context.* → $context.*
    _V1_TO_V2_REWRITES["context.identity.sourceIp"] = "$context.identity.sourceIp"
    # Generic context prefix handled in rewrite function


_build_v1_v2_rewrites()

# Tracks already-warned (authorizer_name, identity_source) pairs
_warned_identity_sources: set[tuple[str, str]] = set()


def rewrite_v1_identity_source(authorizer_name: str, source: str) -> str:
    """Rewrite v1-style identity source to v2 format, warning once per unique combo."""
    if source.startswith("$"):
        return source  # Already v2

    v2 = None
    if source.startswith("method.request.header."):
        suffix = source[len("method.request.header.") :]
        v2 = f"$request.header.{suffix}"
    elif source.startswith("method.request.querystring."):
        suffix = source[len("method.request.querystring.") :]
        v2 = f"$request.querystring.{suffix}"
    elif source.startswith("method.request.path."):
        suffix = source[len("method.request.path.") :]
        v2 = f"$request.path.{suffix}"
    elif source.startswith("context."):
        suffix = source[len("context.") :]
        v2 = f"$context.{suffix}"
    elif source.startswith("stageVariables."):
        suffix = source[len("stageVariables.") :]
        v2 = f"$stageVariables.{suffix}"

    if v2 is None:
        raise ValueError(
            f"identity source '{source}' has no v2 equivalent.\n"
            "HTTP APIs use $request.header.X (single value). See AWS docs:\n"
            "https://docs.aws.amazon.com/apigateway/latest/developerguide/"
            "http-api-lambda-authorizer.html"
        )

    key = (authorizer_name, source)
    if key not in _warned_identity_sources:
        _warned_identity_sources.add(key)
        warnings.warn(
            f"Identity source '{source}' uses v1 format. Rewritten to '{v2}' for HTTP API. "
            "Update your authorizer to use v2 format directly.",
            DeprecationWarning,
            stacklevel=4,
        )
    return v2


def validate_log_retention_days(value: int | None) -> None:
    if value is None:
        return
    if value not in VALID_LOG_RETENTION_DAYS:
        raise ValueError(
            f"Invalid access_log_retention_days={value!r}. "
            f"Must be None or one of: {sorted(VALID_LOG_RETENTION_DAYS)}"
        )


def validate_stage_name(stage_name: str) -> None:
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


def validate_api_mapping_key(key: str) -> None:
    if not key:
        raise ValueError("api_mapping_key cannot be empty string (use None for root mapping)")
    if key.startswith("/") or key.endswith("/"):
        raise ValueError(f"api_mapping_key must not start or end with '/', got {key!r}")
    if "//" in key:
        raise ValueError(f"api_mapping_key must not contain empty path segments (//), got {key!r}")


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


def _validate_path_param(path: str, param: str) -> None:
    if param.endswith("+"):
        if param != "proxy+":
            raise ValueError("Only {proxy+} is supported for greedy paths")
        param_pos = path.index(f"{{{param}}}")
        if param_pos != len(path) - len(f"{{{param}}}"):
            raise ValueError("Greedy parameter must be at the end of the path")
        return
    if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", param):
        raise ValueError(f"Invalid parameter name: {param}")


def _validate_method_for_http_api(
    method: HTTPMethodInput,
    path: str,
) -> None:
    """Validate method(s) for HTTP API routes."""
    if isinstance(method, list):
        if path == "$default":
            raise ValueError("$default path is only valid with method ANY (or *)")
        if not method:
            raise ValueError("Method list cannot be empty")
        for m in method:
            if isinstance(m, str) and m.upper() in ("ANY", "*"):
                raise ValueError("ANY and * are not allowed in a method list")
            if isinstance(m, HTTPMethod) and m == HTTPMethod.ANY:
                raise ValueError("ANY is not allowed in a method list")
            _validate_single_method(m)
    else:
        _validate_single_method(method)
        if path == "$default":
            norm = normalize_method(method)
            if norm not in ("ANY", "*"):
                raise ValueError("$default path is only valid with method ANY (or *)")


def _validate_single_method(method: str | HTTPMethod) -> None:
    if isinstance(method, HTTPMethod):
        return  # All HTTPMethod values are valid
    m = method.upper()
    if m in ("ANY", "*"):
        return
    valid = {v.value for v in HTTPMethod if v != HTTPMethod.ANY}
    if m not in valid:
        raise ValueError(f"Invalid HTTP method: {method!r}")


def normalize_method(method: str | HTTPMethodLiteral | HTTPMethod) -> str:
    if isinstance(method, HTTPMethod):
        return method.value
    m = method.upper()
    return "ANY" if m == "*" else m


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
