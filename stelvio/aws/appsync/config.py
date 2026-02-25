from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, TypedDict, Unpack

from stelvio.aws.function import Function, FunctionConfig, FunctionConfigDict, parse_handler_config

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


@dataclass(frozen=True, kw_only=True)
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


@dataclass(frozen=True, kw_only=True)
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


@dataclass(frozen=True, kw_only=True)
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


class LambdaAuth:
    """Lambda authorizer authentication for AppSync.

    Accepts handler as str, FunctionConfig, or Function. When handler is a string,
    additional function options (links, memory, timeout, environment) can be provided
    directly. When handler is FunctionConfig or Function, configure those on the
    handler itself.

    Attributes:
        handler: Lambda handler specification (resolved to FunctionConfig or Function).
        result_ttl: Authorization result cache TTL in seconds.
        identity_validation_expression: Regex to validate the authorization token.
    """

    handler: FunctionConfig | Function
    result_ttl: int | None
    identity_validation_expression: str | None

    def __init__(
        self,
        *,
        handler: str | FunctionConfig | Function,
        result_ttl: int | None = None,
        identity_validation_expression: str | None = None,
        **fn_opts: Unpack[FunctionConfigDict],
    ) -> None:
        if isinstance(handler, str) and not handler:
            raise ValueError("handler cannot be empty")
        if isinstance(handler, FunctionConfig | Function) and fn_opts:
            raise ValueError(
                "Cannot specify links, memory, timeout, or environment when handler "
                "is a FunctionConfig or Function instance. Configure these on the "
                "handler directly."
            )

        if isinstance(handler, Function):
            self.handler = handler
        else:
            self.handler = parse_handler_config(handler, fn_opts)

        self.result_ttl = result_ttl
        self.identity_validation_expression = identity_validation_expression


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
    api_key: "dict[str, Any] | None"


class AppSyncDataSourceCustomizationDict(TypedDict, total=False):
    data_source: "DataSourceArgs | dict[str, Any] | None"
    service_role: "RoleArgs | dict[str, Any] | None"


class AppSyncResolverCustomizationDict(TypedDict, total=False):
    resolver: "ResolverArgs | dict[str, Any] | None"


class AppSyncPipeFunctionCustomizationDict(TypedDict, total=False):
    function: "FunctionArgs | dict[str, Any] | None"
