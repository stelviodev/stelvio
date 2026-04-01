from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, TypedDict

from stelvio.aws.email import Email  # noqa: TC001

if TYPE_CHECKING:
    import pulumi_aws
    from pulumi import Input

    from stelvio.aws.acm import AcmValidatedDomainCustomizationDict
    from stelvio.aws.cognito.user_pool import UserPool
    from stelvio.aws.cognito.user_pool_client import UserPoolClient
    from stelvio.aws.function import Function, FunctionConfig, FunctionConfigDict
    from stelvio.aws.permission import AwsPermission

type SignInIdentifier = Literal["email", "phone"]
type AliasIdentifier = Literal["email", "phone", "preferred_username"]
type MfaMode = Literal["off", "optional", "on"]
type PoolTier = Literal["lite", "essentials", "plus"]
type IdentityProviderType = Literal["google", "facebook", "apple", "amazon", "oidc", "saml"]
type TriggerHandler = str | FunctionConfig | FunctionConfigDict | Function

VALID_TRIGGER_NAMES: set[str] = {
    "pre_sign_up",
    "post_confirmation",
    "pre_authentication",
    "post_authentication",
    "pre_token_generation",
    "user_migration",
    "define_auth_challenge",
    "create_auth_challenge",
    "verify_auth_challenge_response",
    "custom_message",
}

PROVIDER_TYPE_MAP: dict[str, str] = {
    "google": "Google",
    "facebook": "Facebook",
    "apple": "SignInWithApple",
    "amazon": "LoginWithAmazon",
    "oidc": "OIDC",
    "saml": "SAML",
}

# Cognito prefix domains: lowercase alphanumeric + hyphens, 1-63 chars,
# can't start or end with a hyphen.
_PREFIX_DOMAIN_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")

# Hostname label: alphanumeric + hyphens, 1-63 chars per label.
_HOSTNAME_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$", re.IGNORECASE)


class PasswordPolicyDict(TypedDict, total=False):
    min_length: int
    require_lowercase: bool
    require_uppercase: bool
    require_numbers: bool
    require_symbols: bool
    temporary_password_validity_days: int


@dataclass(frozen=True, kw_only=True)
class PasswordPolicy:
    min_length: int = 8
    require_lowercase: bool = True
    require_uppercase: bool = True
    require_numbers: bool = True
    require_symbols: bool = True
    temporary_password_validity_days: int = 7


class TriggerConfigDict(TypedDict, total=False):
    pre_sign_up: TriggerHandler
    post_confirmation: TriggerHandler
    pre_authentication: TriggerHandler
    post_authentication: TriggerHandler
    pre_token_generation: TriggerHandler
    user_migration: TriggerHandler
    define_auth_challenge: TriggerHandler
    create_auth_challenge: TriggerHandler
    verify_auth_challenge_response: TriggerHandler
    custom_message: TriggerHandler


class UserPoolConfigDict(TypedDict, total=False):
    usernames: list[SignInIdentifier]
    aliases: list[AliasIdentifier]
    mfa: MfaMode
    software_token: bool
    triggers: TriggerConfigDict
    password: PasswordPolicy | PasswordPolicyDict
    email: Email
    tier: PoolTier
    deletion_protection: bool
    domain: str


@dataclass(frozen=True, kw_only=True)
class UserPoolConfig:
    usernames: list[SignInIdentifier] = field(default_factory=list)
    aliases: list[AliasIdentifier] = field(default_factory=list)
    mfa: MfaMode = "off"
    software_token: bool = False
    triggers: TriggerConfigDict | None = None
    password: PasswordPolicy | PasswordPolicyDict | None = None
    email: Email | None = None
    tier: PoolTier = "essentials"
    deletion_protection: bool = False
    domain: str | None = None

    def __post_init__(self) -> None:
        # Normalize password from dict to PasswordPolicy so its validation errors
        # appear before other config validation errors
        if isinstance(self.password, dict):
            object.__setattr__(self, "password", PasswordPolicy(**self.password))

        if self.usernames and self.aliases:
            raise ValueError(
                "Cannot specify both 'usernames' and 'aliases'. "
                "Use 'usernames' for username-based sign-in or "
                "'aliases' for alias-based sign-in."
            )

        if self.mfa in ("on", "optional") and not self.software_token:
            raise ValueError(
                "MFA requires 'software_token=True'. "
                "Set 'software_token=True' to enable TOTP-based MFA."
            )

        if self.triggers:
            invalid_keys = set(self.triggers.keys()) - VALID_TRIGGER_NAMES
            if invalid_keys:
                raise ValueError(
                    f"Invalid trigger keys: {invalid_keys}. "
                    f"Valid keys: {sorted(VALID_TRIGGER_NAMES)}"
                )
            # Validate trigger values are correct types
            for trigger_name, handler in self.triggers.items():
                if isinstance(handler, (int, float, bool, list, tuple, set, bytes)):
                    raise TypeError(
                        f"Invalid handler type for trigger '{trigger_name}': "
                        f"expected str, FunctionConfig, FunctionConfigDict, or Function, "
                        f"got {type(handler).__name__}"
                    )

        if self.domain is not None:
            _validate_domain(self.domain)


def _validate_domain(domain: str) -> None:
    stripped = domain.strip()
    if not stripped:
        raise ValueError("Domain cannot be empty.")

    is_custom = "." in stripped
    if is_custom:
        labels = stripped.split(".")
        for label in labels:
            if not _HOSTNAME_LABEL_RE.match(label):
                raise ValueError(
                    f"Invalid custom domain '{domain}': "
                    f"each label must be 1-63 characters of letters, digits, or hyphens, "
                    f"and cannot start or end with a hyphen."
                )
    elif not _PREFIX_DOMAIN_RE.match(stripped):
        raise ValueError(
            f"Invalid prefix domain '{domain}': "
            f"must be 1-63 lowercase letters, digits, or hyphens, "
            f"and cannot start or end with a hyphen."
        )


class UserPoolCustomizationDict(TypedDict, total=False):
    user_pool: pulumi_aws.cognito.UserPoolArgs
    user_pool_domain: pulumi_aws.cognito.UserPoolDomainArgs
    acm_validated_domain: AcmValidatedDomainCustomizationDict


class UserPoolClientConfigDict(TypedDict, total=False):
    callback_urls: list[str]
    logout_urls: list[str]
    providers: list[Input[str]]
    generate_secret: bool


@dataclass(frozen=True, kw_only=True)
class UserPoolClientConfig:
    callback_urls: list[str] | None = None
    logout_urls: list[str] | None = None
    providers: list[Input[str]] | None = None
    generate_secret: bool = False


class UserPoolClientCustomizationDict(TypedDict, total=False):
    client: pulumi_aws.cognito.UserPoolClientArgs


@dataclass(frozen=True, kw_only=True)
class IdentityProviderConfig:
    provider_name: str
    provider_type: str
    details: dict[str, str]
    attributes: dict[str, str] | None = None


class IdentityProviderCustomizationDict(TypedDict, total=False):
    identity_provider: pulumi_aws.cognito.IdentityProviderArgs


# =========================================================================
# IdentityPool types
# =========================================================================


class IdentityPoolBindingDict(TypedDict):
    user_pool: UserPool | str
    client: UserPoolClient | str


@dataclass(frozen=True, kw_only=True)
class IdentityPoolBinding:
    user_pool: UserPool | str
    client: UserPoolClient | str


class IdentityPoolPermissionsDict(TypedDict, total=False):
    authenticated: list[AwsPermission]
    unauthenticated: list[AwsPermission]


@dataclass(frozen=True, kw_only=True)
class IdentityPoolPermissions:
    authenticated: list[AwsPermission] = field(default_factory=list)
    unauthenticated: list[AwsPermission] = field(default_factory=list)


class IdentityPoolConfigDict(TypedDict, total=False):
    user_pools: list[IdentityPoolBinding | IdentityPoolBindingDict]
    permissions: IdentityPoolPermissions | IdentityPoolPermissionsDict
    allow_unauthenticated: bool


@dataclass(frozen=True, kw_only=True)
class IdentityPoolConfig:
    user_pools: list[IdentityPoolBinding | IdentityPoolBindingDict]
    permissions: IdentityPoolPermissions | None = None
    allow_unauthenticated: bool = False

    def __post_init__(self) -> None:
        if not self.user_pools:
            raise ValueError("user_pools must contain at least one binding")

        # Normalize bindings from dicts to IdentityPoolBinding
        normalized_bindings: list[IdentityPoolBinding | IdentityPoolBindingDict] = []
        for binding in self.user_pools:
            if isinstance(binding, dict):
                normalized_bindings.append(IdentityPoolBinding(**binding))
            else:
                normalized_bindings.append(binding)
        object.__setattr__(self, "user_pools", normalized_bindings)

        # Normalize permissions from dict to IdentityPoolPermissions
        if isinstance(self.permissions, dict):
            object.__setattr__(self, "permissions", IdentityPoolPermissions(**self.permissions))

        # Validate: unauthenticated permissions require allow_unauthenticated
        if (
            self.permissions
            and self.permissions.unauthenticated
            and not self.allow_unauthenticated
        ):
            raise ValueError(
                "Unauthenticated permissions require 'allow_unauthenticated=True'. "
                "Set 'allow_unauthenticated=True' to grant permissions to "
                "unauthenticated identities."
            )

        # Detect duplicate (user_pool, client) pairs
        seen: set[tuple[object, object]] = set()
        for binding in self.user_pools:
            key = (binding.user_pool, binding.client)
            if key in seen:
                raise ValueError(
                    f"Duplicate binding: user_pool={binding.user_pool!r}, "
                    f"client={binding.client!r}. Each (user_pool, client) pair "
                    "must be unique."
                )
            seen.add(key)


class IdentityPoolCustomizationDict(TypedDict, total=False):
    identity_pool: pulumi_aws.cognito.IdentityPoolArgs
    authenticated_role: pulumi_aws.iam.RoleArgs
    unauthenticated_role: pulumi_aws.iam.RoleArgs
    authenticated_role_policy: pulumi_aws.iam.RolePolicyArgs
    unauthenticated_role_policy: pulumi_aws.iam.RolePolicyArgs
    roles_attachment: pulumi_aws.cognito.IdentityPoolRoleAttachmentArgs
