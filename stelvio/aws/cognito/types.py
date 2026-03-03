from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, TypedDict

if TYPE_CHECKING:
    from stelvio.aws.email import Email
    from stelvio.aws.function import Function, FunctionConfig, FunctionConfigDict

type SignInIdentifier = Literal["email", "phone"]
type AliasIdentifier = Literal["email", "phone", "preferred_username"]
type MfaMode = Literal["off", "optional", "on"]
type PoolTier = Literal["lite", "essentials", "plus"]
type IdentityProviderType = Literal["google", "facebook", "apple", "amazon", "oidc", "saml"]
type TriggerHandler = str | FunctionConfig | FunctionConfigDict | Function

TRIGGER_CONFIG_MAP: dict[str, str] = {
    "pre_sign_up": "pre_sign_up",
    "post_confirmation": "post_confirmation",
    "pre_authentication": "pre_authentication",
    "post_authentication": "post_authentication",
    "pre_token_generation": "pre_token_generation",
    "user_migration": "user_migration",
    "define_auth_challenge": "define_auth_challenge",
    "create_auth_challenge": "create_auth_challenge",
    "verify_auth_challenge_response": "verify_auth_challenge_response",
    "custom_message": "custom_message",
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
            invalid_keys = set(self.triggers.keys()) - set(TRIGGER_CONFIG_MAP.keys())
            if invalid_keys:
                raise ValueError(
                    f"Invalid trigger keys: {invalid_keys}. "
                    f"Valid keys: {sorted(TRIGGER_CONFIG_MAP.keys())}"
                )

        # Normalize password from dict to PasswordPolicy
        if isinstance(self.password, dict):
            object.__setattr__(self, "password", PasswordPolicy(**self.password))


class UserPoolCustomizationDict(TypedDict, total=False):
    user_pool: Any


class UserPoolClientCustomizationDict(TypedDict, total=False):
    client: Any


class IdentityProviderCustomizationDict(TypedDict, total=False):
    identity_provider: Any
