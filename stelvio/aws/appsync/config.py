from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, TypedDict

from stelvio.aws.function import Function, FunctionConfig
from stelvio.aws.layer import Layer
from stelvio.aws.types import AwsArchitecture, AwsLambdaRuntime
from stelvio.link import Link, Linkable

if TYPE_CHECKING:
    from pulumi_aws.appsync import (
        DataSourceArgs,
        DomainNameArgs,
        FunctionArgs,
        GraphQLApiArgs,
        ResolverArgs,
    )
    from pulumi_aws.iam import RoleArgs

# AWS AppSync API key max expiration in days
_API_KEY_MAX_EXPIRY_DAYS = 365


@dataclass(frozen=True)
class ApiKeyAuth:
    """API key authentication for AppSync.

    Attributes:
        expires: Number of days until the API key expires (1-365, default 365).
    """

    expires: int = _API_KEY_MAX_EXPIRY_DAYS

    def __post_init__(self) -> None:
        if (
            not isinstance(self.expires, int)
            or self.expires < 1
            or self.expires > _API_KEY_MAX_EXPIRY_DAYS
        ):
            raise ValueError(
                f"expires must be an integer between 1 and {_API_KEY_MAX_EXPIRY_DAYS}"
            )


@dataclass(frozen=True)
class CognitoAuth:
    """Amazon Cognito User Pool authentication for AppSync.

    Attributes:
        user_pool_id: The Cognito User Pool ID.
        region: AWS region of the user pool. Defaults to the stack's region.
        app_id_client_regex: Regex to match against the client ID in the JWT token.
    """

    user_pool_id: str
    region: str | None = None
    app_id_client_regex: str | None = None

    def __post_init__(self) -> None:
        if not self.user_pool_id:
            raise ValueError("user_pool_id cannot be empty")


@dataclass(frozen=True)
class OidcAuth:
    """OpenID Connect authentication for AppSync.

    Attributes:
        issuer: The OIDC issuer URL.
        client_id: Client identifier to validate against the aud claim.
        auth_ttl: Token expiration TTL in milliseconds.
        iat_ttl: Token issued-at TTL in milliseconds.
    """

    issuer: str
    client_id: str | None = None
    auth_ttl: int | None = None
    iat_ttl: int | None = None

    def __post_init__(self) -> None:
        if not self.issuer:
            raise ValueError("issuer cannot be empty")


@dataclass(frozen=True, kw_only=True)
class LambdaAuth:
    """Lambda authorizer authentication for AppSync.

    Accepts handler as str, FunctionConfig, or Function. When handler is a string,
    additional function options (links, memory, timeout, environment) can be provided
    directly. When handler is FunctionConfig or Function, configure those on the
    handler itself.

    Attributes:
        handler: Lambda handler specification.
        result_ttl: Authorization result cache TTL in seconds.
        identity_validation_expression: Regex to validate the authorization token.
        links: Resources to link to the authorizer function.
        memory: Memory size in MB for the authorizer function.
        timeout: Timeout in seconds for the authorizer function.
        environment: Environment variables for the authorizer function.
    """

    handler: str | FunctionConfig | Function
    result_ttl: int | None = None
    identity_validation_expression: str | None = None
    # Convenience fields when handler is a string
    links: list[Link | Linkable] = field(default_factory=list)
    memory: int | None = None
    timeout: int | None = None
    environment: dict[str, str] = field(default_factory=dict)
    architecture: AwsArchitecture | None = None
    runtime: AwsLambdaRuntime | None = None
    requirements: str | list[str] | Literal[False] | None = None
    layers: list[Layer] = field(default_factory=list)
    folder: str | None = None
    url: Literal["public", "private"] | None = None

    def __post_init__(self) -> None:
        if isinstance(self.handler, str) and not self.handler:
            raise ValueError("handler cannot be empty")
        if isinstance(self.handler, FunctionConfig | Function):
            has_extra = (
                self.links
                or self.memory is not None
                or self.timeout is not None
                or self.environment
                or self.architecture is not None
                or self.runtime is not None
                or self.requirements is not None
                or self.layers
                or self.folder is not None
                or self.url is not None
            )
            if has_extra:
                raise ValueError(
                    "Cannot specify links, memory, timeout, or environment when handler "
                    "is a FunctionConfig or Function instance. Configure these on the "
                    "handler directly."
                )


type AuthConfig = Literal["iam"] | ApiKeyAuth | CognitoAuth | OidcAuth | LambdaAuth

_VALID_AUTH_TYPES = (ApiKeyAuth, CognitoAuth, OidcAuth, LambdaAuth)


def validate_auth_config(auth: AuthConfig) -> None:
    """Validate that auth is a valid AuthConfig value."""
    if auth == "iam":
        return
    if isinstance(auth, _VALID_AUTH_TYPES):
        return
    raise TypeError(
        f"Invalid auth config: {auth!r}. Must be 'iam', ApiKeyAuth, CognitoAuth, "
        "OidcAuth, or LambdaAuth."
    )


# --- Customization TypedDicts ---


class AppSyncCustomizationDict(TypedDict, total=False):
    api: "GraphQLApiArgs | dict[str, Any] | None"
    domain_name: "DomainNameArgs | dict[str, Any] | None"


class AppSyncDataSourceCustomizationDict(TypedDict, total=False):
    data_source: "DataSourceArgs | dict[str, Any] | None"
    service_role: "RoleArgs | dict[str, Any] | None"


class AppSyncResolverCustomizationDict(TypedDict, total=False):
    resolver: "ResolverArgs | dict[str, Any] | None"


class AppSyncPipeFunctionCustomizationDict(TypedDict, total=False):
    function: "FunctionArgs | dict[str, Any] | None"
