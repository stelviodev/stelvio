from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pulumi import Output

    from stelvio.aws.function import Function


USER_POOL_ARN_PARTS = 6


@dataclass(frozen=True)
class _LambdaAuthorizer:
    """Lambda (REQUEST) authorizer for HTTP API."""

    name: str
    function: Function
    identity_sources: list[str]
    ttl: int = 300
    simple_response: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.identity_sources, list):
            raise TypeError(f"Authorizer '{self.name}' identity_sources must be a list")
        if not self.identity_sources:
            raise ValueError(f"Authorizer '{self.name}' requires at least one identity_source")
        for source in self.identity_sources:
            if not isinstance(source, str) or not source:
                raise ValueError(
                    f"Authorizer '{self.name}' identity_source values must be non-empty strings"
                )
        if not (0 <= self.ttl <= 3600):  # noqa: PLR2004
            raise ValueError(
                f"Authorizer '{self.name}' ttl must be between 0 and 3600, got {self.ttl}"
            )


@dataclass(frozen=True)
class _JwtAuthorizer:
    """Generic JWT/OIDC authorizer for HTTP API."""

    name: str
    issuer: str
    audiences: list[str]
    identity_source: str = "$request.header.Authorization"

    def __post_init__(self) -> None:
        if not self.issuer:
            raise ValueError(f"JWT authorizer '{self.name}' issuer cannot be empty")
        if not self.audiences:
            raise ValueError(f"JWT authorizer '{self.name}' audiences cannot be empty")
        for audience in self.audiences:
            if not audience:
                raise ValueError(
                    f"JWT authorizer '{self.name}' audience values must be non-empty strings"
                )


@dataclass(frozen=True)
class _CognitoAuthorizer:
    """Cognito JWT authorizer for HTTP API."""

    name: str
    user_pool_issuer: Output[str]
    audiences: list[Output[str] | str]
    identity_source: str = "$request.header.Authorization"

    def __post_init__(self) -> None:
        if not self.audiences:
            raise ValueError(f"Cognito authorizer '{self.name}' audiences cannot be empty")
        for audience in self.audiences:
            if isinstance(audience, str) and not audience:
                raise ValueError(
                    f"Cognito authorizer '{self.name}' audience values must be non-empty strings"
                )


def _parse_user_pool_arn(authorizer_name: str, arn: str) -> tuple[str, str]:
    parts = arn.split(":", 5)
    if len(parts) != USER_POOL_ARN_PARTS or parts[0] != "arn" or parts[2] != "cognito-idp":
        raise ValueError(f"Cognito authorizer '{authorizer_name}' user_pool ARN is invalid: {arn}")
    region = parts[3]
    resource = parts[5]
    prefix = "userpool/"
    if not region or not resource.startswith(prefix) or not resource[len(prefix) :]:
        raise ValueError(f"Cognito authorizer '{authorizer_name}' user_pool ARN is invalid: {arn}")
    return region, resource[len(prefix) :]


# Union type for all HTTP API authorizers
_HttpAuthorizer = _LambdaAuthorizer | _JwtAuthorizer | _CognitoAuthorizer
