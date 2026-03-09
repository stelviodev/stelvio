"""Integration tests for Cognito UserPool component.

Tests deploy real AWS Cognito resources and verify properties via boto3.
"""

import json

import pytest
from botocore.exceptions import ClientError

from stelvio.aws.cognito import PasswordPolicy, UserPool
from stelvio.aws.dynamo_db import DynamoTable
from stelvio.aws.function import FunctionConfig

from .assert_helpers import (
    admin_delete_cognito_user,
    assert_cognito_identity_provider,
    assert_cognito_tags,
    assert_cognito_user_pool,
    assert_cognito_user_pool_client,
    assert_lambda_function,
    assert_lambda_tags,
    disable_cognito_deletion_protection,
    poll_dynamo_items,
    sign_up_cognito_user,
)

pytestmark = pytest.mark.integration


# --- UserPool Property Tests ---


def test_user_pool_basic(stelvio_env):
    def infra():
        UserPool("auth", usernames=["email"])

    outputs = stelvio_env.deploy(infra)

    assert_cognito_user_pool(
        outputs["user_pool_auth_id"],
        username_attributes=["email"],
        auto_verified_attributes=["email"],
    )


def test_user_pool_aliases(stelvio_env):
    def infra():
        UserPool("auth", aliases=["email", "preferred_username"])

    outputs = stelvio_env.deploy(infra)

    assert_cognito_user_pool(
        outputs["user_pool_auth_id"],
        alias_attributes=["email", "preferred_username"],
    )


def test_user_pool_password_policy(stelvio_env):
    def infra():
        UserPool(
            "auth",
            usernames=["email"],
            password=PasswordPolicy(min_length=12, require_symbols=False),
        )

    outputs = stelvio_env.deploy(infra)

    assert_cognito_user_pool(
        outputs["user_pool_auth_id"],
        password_policy={
            "MinimumLength": 12,
            "RequireSymbols": False,
            "RequireLowercase": True,
            "RequireUppercase": True,
            "RequireNumbers": True,
        },
    )


def test_user_pool_mfa(stelvio_env):
    def infra():
        UserPool("auth", usernames=["email"], mfa="optional", software_token=True)

    outputs = stelvio_env.deploy(infra)

    assert_cognito_user_pool(
        outputs["user_pool_auth_id"],
        mfa_configuration="OPTIONAL",
    )


def test_user_pool_deletion_protection(stelvio_env):
    def infra():
        UserPool("auth", usernames=["email"], deletion_protection=True)

    outputs = stelvio_env.deploy(infra)
    pool_id = outputs["user_pool_auth_id"]

    try:
        assert_cognito_user_pool(pool_id, deletion_protection="ACTIVE")
    finally:
        # Must disable protection before destroy can succeed
        disable_cognito_deletion_protection(pool_id)


@pytest.mark.parametrize(
    ("tier", "expected_tier"),
    [
        ("lite", "LITE"),
        ("essentials", "ESSENTIALS"),
        ("plus", "PLUS"),
    ],
)
def test_user_pool_tier(stelvio_env, tier, expected_tier):
    def infra():
        UserPool("auth", usernames=["email"], tier=tier)

    outputs = stelvio_env.deploy(infra)

    assert_cognito_user_pool(
        outputs["user_pool_auth_id"],
        tier=expected_tier,
    )


def test_user_pool_tags(stelvio_env):
    def infra():
        UserPool("auth", usernames=["email"], tags={"Team": "platform"})

    outputs = stelvio_env.deploy(infra)

    assert_cognito_tags(outputs["user_pool_auth_arn"], {"Team": "platform"})


# --- Client Tests ---


def test_user_pool_client_basic(stelvio_env):
    def infra():
        pool = UserPool("auth", usernames=["email"])
        pool.add_client("web")

    outputs = stelvio_env.deploy(infra)

    pool_id = outputs["user_pool_auth_id"]
    client_id = outputs["user_pool_client_auth-web_id"]

    assert_cognito_user_pool_client(
        pool_id,
        client_id,
        generate_secret=False,
        supported_identity_providers=["COGNITO"],
    )


def test_user_pool_client_oauth(stelvio_env):
    def infra():
        pool = UserPool("auth", usernames=["email"])
        pool.add_client(
            "web",
            callback_urls=["https://example.com/callback"],
            logout_urls=["https://example.com/logout"],
        )

    outputs = stelvio_env.deploy(infra)

    pool_id = outputs["user_pool_auth_id"]
    client_id = outputs["user_pool_client_auth-web_id"]

    assert_cognito_user_pool_client(
        pool_id,
        client_id,
        callback_urls=["https://example.com/callback"],
        logout_urls=["https://example.com/logout"],
        allowed_oauth_flows=["code"],
        allowed_oauth_scopes=["openid", "email", "profile"],
    )


def test_user_pool_client_secret(stelvio_env):
    def infra():
        pool = UserPool("auth", usernames=["email"])
        pool.add_client("server", generate_secret=True)

    outputs = stelvio_env.deploy(infra)

    pool_id = outputs["user_pool_auth_id"]
    client_id = outputs["user_pool_client_auth-server_id"]

    assert_cognito_user_pool_client(
        pool_id,
        client_id,
        generate_secret=True,
    )


def test_user_pool_multiple_clients(stelvio_env):
    def infra():
        pool = UserPool("auth", usernames=["email"])
        pool.add_client("web")
        pool.add_client(
            "mobile",
            callback_urls=["myapp://callback"],
            logout_urls=["myapp://logout"],
        )

    outputs = stelvio_env.deploy(infra)

    pool_id = outputs["user_pool_auth_id"]

    # Web client — no OAuth
    assert_cognito_user_pool_client(
        pool_id,
        outputs["user_pool_client_auth-web_id"],
        generate_secret=False,
    )

    # Mobile client — OAuth configured
    assert_cognito_user_pool_client(
        pool_id,
        outputs["user_pool_client_auth-mobile_id"],
        callback_urls=["myapp://callback"],
        logout_urls=["myapp://logout"],
        allowed_oauth_flows=["code"],
    )


# --- Trigger Tests ---


def test_user_pool_trigger_creates_function(stelvio_env, project_dir):
    def infra():
        UserPool(
            "auth",
            usernames=["email"],
            triggers={"pre_sign_up": "handlers/echo.main"},
        )

    outputs = stelvio_env.deploy(infra)

    # Verify Lambda function was created for the trigger
    assert_lambda_function(outputs["function_auth-trigger-pre_sign_up_arn"])

    # Verify pool has the trigger configured
    assert_cognito_user_pool(
        outputs["user_pool_auth_id"],
        lambda_config_triggers=["pre_sign_up"],
    )


def test_user_pool_multiple_triggers(stelvio_env, project_dir):
    def infra():
        UserPool(
            "auth",
            usernames=["email"],
            triggers={
                "pre_sign_up": "handlers/echo.main",
                "post_confirmation": "handlers/echo.main",
            },
        )

    outputs = stelvio_env.deploy(infra)

    # Both Lambda functions created
    assert_lambda_function(outputs["function_auth-trigger-pre_sign_up_arn"])
    assert_lambda_function(outputs["function_auth-trigger-post_confirmation_arn"])

    # Pool has both triggers
    assert_cognito_user_pool(
        outputs["user_pool_auth_id"],
        lambda_config_triggers=["pre_sign_up", "post_confirmation"],
    )


def test_user_pool_trigger_e2e(stelvio_env, project_dir):
    """End-to-end: sign up triggers pre_sign_up Lambda, which records to DynamoDB."""

    def infra():
        results = DynamoTable("results", fields={"pk": "S"}, partition_key="pk")
        pool = UserPool(
            "auth",
            usernames=["email"],
            triggers={
                "pre_sign_up": FunctionConfig(
                    handler="handlers/cognito_trigger.pre_sign_up",
                    links=[results],
                ),
            },
        )
        pool.add_client("web")

    outputs = stelvio_env.deploy(infra)

    pool_id = outputs["user_pool_auth_id"]
    client_id = outputs["user_pool_client_auth-web_id"]
    test_email = "test-cognito-e2e@example.com"
    test_password = "TestP@ssw0rd!"  # noqa: S105

    try:
        # Trigger: sign up a user — pre_sign_up Lambda fires
        sign_up_cognito_user(pool_id, client_id, test_email, test_password)

        # Poll: wait for the trigger Lambda to write to results table
        items = poll_dynamo_items(outputs["dynamotable_results_name"])
        assert len(items) >= 1
        event = json.loads(items[0]["event"])
        assert event["triggerSource"] == "PreSignUp_SignUp"
        assert event["request"]["userAttributes"]["email"] == test_email
    finally:
        # Cleanup: delete the test user
        try:
            admin_delete_cognito_user(pool_id, test_email)
        except ClientError as e:
            if e.response["Error"]["Code"] != "UserNotFoundException":
                raise


def test_user_pool_trigger_tags_propagate(stelvio_env, project_dir):
    def infra():
        UserPool(
            "auth",
            usernames=["email"],
            triggers={"pre_sign_up": "handlers/echo.main"},
            tags={"Team": "platform"},
        )

    outputs = stelvio_env.deploy(infra)

    assert_lambda_tags(
        outputs["function_auth-trigger-pre_sign_up_arn"],
        {"Team": "platform"},
    )


# --- Identity Provider Tests ---


def test_user_pool_identity_provider_google(stelvio_env):
    def infra():
        pool = UserPool("auth", usernames=["email"])
        pool.add_identity_provider(
            "google",
            provider_type="google",
            details={
                "client_id": "fake-google-client-id",
                "client_secret": "fake-google-client-secret",
                "authorize_scopes": "openid email profile",
            },
            attributes={"email": "email", "username": "sub"},
        )

    outputs = stelvio_env.deploy(infra)

    assert_cognito_identity_provider(
        outputs["user_pool_auth_id"],
        "Google",
        provider_type="Google",
        provider_details={
            "client_id": "fake-google-client-id",
            "authorize_scopes": "openid email profile",
        },
        attribute_mapping={"email": "email", "username": "sub"},
    )


def test_user_pool_identity_provider_oidc(stelvio_env):
    def infra():
        pool = UserPool("auth", usernames=["email"])
        pool.add_identity_provider(
            "myoidc",
            provider_type="oidc",
            details={
                "client_id": "fake-oidc-client-id",
                "client_secret": "fake-oidc-client-secret",
                "oidc_issuer": "https://accounts.google.com",
                "authorize_scopes": "openid email",
                "attributes_request_method": "GET",
            },
            attributes={"email": "email"},
        )

    outputs = stelvio_env.deploy(infra)

    assert_cognito_identity_provider(
        outputs["user_pool_auth_id"],
        "myoidc",
        provider_type="OIDC",
        provider_details={
            "client_id": "fake-oidc-client-id",
            "oidc_issuer": "https://accounts.google.com",
            "authorize_scopes": "openid email",
        },
        attribute_mapping={"email": "email"},
    )


def test_user_pool_client_with_provider(stelvio_env):
    def infra():
        pool = UserPool("auth", usernames=["email"])
        google = pool.add_identity_provider(
            "google",
            provider_type="google",
            details={
                "client_id": "fake-google-client-id",
                "client_secret": "fake-google-client-secret",
                "authorize_scopes": "openid email profile",
            },
        )
        pool.add_client(
            "web",
            callback_urls=["https://example.com/callback"],
            logout_urls=["https://example.com/logout"],
            providers=[google.provider_name, "COGNITO"],
        )

    outputs = stelvio_env.deploy(infra)

    assert_cognito_user_pool_client(
        outputs["user_pool_auth_id"],
        outputs["user_pool_client_auth-web_id"],
        supported_identity_providers=["Google", "COGNITO"],
    )
