from __future__ import annotations

from dataclasses import dataclass
from typing import TypedDict


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


def normalize_cors_config(
    cors: bool | CorsConfig | CorsConfigDict | None,
) -> CorsConfig | None:
    if cors is True:
        return CorsConfig(allow_origins="*", allow_headers="*", allow_methods="*")
    if isinstance(cors, CorsConfig):
        return cors
    if isinstance(cors, dict):
        return CorsConfig(**cors)
    return None


def cors_config_key(cors: CorsConfig | None) -> dict | None:
    if cors is None:
        return None

    def sort_if_list(value: str | list[str] | None) -> str | list[str] | None:
        return sorted(value) if isinstance(value, list) else value

    return {
        "allow_origins": sort_if_list(cors.allow_origins),
        "allow_methods": sort_if_list(cors.allow_methods),
        "allow_headers": sort_if_list(cors.allow_headers),
        "allow_credentials": cors.allow_credentials,
        "max_age": cors.max_age,
        "expose_headers": sort_if_list(cors.expose_headers),
    }


@dataclass(frozen=True, kw_only=True)
class CorsConfig:
    """CORS configuration for API Gateway and Function URLs.

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
