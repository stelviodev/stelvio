import pulumi
import pytest

from stelvio.aws.cognito.types import PasswordPolicy, UserPoolConfig
from stelvio.aws.cognito.user_pool import UserPool
from stelvio.aws.email import Email

from ...conftest import TP


@pulumi.runtime.test
def test_user_pool_basic_creation(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])

    def check_resources(_):
        user_pool = pulumi_mocks.assert_user_pool_created(f"{TP}users")
        assert user_pool.typ == "aws:cognito/userPool:UserPool"

    return pool.id.apply(check_resources)


@pulumi.runtime.test
@pytest.mark.parametrize(
    "factory",
    [
        lambda: UserPool("typed", config=UserPoolConfig(usernames=["email"])),
        lambda: UserPool("opts", usernames=["email"]),
    ],
)
def test_user_pool_config_and_opts_forms_create_pool(pulumi_mocks, factory):
    pool = factory()

    def check_resources(_):
        pools = pulumi_mocks.created_user_pools()
        assert len(pools) == 1
        assert pools[0].typ == "aws:cognito/userPool:UserPool"

    return pool.arn.apply(check_resources)


def test_user_pool_rejects_config_and_opts_together():
    with pytest.raises(ValueError, match="cannot combine 'config' parameter"):
        UserPool("users", config=UserPoolConfig(usernames=["email"]), aliases=["email"])


def test_user_pool_rejects_usernames_and_aliases_together():
    with pytest.raises(ValueError, match="mutually exclusive"):
        UserPool("users", usernames=["email"], aliases=["preferred_username"])


def test_user_pool_mfa_validation_requires_software_token():
    with pytest.raises(ValueError, match="requires software_token=True"):
        UserPool("users", mfa="on", software_token=False)


@pulumi.runtime.test
def test_user_pool_password_policy_is_passed_to_resource(pulumi_mocks):
    pool = UserPool(
        "users",
        password=PasswordPolicy(
            min_length=12,
            require_lowercase=True,
            require_uppercase=True,
            require_numbers=True,
            require_symbols=False,
            temporary_password_validity_days=14,
        ),
    )

    def check_resources(_):
        user_pool = pulumi_mocks.assert_user_pool_created(f"{TP}users")
        password_policy = user_pool.inputs["passwordPolicy"]
        assert password_policy == {
            "minimumLength": 12,
            "requireLowercase": True,
            "requireUppercase": True,
            "requireNumbers": True,
            "requireSymbols": False,
            "temporaryPasswordValidityDays": 14,
        }

    return pool.id.apply(check_resources)


@pulumi.runtime.test
def test_user_pool_default_password_policy_is_applied(pulumi_mocks):
    pool = UserPool("users")

    def check_resources(_):
        user_pool = pulumi_mocks.assert_user_pool_created(f"{TP}users")
        password_policy = user_pool.inputs["passwordPolicy"]
        assert password_policy == {
            "minimumLength": 8,
            "requireLowercase": True,
            "requireUppercase": True,
            "requireNumbers": True,
            "requireSymbols": True,
            "temporaryPasswordValidityDays": 7,
        }

    return pool.id.apply(check_resources)


@pulumi.runtime.test
def test_user_pool_email_configuration_with_email_component(pulumi_mocks):
    sender = Email("sender", "noreply@example.com", dmarc=None)
    pool = UserPool("users", usernames=["email"], email=sender)

    def check_resources(_):
        user_pool = pulumi_mocks.assert_user_pool_created(f"{TP}users")
        email_config = user_pool.inputs["emailConfiguration"]
        assert email_config["emailSendingAccount"] == "DEVELOPER"

    return pool.id.apply(check_resources)


@pulumi.runtime.test
def test_user_pool_auto_verified_attributes_for_email_sign_in(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])

    def check_resources(_):
        user_pool = pulumi_mocks.assert_user_pool_created(f"{TP}users")
        assert user_pool.inputs["autoVerifiedAttributes"] == ["email"]

    return pool.id.apply(check_resources)


@pulumi.runtime.test
@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("off", "OFF"),
        ("optional", "OPTIONAL"),
        ("on", "ON"),
    ],
)
def test_user_pool_mfa_mode_mapping(pulumi_mocks, mode, expected):
    kwargs = {"mfa": mode}
    if mode != "off":
        kwargs["software_token"] = True
    pool = UserPool("users", **kwargs)

    def check_resources(_):
        user_pool = pulumi_mocks.assert_user_pool_created(f"{TP}users")
        assert user_pool.inputs["mfaConfiguration"] == expected

    return pool.id.apply(check_resources)


@pulumi.runtime.test
def test_user_pool_deletion_protection_active_when_enabled(pulumi_mocks):
    pool = UserPool("users", deletion_protection=True)

    def check_resources(_):
        user_pool = pulumi_mocks.assert_user_pool_created(f"{TP}users")
        assert user_pool.inputs["deletionProtection"] == "ACTIVE"

    return pool.id.apply(check_resources)


@pulumi.runtime.test
@pytest.mark.parametrize(
    ("tier", "expected"),
    [
        ("lite", "LITE"),
        ("essentials", "ESSENTIALS"),
        ("plus", "PLUS"),
    ],
)
def test_user_pool_tier_is_mapped_to_aws(pulumi_mocks, tier, expected):
    pool = UserPool("users", tier=tier)

    def check_resources(_):
        user_pool = pulumi_mocks.assert_user_pool_created(f"{TP}users")
        assert user_pool.inputs["userPoolTier"] == expected

    return pool.id.apply(check_resources)


@pulumi.runtime.test
def test_user_pool_customization_overrides_args(pulumi_mocks):
    pool = UserPool(
        "users",
        mfa="off",
        customize={
            "user_pool": {
                "mfa_configuration": "ON",
            }
        },
    )

    def check_resources(_):
        user_pool = pulumi_mocks.assert_user_pool_created(f"{TP}users")
        assert user_pool.inputs["mfaConfiguration"] == "ON"

    return pool.id.apply(check_resources)
