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
from ..conftest import assert_hash_truncated
from ..pulumi_mocks import ACCOUNT_ID, DEFAULT_REGION, R, tn

SES_IDENTITY_ARN = f"arn:aws:ses:{DEFAULT_REGION}:{ACCOUNT_ID}:identity/noreply@example.com"

# =========================================================================
# Config validation tests (no Pulumi mocks needed)
# =========================================================================


def test_config_vs_opts_rejection():
    with pytest.raises(ValueError, match="cannot combine"):
        UserPool("users", config=UserPoolConfig(usernames=["email"]), usernames=["email"])


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
    assert config.software_token is False
    assert config.triggers is None
    assert config.password is None
    assert config.email is None


def test_user_pool_config_property():
    config = UserPoolConfig(usernames=["email"], mfa="optional", software_token=True)
    pool = UserPool("users", config=config)
    assert pool.config is config


# =========================================================================
# Resource creation tests (require Pulumi mocks)
# =========================================================================


def test_basic_user_pool_creation(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])

    @pulumi.runtime.test
    def deploy():
        return pool.resources

    deploy()

    pulumi_mocks.assert_res(
        "users",
        R.USER_POOL,
        {
            "usernameAttributes": ["email"],
            "autoVerifiedAttributes": ["email"],
            "mfaConfiguration": "OFF",
            "deletionProtection": "INACTIVE",
            "userPoolTier": "ESSENTIALS",
        },
    )
    pulumi_mocks.assert_res_counts({R.USER_POOL: 1})


def test_user_pool_with_username_attributes(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])

    @pulumi.runtime.test
    def deploy():
        return pool.resources

    deploy()

    pulumi_mocks.assert_res(
        "users",
        R.USER_POOL,
        {"usernameAttributes": ["email"], "aliasAttributes": None},
        partial=True,
    )


def test_user_pool_with_alias_attributes(pulumi_mocks):
    pool = UserPool("users", aliases=["email", "preferred_username"])

    @pulumi.runtime.test
    def deploy():
        return pool.resources

    deploy()

    pulumi_mocks.assert_res(
        "users",
        R.USER_POOL,
        {"aliasAttributes": ["email", "preferred_username"], "usernameAttributes": None},
        partial=True,
    )


def test_user_pool_auto_verified_email(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])

    @pulumi.runtime.test
    def deploy():
        return pool.resources

    deploy()

    pulumi_mocks.assert_res(
        "users", R.USER_POOL, {"autoVerifiedAttributes": ["email"]}, partial=True
    )


def test_user_pool_auto_verified_phone(pulumi_mocks):
    pool = UserPool("users", usernames=["phone"])

    @pulumi.runtime.test
    def deploy():
        return pool.resources

    deploy()

    pulumi_mocks.assert_res(
        "users", R.USER_POOL, {"autoVerifiedAttributes": ["phone_number"]}, partial=True
    )


def test_user_pool_auto_verified_both(pulumi_mocks):
    pool = UserPool("users", aliases=["email", "phone"])

    @pulumi.runtime.test
    def deploy():
        return pool.resources

    deploy()

    pulumi_mocks.assert_res(
        "users", R.USER_POOL, {"autoVerifiedAttributes": ["email", "phone_number"]}, partial=True
    )


def test_user_pool_no_auto_verified_when_empty(pulumi_mocks):
    pool = UserPool("users")

    @pulumi.runtime.test
    def deploy():
        return pool.resources

    deploy()

    pulumi_mocks.assert_res("users", R.USER_POOL, {"autoVerifiedAttributes": None}, partial=True)


def test_user_pool_alias_phone_auto_verified(pulumi_mocks):
    """Phone in aliases (not just usernames) should be auto-verified."""
    pool = UserPool("users", aliases=["phone"])

    @pulumi.runtime.test
    def deploy():
        return pool.resources

    deploy()

    pulumi_mocks.assert_res(
        "users", R.USER_POOL, {"autoVerifiedAttributes": ["phone_number"]}, partial=True
    )


def test_user_pool_alias_preferred_username_no_auto_verify(pulumi_mocks):
    """preferred_username in aliases should NOT be auto-verified."""
    pool = UserPool("users", aliases=["preferred_username"])

    @pulumi.runtime.test
    def deploy():
        return pool.resources

    deploy()

    pulumi_mocks.assert_res("users", R.USER_POOL, {"autoVerifiedAttributes": None}, partial=True)


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

    @pulumi.runtime.test
    def deploy():
        return pool.resources

    deploy()

    pulumi_mocks.assert_res(
        "users",
        R.USER_POOL,
        {
            "passwordPolicy": {
                "minimumLength": 12,
                "requireLowercase": True,
                "requireUppercase": True,
                "requireNumbers": True,
                "requireSymbols": False,
                "temporaryPasswordValidityDays": 3,
            }
        },
        partial=True,
    )


def test_default_password_not_set(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])

    @pulumi.runtime.test
    def deploy():
        return pool.resources

    deploy()

    pulumi_mocks.assert_res("users", R.USER_POOL, {"passwordPolicy": None}, partial=True)


def test_mfa_off(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])

    @pulumi.runtime.test
    def deploy():
        return pool.resources

    deploy()

    pulumi_mocks.assert_res("users", R.USER_POOL, {"mfaConfiguration": "OFF"}, partial=True)


def test_mfa_optional(pulumi_mocks):
    pool = UserPool("users", usernames=["email"], mfa="optional", software_token=True)

    @pulumi.runtime.test
    def deploy():
        return pool.resources

    deploy()

    pulumi_mocks.assert_res(
        "users",
        R.USER_POOL,
        {"mfaConfiguration": "OPTIONAL", "softwareTokenMfaConfiguration": {"enabled": True}},
        partial=True,
    )


def test_mfa_on(pulumi_mocks):
    pool = UserPool("users", usernames=["email"], mfa="on", software_token=True)

    @pulumi.runtime.test
    def deploy():
        return pool.resources

    deploy()

    pulumi_mocks.assert_res(
        "users",
        R.USER_POOL,
        {"mfaConfiguration": "ON", "softwareTokenMfaConfiguration": {"enabled": True}},
        partial=True,
    )


def test_deletion_protection_enabled(pulumi_mocks):
    pool = UserPool("users", usernames=["email"], deletion_protection=True)

    @pulumi.runtime.test
    def deploy():
        return pool.resources

    deploy()

    pulumi_mocks.assert_res("users", R.USER_POOL, {"deletionProtection": "ACTIVE"}, partial=True)


def test_deletion_protection_disabled_by_default(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])

    @pulumi.runtime.test
    def deploy():
        return pool.resources

    deploy()

    pulumi_mocks.assert_res("users", R.USER_POOL, {"deletionProtection": "INACTIVE"}, partial=True)


def test_tier_essentials_default(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])

    @pulumi.runtime.test
    def deploy():
        return pool.resources

    deploy()

    pulumi_mocks.assert_res("users", R.USER_POOL, {"userPoolTier": "ESSENTIALS"}, partial=True)


def test_tier_lite(pulumi_mocks):
    pool = UserPool("users", usernames=["email"], tier="lite")

    @pulumi.runtime.test
    def deploy():
        return pool.resources

    deploy()

    pulumi_mocks.assert_res("users", R.USER_POOL, {"userPoolTier": "LITE"}, partial=True)


def test_tier_plus(pulumi_mocks):
    pool = UserPool("users", usernames=["email"], tier="plus")

    @pulumi.runtime.test
    def deploy():
        return pool.resources

    deploy()

    pulumi_mocks.assert_res("users", R.USER_POOL, {"userPoolTier": "PLUS"}, partial=True)


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


def test_customization_overrides(pulumi_mocks):
    pool = UserPool(
        "users",
        usernames=["email"],
        customize={"user_pool": {"name": "custom-pool-name"}},
    )

    @pulumi.runtime.test
    def deploy():
        return pool.resources

    deploy()

    pulumi_mocks.assert_res("users", R.USER_POOL, {"name": "custom-pool-name"}, partial=True)


def test_no_triggers_by_default(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])

    @pulumi.runtime.test
    def deploy():
        return pool.resources

    deploy()

    assert pool.resources.trigger_functions == {}
    assert pool.resources.trigger_permissions == {}
    pulumi_mocks.assert_res("users", R.USER_POOL, {"lambdaConfig": None}, partial=True)


def test_config_parameter_style(pulumi_mocks):
    pool = UserPool(
        "users",
        config=UserPoolConfig(usernames=["email"], tier="plus"),
    )

    @pulumi.runtime.test
    def deploy():
        return pool.resources

    deploy()

    pulumi_mocks.assert_res(
        "users",
        R.USER_POOL,
        {"usernameAttributes": ["email"], "userPoolTier": "PLUS"},
        partial=True,
    )


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


# =========================================================================
# SES email integration tests
# =========================================================================


def test_user_pool_with_ses_email(pulumi_mocks):
    from stelvio.aws.email import Email

    email = Email("auth-email", "noreply@example.com", dmarc=None)
    pool = UserPool("users", usernames=["email"], email=email)

    @pulumi.runtime.test
    def deploy():
        return pool.resources

    deploy()

    pulumi_mocks.assert_res(
        "users",
        R.USER_POOL,
        {
            "emailConfiguration": {
                "emailSendingAccount": "DEVELOPER",
                "sourceArn": SES_IDENTITY_ARN,
                "fromEmailAddress": "noreply@example.com",
            }
        },
        partial=True,
    )


# =========================================================================
# Fully-loaded config test
# =========================================================================


def test_fully_loaded_config(pulumi_mocks, project_cwd):
    from stelvio.aws.email import Email

    email = Email("full-email", "noreply@example.com", dmarc=None)
    pool = UserPool(
        "full",
        usernames=["email"],
        mfa="optional",
        software_token=True,
        password=PasswordPolicy(min_length=12),
        email=email,
        tier="plus",
        deletion_protection=True,
        triggers={"pre_sign_up": "functions/auth/validate.handler"},
    )

    @pulumi.runtime.test
    def deploy():
        return pool.resources

    deploy()

    pulumi_mocks.assert_res(
        "full",
        R.USER_POOL,
        {
            "usernameAttributes": ["email"],
            "autoVerifiedAttributes": ["email"],
            "mfaConfiguration": "OPTIONAL",
            "softwareTokenMfaConfiguration": {"enabled": True},
            "passwordPolicy": {
                "minimumLength": 12,
                "requireLowercase": True,
                "requireUppercase": True,
                "requireNumbers": True,
                "requireSymbols": True,
                "temporaryPasswordValidityDays": 7,
            },
            "emailConfiguration": {
                "emailSendingAccount": "DEVELOPER",
                "sourceArn": SES_IDENTITY_ARN,
                "fromEmailAddress": "noreply@example.com",
            },
            "lambdaConfig": {
                "preSignUp": f"arn:aws:lambda:{DEFAULT_REGION}:{ACCOUNT_ID}:function:"
                f"{tn(TP + 'full-trigger-pre_sign_up')}"
            },
            "deletionProtection": "ACTIVE",
            "userPoolTier": "PLUS",
        },
    )


def test_user_pool_lets_pulumi_name_it(pulumi_mocks):
    @pulumi.runtime.test
    def deploy():
        return UserPool("users", usernames=["email"]).resources

    deploy()

    [pool] = pulumi_mocks.created(R.USER_POOL, f"{TP}users")
    assert "name" not in pool.inputs


def test_user_pool_long_name_truncates_logical_name(pulumi_mocks):
    @pulumi.runtime.test
    def deploy():
        return UserPool("u" * 150, usernames=["email"]).resources

    deploy()

    [pool] = pulumi_mocks.created(R.USER_POOL)
    assert_hash_truncated(pool.name, 120)  # Cognito 128 minus the 8-char Pulumi suffix
