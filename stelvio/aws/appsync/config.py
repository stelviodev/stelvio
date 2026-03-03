from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Required, TypedDict, Unpack, cast

from pulumi import Output

from stelvio.aws.appsync.constants import (
    AUTH_TYPE_API_KEY,
    AUTH_TYPE_COGNITO,
    AUTH_TYPE_IAM,
    AUTH_TYPE_LAMBDA,
    AUTH_TYPE_OIDC,
)
from stelvio.aws.function import Function, FunctionConfig, FunctionConfigDict

if TYPE_CHECKING:
    from pulumi_aws import lambda_
    from pulumi_aws.appsync import (
        DataSourceArgs,
        DomainNameApiAssociationArgs,
        DomainNameArgs,
        FunctionArgs,
        GraphQLApiArgs,
        ResolverArgs,
    )
    from pulumi_aws.iam import RoleArgs

    from stelvio.aws.acm import AcmValidatedDomainCustomizationDict

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

    def to_provider_config(self) -> dict[str, Any]:
        config: dict[str, Any] = {"user_pool_id": self.user_pool_id}
        if self.region:
            config["aws_region"] = self.region
        if self.app_id_client_regex:
            config["app_id_client_regex"] = self.app_id_client_regex
        return config


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

    def to_provider_config(self) -> dict[str, Any]:
        config: dict[str, Any] = {"issuer": self.issuer}
        if self.client_id:
            config["client_id"] = self.client_id
        if self.auth_ttl is not None:
            config["auth_ttl"] = self.auth_ttl
        if self.iat_ttl is not None:
            config["iat_ttl"] = self.iat_ttl
        return config


@dataclass(frozen=True, kw_only=True, init=False)
class LambdaAuth:
    """Lambda authorizer authentication for AppSync.

    Accepts handler as str, FunctionConfig, or Function. When handler is a string,
    additional function options can be provided via ``**fn_opts`` and are parsed
    into a FunctionConfig when creating the authorizer Function.

    Attributes:
        handler: Lambda handler specification.
        result_ttl: Authorization result cache TTL in seconds.
        identity_validation_expression: Regex to validate the authorization token.
        fn_opts: Optional Function config overrides used only when handler is a string.
    """

    handler: str | FunctionConfig | Function
    result_ttl: int | None = None
    identity_validation_expression: str | None = None
    fn_opts: FunctionConfigDict = field(default_factory=dict)

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
                "Cannot specify function options when handler is a FunctionConfig or "
                "Function instance. Configure these on the handler directly."
            )

        object.__setattr__(self, "handler", handler)
        object.__setattr__(self, "result_ttl", result_ttl)
        object.__setattr__(self, "identity_validation_expression", identity_validation_expression)
        object.__setattr__(self, "fn_opts", cast("FunctionConfigDict", dict(fn_opts)))

    def to_authorizer_config(self, invoke_arn: Output[str]) -> dict[str, Any]:
        config: dict[str, Any] = {"authorizer_uri": invoke_arn}
        if self.result_ttl is not None:
            config["authorizer_result_ttl_in_seconds"] = self.result_ttl
        if self.identity_validation_expression:
            config["identity_validation_expression"] = self.identity_validation_expression
        return config


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


def _validate_no_duplicate_auth(auth: AuthConfig, additional_auth: list[AuthConfig]) -> None:
    all_types = [_auth_type_string(auth)]
    for additional in additional_auth:
        auth_type = _auth_type_string(additional)
        if auth_type in all_types:
            raise ValueError(
                f"Duplicate authentication mode '{auth_type}'. "
                "Each auth mode can only appear once across auth and additional_auth."
            )
        all_types.append(auth_type)


def _auth_type_string(auth: AuthConfig) -> str:
    if auth == "iam":
        return AUTH_TYPE_IAM
    if isinstance(auth, ApiKeyAuth):
        return AUTH_TYPE_API_KEY
    if isinstance(auth, CognitoAuth):
        return AUTH_TYPE_COGNITO
    if isinstance(auth, OidcAuth):
        return AUTH_TYPE_OIDC
    if isinstance(auth, LambdaAuth):
        return AUTH_TYPE_LAMBDA
    raise TypeError(f"Unexpected auth config type: {type(auth).__name__}")


# --- AppSync config ---


class AppSyncConfigDict(TypedDict, total=False):
    schema: Required[str]
    auth: Required[AuthConfig]
    additional_auth: list[AuthConfig]
    domain: str | None


@dataclass(frozen=True, kw_only=True)
class AppSyncConfig:
    """Configuration for an AppSync GraphQL API.

    Attributes:
        schema: GraphQL schema as a file path (.graphql/.gql) or inline SDL string.
        auth: Default authentication mode.
        additional_auth: Additional authentication modes.
        domain: Custom domain name for the API.
    """

    schema: str
    auth: AuthConfig
    additional_auth: list[AuthConfig] = field(default_factory=list)
    domain: str | None = None

    def __post_init__(self) -> None:
        if not self.schema:
            raise ValueError("schema cannot be empty")

        validate_auth_config(self.auth)
        for auth_config in self.additional_auth:
            validate_auth_config(auth_config)
        if self.additional_auth:
            _validate_no_duplicate_auth(self.auth, self.additional_auth)


# --- Customization TypedDicts ---


class AppSyncCustomizationDict(TypedDict, total=False):
    api: "GraphQLApiArgs | dict[str, Any] | None"
    domain_name: "DomainNameArgs | dict[str, Any] | None"
    auth_permissions: "lambda_.PermissionArgs | dict[str, Any] | None"
    api_key: "dict[str, Any] | None"

    acm_domain: "AcmValidatedDomainCustomizationDict | None"
    domain_association: "DomainNameApiAssociationArgs | dict[str, Any] | None"
    domain_dns_record: "dict[str, Any] | None"


class AppSyncDataSourceCustomizationDict(TypedDict, total=False):
    data_source: "DataSourceArgs | dict[str, Any] | None"
    service_role: "RoleArgs | dict[str, Any] | None"


class AppSyncResolverCustomizationDict(TypedDict, total=False):
    resolver: "ResolverArgs | dict[str, Any] | None"


class AppSyncPipeFunctionCustomizationDict(TypedDict, total=False):
    function: "FunctionArgs | dict[str, Any] | None"
