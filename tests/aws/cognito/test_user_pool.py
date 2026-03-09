import pulumi
import pytest

from stelvio.aws.cognito.types import (
    PasswordPolicy,
    PasswordPolicyDict,
    UserPoolConfig,
    UserPoolConfigDict,
)
from stelvio.aws.cognito.user_pool import UserPool

from ...conftest import TP
from ...test_utils import assert_config_dict_matches_dataclass
from ..pulumi_mocks import ACCOUNT_ID, DEFAULT_REGION, tn

POOL_ARN_TEMPLATE = f"arn:aws:cognito-idp:{DEFAULT_REGION}:{ACCOUNT_ID}:userpool/{{pool_id}}"


# =========================================================================
# Config validation tests (no Pulumi mocks needed)
# =========================================================================


def test_config_vs_opts_rejection():
    with pytest.raises(ValueError, match="cannot combine"):
        UserPool._parse_config(
            config=UserPoolConfig(usernames=["email"]),
            opts={"usernames": ["email"]},
        )


def test_usernames_and_aliases_rejection():
    with pytest.raises(ValueError, match="Cannot specify both"):
        UserPoolConfig(usernames=["email"], aliases=["phone"])


def test_mfa_without_software_token_rejection():
    with pytest.raises(ValueError, match="software_token"):
        UserPoolConfig(mfa="on")


def test_mfa_optional_without_software_token_rejection():
    with pytest.raises(ValueError, match="software_token"):
        UserPoolConfig(mfa="optional")


def test_mfa_on_with_software_token():
    config = UserPoolConfig(mfa="on", software_token=True)
    assert config.mfa == "on"
    assert config.software_token is True


def test_config_from_dict():
    config = UserPool._parse_config(
        config={"usernames": ["email"]},
        opts={},
    )
    assert config.usernames == ["email"]
    assert isinstance(config, UserPoolConfig)


def test_config_from_dataclass():
    original = UserPoolConfig(usernames=["email"])
    config = UserPool._parse_config(config=original, opts={})
    assert config is original


def test_config_from_opts():
    config = UserPool._parse_config(
        config=None,
        opts={"usernames": ["email"], "mfa": "off"},
    )
    assert config.usernames == ["email"]
    assert config.mfa == "off"


def test_password_normalization_from_dict():
    config = UserPoolConfig(password={"min_length": 12, "require_symbols": False})
    assert isinstance(config.password, PasswordPolicy)
    assert config.password.min_length == 12
    assert config.password.require_symbols is False


def test_password_defaults():
    policy = PasswordPolicy()
    assert policy.min_length == 8
    assert policy.require_lowercase is True
    assert policy.require_uppercase is True
    assert policy.require_numbers is True
    assert policy.require_symbols is True
    assert policy.temporary_password_validity_days == 7


def test_default_config_values():
    config = UserPoolConfig()
    assert config.usernames == []
    assert config.aliases == []
    assert config.mfa == "off"
    assert config.software_token is False
    assert config.triggers is None
    assert config.password is None
    assert config.email is None
    assert config.tier == "essentials"
    assert config.deletion_protection is False


def test_user_pool_config_property():
    config = UserPoolConfig(usernames=["email"], mfa="optional", software_token=True)
    pool = UserPool("users", config=config)
    assert pool.config is config


# =========================================================================
# Resource creation tests (require Pulumi mocks)
# =========================================================================


@pulumi.runtime.test
def test_basic_user_pool_creation(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])

    def check(_):
        pools = pulumi_mocks.created_user_pools()
        assert len(pools) == 1
        assert pools[0].typ == "aws:cognito/userPool:UserPool"

    pool.arn.apply(check)


@pulumi.runtime.test
def test_user_pool_with_username_attributes(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])

    def check(_):
        mock = pulumi_mocks.assert_user_pool_created(TP + "users")
        assert mock.inputs["usernameAttributes"] == ["email"]
        assert mock.inputs.get("aliasAttributes") is None

    pool.arn.apply(check)


@pulumi.runtime.test
def test_user_pool_with_alias_attributes(pulumi_mocks):
    pool = UserPool("users", aliases=["email", "preferred_username"])

    def check(_):
        mock = pulumi_mocks.assert_user_pool_created(TP + "users")
        assert mock.inputs["aliasAttributes"] == ["email", "preferred_username"]
        assert mock.inputs.get("usernameAttributes") is None

    pool.arn.apply(check)


@pulumi.runtime.test
def test_user_pool_auto_verified_email(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])

    def check(_):
        mock = pulumi_mocks.assert_user_pool_created(TP + "users")
        assert "email" in mock.inputs["autoVerifiedAttributes"]

    pool.arn.apply(check)


@pulumi.runtime.test
def test_user_pool_auto_verified_phone(pulumi_mocks):
    pool = UserPool("users", usernames=["phone"])

    def check(_):
        mock = pulumi_mocks.assert_user_pool_created(TP + "users")
        assert "phone_number" in mock.inputs["autoVerifiedAttributes"]

    pool.arn.apply(check)


@pulumi.runtime.test
def test_user_pool_auto_verified_both(pulumi_mocks):
    pool = UserPool("users", aliases=["email", "phone"])

    def check(_):
        mock = pulumi_mocks.assert_user_pool_created(TP + "users")
        assert "email" in mock.inputs["autoVerifiedAttributes"]
        assert "phone_number" in mock.inputs["autoVerifiedAttributes"]

    pool.arn.apply(check)


@pulumi.runtime.test
def test_user_pool_no_auto_verified_when_empty(pulumi_mocks):
    pool = UserPool("users")

    def check(_):
        mock = pulumi_mocks.assert_user_pool_created(TP + "users")
        assert mock.inputs.get("autoVerifiedAttributes") is None

    pool.arn.apply(check)


@pulumi.runtime.test
def test_user_pool_alias_phone_auto_verified(pulumi_mocks):
    """Phone in aliases (not just usernames) should be auto-verified."""
    pool = UserPool("users", aliases=["phone"])

    def check(_):
        mock = pulumi_mocks.assert_user_pool_created(TP + "users")
        assert "phone_number" in mock.inputs["autoVerifiedAttributes"]

    pool.arn.apply(check)


@pulumi.runtime.test
def test_user_pool_alias_preferred_username_no_auto_verify(pulumi_mocks):
    """preferred_username in aliases should NOT be auto-verified."""
    pool = UserPool("users", aliases=["preferred_username"])

    def check(_):
        mock = pulumi_mocks.assert_user_pool_created(TP + "users")
        assert mock.inputs.get("autoVerifiedAttributes") is None

    pool.arn.apply(check)


@pulumi.runtime.test
def test_password_policy_args(pulumi_mocks):
    pool = UserPool(
        "users",
        usernames=["email"],
        password=PasswordPolicy(
            min_length=12,
            require_symbols=False,
            temporary_password_validity_days=3,
        ),
    )

    def check(_):
        mock = pulumi_mocks.assert_user_pool_created(TP + "users")
        pw = mock.inputs["passwordPolicy"]
        assert pw["minimumLength"] == 12
        assert pw["requireSymbols"] is False
        assert pw["requireLowercase"] is True
        assert pw["requireUppercase"] is True
        assert pw["requireNumbers"] is True
        assert pw["temporaryPasswordValidityDays"] == 3

    pool.arn.apply(check)


@pulumi.runtime.test
def test_default_password_not_set(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])

    def check(_):
        mock = pulumi_mocks.assert_user_pool_created(TP + "users")
        assert mock.inputs.get("passwordPolicy") is None

    pool.arn.apply(check)


@pulumi.runtime.test
def test_mfa_off(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])

    def check(_):
        mock = pulumi_mocks.assert_user_pool_created(TP + "users")
        assert mock.inputs["mfaConfiguration"] == "OFF"

    pool.arn.apply(check)


@pulumi.runtime.test
def test_mfa_optional(pulumi_mocks):
    pool = UserPool("users", usernames=["email"], mfa="optional", software_token=True)

    def check(_):
        mock = pulumi_mocks.assert_user_pool_created(TP + "users")
        assert mock.inputs["mfaConfiguration"] == "OPTIONAL"
        assert mock.inputs["softwareTokenMfaConfiguration"]["enabled"] is True

    pool.arn.apply(check)


@pulumi.runtime.test
def test_mfa_on(pulumi_mocks):
    pool = UserPool("users", usernames=["email"], mfa="on", software_token=True)

    def check(_):
        mock = pulumi_mocks.assert_user_pool_created(TP + "users")
        assert mock.inputs["mfaConfiguration"] == "ON"
        assert mock.inputs["softwareTokenMfaConfiguration"]["enabled"] is True

    pool.arn.apply(check)


@pulumi.runtime.test
def test_deletion_protection_enabled(pulumi_mocks):
    pool = UserPool("users", usernames=["email"], deletion_protection=True)

    def check(_):
        mock = pulumi_mocks.assert_user_pool_created(TP + "users")
        assert mock.inputs["deletionProtection"] == "ACTIVE"

    pool.arn.apply(check)


@pulumi.runtime.test
def test_deletion_protection_disabled_by_default(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])

    def check(_):
        mock = pulumi_mocks.assert_user_pool_created(TP + "users")
        assert mock.inputs["deletionProtection"] == "INACTIVE"

    pool.arn.apply(check)


@pulumi.runtime.test
def test_tier_essentials_default(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])

    def check(_):
        mock = pulumi_mocks.assert_user_pool_created(TP + "users")
        assert mock.inputs["userPoolTier"] == "ESSENTIALS"

    pool.arn.apply(check)


@pulumi.runtime.test
def test_tier_lite(pulumi_mocks):
    pool = UserPool("users", usernames=["email"], tier="lite")

    def check(_):
        mock = pulumi_mocks.assert_user_pool_created(TP + "users")
        assert mock.inputs["userPoolTier"] == "LITE"

    pool.arn.apply(check)


@pulumi.runtime.test
def test_tier_plus(pulumi_mocks):
    pool = UserPool("users", usernames=["email"], tier="plus")

    def check(_):
        mock = pulumi_mocks.assert_user_pool_created(TP + "users")
        assert mock.inputs["userPoolTier"] == "PLUS"

    pool.arn.apply(check)


@pulumi.runtime.test
def test_user_pool_properties(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])

    def check(args):
        pool_id, arn, name = args
        expected_resource_name = TP + "users"
        # pool.id resolves to the resource_id from tid(), not output_props["id"]
        assert pool_id == expected_resource_name + "-test-id"
        assert "cognito-idp" in arn
        assert "userpool" in arn
        assert name == tn(expected_resource_name)

    pulumi.Output.all(pool.id, pool.arn, pool.name_in_aws).apply(check)


@pulumi.runtime.test
def test_customization_overrides(pulumi_mocks):
    pool = UserPool(
        "users",
        usernames=["email"],
        customize={"user_pool": {"name": "custom-pool-name"}},
    )

    def check(_):
        mock = pulumi_mocks.assert_user_pool_created(TP + "users")
        assert mock.inputs["name"] == "custom-pool-name"

    pool.arn.apply(check)


@pulumi.runtime.test
def test_no_triggers_by_default(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])

    def check(_):
        assert pool.resources.trigger_functions == {}
        assert pool.resources.trigger_permissions == {}
        mock = pulumi_mocks.assert_user_pool_created(TP + "users")
        assert mock.inputs.get("lambdaConfig") is None

    pool.arn.apply(check)


@pulumi.runtime.test
def test_config_parameter_style(pulumi_mocks):
    pool = UserPool(
        "users",
        config=UserPoolConfig(usernames=["email"], tier="plus"),
    )

    def check(_):
        mock = pulumi_mocks.assert_user_pool_created(TP + "users")
        assert mock.inputs["usernameAttributes"] == ["email"]
        assert mock.inputs["userPoolTier"] == "PLUS"

    pool.arn.apply(check)


# =========================================================================
# Config parity tests
# =========================================================================


def test_password_policy_dict_matches_dataclass():
    assert_config_dict_matches_dataclass(PasswordPolicy, PasswordPolicyDict)


def test_user_pool_config_dict_matches_dataclass():
    assert_config_dict_matches_dataclass(UserPoolConfig, UserPoolConfigDict)


# =========================================================================
# Validation edge case tests
# =========================================================================


def test_invalid_trigger_keys_rejected():
    with pytest.raises(ValueError, match="Invalid trigger keys"):
        UserPoolConfig(triggers={"invalid_key": "handler"})


@pytest.mark.parametrize(
    "invalid_handler",
    [
        123,  # int
        45.67,  # float
        True,  # bool
        ["handler"],  # list
        ("handler",),  # tuple
        {"handler"},  # set
        b"handler",  # bytes
    ],
)
def test_invalid_trigger_handler_types_rejected(invalid_handler):
    """Reject clearly wrong trigger handler types."""
    with pytest.raises(TypeError, match="Invalid handler type for trigger"):
        UserPoolConfig(triggers={"pre_sign_up": invalid_handler})


def test_parse_config_invalid_type():
    with pytest.raises(TypeError, match="Invalid config type"):
        UserPool._parse_config(config=42, opts={})
