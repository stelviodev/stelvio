from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, TypedDict

if TYPE_CHECKING:
    import pulumi_aws
    from pulumi import Input

    from stelvio.aws.email import Email
    from stelvio.aws.function import Function, FunctionConfig, FunctionConfigDict

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
    post_authentication: TriggerHandler
    pre_authentication: TriggerHandler
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


@dataclass(frozen=True, kw_only=True)
class UserPoolConfig:
    usernames: list[SignInIdentifier] = field(default_factory=list)
    aliases: list[AliasIdentifier] = field(default_factory=list)
    mfa: MfaMode = "off"
    software_token: bool = False
    triggers: TriggerConfigDict | None = None
    password: PasswordPolicy | None = None
    email: Email | None = None
    tier: PoolTier = "essentials"
    deletion_protection: bool = False

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


class UserPoolCustomizationDict(TypedDict, total=False):
    user_pool: pulumi_aws.cognito.UserPoolArgs


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
