import pulumi
import pytest

from stelvio.aws.cognito.types import (
    UserPoolClientConfig,
    UserPoolClientConfigDict,
)
from stelvio.aws.cognito.user_pool import UserPool
from stelvio.aws.cognito.user_pool_client import UserPoolClient

from ...conftest import TP
from ..conftest import assert_hash_truncated, assert_urn
from ..pulumi_mocks import R, tid

# =========================================================================
# Basic client creation
# =========================================================================


def test_basic_client_creation(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client("web")

    @pulumi.runtime.test
    def deploy():
        return [pool.resources, client.resources]

    deploy()

    pulumi_mocks.assert_res(
        "users-web",
        R.USER_POOL_CLIENT,
        {
            "userPoolId": tid(f"{TP}users"),
            "generateSecret": False,
            "supportedIdentityProviders": ["COGNITO"],
        },
    )
    pulumi_mocks.assert_res_counts({R.USER_POOL: 1, R.USER_POOL_CLIENT: 1})


def test_client_naming(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client("web")

    @pulumi.runtime.test
    def deploy():
        return [pool.resources, client.resources]

    deploy()

    pulumi_mocks.assert_res("users-web", R.USER_POOL_CLIENT, {"name": None}, partial=True)


def test_client_user_pool_id_reference(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client("web")

    @pulumi.runtime.test
    def deploy():
        return [pool.resources, client.resources]

    deploy()

    pulumi_mocks.assert_res(
        "users-web", R.USER_POOL_CLIENT, {"userPoolId": tid(f"{TP}users")}, partial=True
    )


@pulumi.runtime.test
def test_add_client_returns_user_pool_client(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client("web")

    def check(_):
        assert isinstance(client, UserPoolClient)

    pulumi.Output.all(pool.arn, client.client_id).apply(check)


# =========================================================================
# OAuth configuration
# =========================================================================


def test_client_with_callback_urls(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client(
        "web",
        callback_urls=["https://app.example.com/callback"],
    )

    @pulumi.runtime.test
    def deploy():
        return [pool.resources, client.resources]

    deploy()

    pulumi_mocks.assert_res(
        "users-web",
        R.USER_POOL_CLIENT,
        {
            "callbackUrls": ["https://app.example.com/callback"],
            "allowedOauthFlowsUserPoolClient": True,
            "allowedOauthFlows": ["code"],
            "allowedOauthScopes": ["openid", "email", "profile"],
        },
        partial=True,
    )


def test_client_with_logout_urls(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client(
        "web",
        logout_urls=["https://app.example.com/logout"],
    )

    @pulumi.runtime.test
    def deploy():
        return [pool.resources, client.resources]

    deploy()

    pulumi_mocks.assert_res(
        "users-web",
        R.USER_POOL_CLIENT,
        {
            "logoutUrls": ["https://app.example.com/logout"],
            "allowedOauthFlowsUserPoolClient": True,
        },
        partial=True,
    )


def test_client_with_both_callback_and_logout_urls(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client(
        "web",
        callback_urls=["https://app.example.com/callback"],
        logout_urls=["https://app.example.com/logout"],
    )

    @pulumi.runtime.test
    def deploy():
        return [pool.resources, client.resources]

    deploy()

    pulumi_mocks.assert_res(
        "users-web",
        R.USER_POOL_CLIENT,
        {
            "callbackUrls": ["https://app.example.com/callback"],
            "logoutUrls": ["https://app.example.com/logout"],
            "allowedOauthFlowsUserPoolClient": True,
            "allowedOauthFlows": ["code"],
            "allowedOauthScopes": ["openid", "email", "profile"],
        },
        partial=True,
    )


def test_client_without_callbacks_no_oauth(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client("web")

    @pulumi.runtime.test
    def deploy():
        return [pool.resources, client.resources]

    deploy()

    pulumi_mocks.assert_res(
        "users-web",
        R.USER_POOL_CLIENT,
        {
            "callbackUrls": None,
            "logoutUrls": None,
            "allowedOauthFlowsUserPoolClient": None,
            "allowedOauthFlows": None,
            "allowedOauthScopes": None,
        },
        partial=True,
    )


# =========================================================================
# Generate secret
# =========================================================================


def test_generate_secret_true(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client("backend", generate_secret=True)

    @pulumi.runtime.test
    def deploy():
        return [pool.resources, client.resources]

    deploy()

    pulumi_mocks.assert_res(
        "users-backend", R.USER_POOL_CLIENT, {"generateSecret": True}, partial=True
    )


def test_generate_secret_false_by_default(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client("web")

    @pulumi.runtime.test
    def deploy():
        return [pool.resources, client.resources]

    deploy()

    pulumi_mocks.assert_res(
        "users-web", R.USER_POOL_CLIENT, {"generateSecret": False}, partial=True
    )


# =========================================================================
# Providers
# =========================================================================


def test_default_provider_cognito(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client("web")

    @pulumi.runtime.test
    def deploy():
        return [pool.resources, client.resources]

    deploy()

    pulumi_mocks.assert_res(
        "users-web", R.USER_POOL_CLIENT, {"supportedIdentityProviders": ["COGNITO"]}, partial=True
    )


def test_custom_providers_list(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client("web", providers=["Google", "COGNITO"])

    @pulumi.runtime.test
    def deploy():
        return [pool.resources, client.resources]

    deploy()

    pulumi_mocks.assert_res(
        "users-web",
        R.USER_POOL_CLIENT,
        {"supportedIdentityProviders": ["Google", "COGNITO"]},
        partial=True,
    )


# =========================================================================
# Multiple clients
# =========================================================================


def test_multiple_clients(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    web = pool.add_client("web")
    mobile = pool.add_client("mobile")

    @pulumi.runtime.test
    def deploy():
        return [pool.resources, web.resources, mobile.resources]

    deploy()

    pulumi_mocks.assert_res("users-web", R.USER_POOL_CLIENT)
    pulumi_mocks.assert_res("users-mobile", R.USER_POOL_CLIENT)
    pulumi_mocks.assert_res_counts({R.USER_POOL: 1, R.USER_POOL_CLIENT: 2})


# =========================================================================
# Duplicate name rejection
# =========================================================================


def test_duplicate_client_name_rejection(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    pool.add_client("web")
    with pytest.raises(ValueError, match="Duplicate client name"):
        pool.add_client("web")


# =========================================================================
# Modify after create
# =========================================================================


@pulumi.runtime.test
def test_add_client_after_resources_created_raises(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])

    def check(_):
        # Accessing .resources triggers creation
        with pytest.raises(RuntimeError, match="Cannot modify"):
            pool.add_client("web")

    pool.arn.apply(check)


# =========================================================================
# Client properties
# =========================================================================


@pulumi.runtime.test
def test_client_id_property(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client("web")

    def check(args):
        client_id = args[1]
        # Mock returns tid(resource_name) as the Pulumi resource ID
        assert client_id == tid(f"{TP}users-web")

    pulumi.Output.all(pool.arn, client.client_id).apply(check)


@pulumi.runtime.test
def test_client_secret_property_with_secret(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client("backend", generate_secret=True)

    def check(args):
        secret = args[1]
        assert "client-secret" in secret

    pulumi.Output.all(pool.arn, client.client_secret).apply(check)


def test_client_secret_property_without_secret(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client("web")
    assert client.generate_secret is False
    assert client.client_secret is None


# =========================================================================
# Client customization
# =========================================================================


def test_client_customization(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client(
        "web",
        customize={"client": {"explicit_auth_flows": ["ALLOW_USER_PASSWORD_AUTH"]}},
    )

    @pulumi.runtime.test
    def deploy():
        return [pool.resources, client.resources]

    deploy()

    pulumi_mocks.assert_res(
        "users-web",
        R.USER_POOL_CLIENT,
        {"explicitAuthFlows": ["ALLOW_USER_PASSWORD_AUTH"]},
        partial=True,
    )


def test_client_customization_overrides_defaults(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client(
        "web",
        customize={"client": {"supported_identity_providers": ["Google"]}},
    )

    @pulumi.runtime.test
    def deploy():
        return [pool.resources, client.resources]

    deploy()

    # Customizer overrides the default ["COGNITO"]
    pulumi_mocks.assert_res(
        "users-web", R.USER_POOL_CLIENT, {"supportedIdentityProviders": ["Google"]}, partial=True
    )


# =========================================================================
# Lazy pool creation
# =========================================================================


def test_create_resources_without_pool_creates_pool_first(pulumi_mocks):
    """Client resources can be created independently.

    They trigger pool creation automatically.
    """
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client("web")

    # Only client.resources is read: it triggers pool creation first via lazy evaluation
    @pulumi.runtime.test
    def deploy():
        return client.resources

    deploy()

    pulumi_mocks.assert_res_counts({R.USER_POOL: 1, R.USER_POOL_CLIENT: 1})


# =========================================================================
# Config object
# =========================================================================


def test_add_client_with_config_object(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client(
        "web",
        config=UserPoolClientConfig(
            callback_urls=["https://app.example.com/callback"],
            logout_urls=["https://app.example.com/logout"],
            generate_secret=True,
        ),
    )

    @pulumi.runtime.test
    def deploy():
        return [pool.resources, client.resources]

    deploy()

    pulumi_mocks.assert_res(
        "users-web",
        R.USER_POOL_CLIENT,
        {
            "callbackUrls": ["https://app.example.com/callback"],
            "logoutUrls": ["https://app.example.com/logout"],
            "generateSecret": True,
            "allowedOauthFlowsUserPoolClient": True,
        },
        partial=True,
    )


def test_add_client_config_and_opts_raises(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    with pytest.raises(ValueError, match="cannot combine"):
        pool.add_client(
            "web",
            config=UserPoolClientConfig(generate_secret=True),
            callback_urls=["https://app.example.com/callback"],
        )


# =========================================================================
# Config parity tests
# =========================================================================


def test_user_pool_config_dict_matches_dataclass():
    """UserPoolClientConfigDict has the same fields as UserPoolClientConfig.

    Can't use assert_config_dict_matches_dataclass because 'providers' uses
    Input[str] which contains a ForwardRef to Output that get_type_hints()
    cannot resolve at test time.
    """
    from dataclasses import fields

    dataclass_fields = {f.name for f in fields(UserPoolClientConfig)}
    typeddict_fields = set(UserPoolClientConfigDict.__annotations__.keys())

    assert dataclass_fields == typeddict_fields, (
        f"UserPoolClientConfigDict and UserPoolClientConfig have different fields: "
        f"dataclass={dataclass_fields}, typeddict={typeddict_fields}"
    )

    # Compare raw annotation strings for fields that should have identical types.
    # Skip providers (uses Input[str] which get_type_hints can't resolve).
    # TypedDict (total=False) doesn't add | None; dataclass does — strip it for comparison.
    skip_fields = {"providers"}
    dc_annotations = UserPoolClientConfig.__dataclass_fields__
    td_annotations = UserPoolClientConfigDict.__annotations__
    for field_name in dataclass_fields - skip_fields:
        dc_type = dc_annotations[field_name].type
        td_type = td_annotations[field_name]
        if hasattr(td_type, "__forward_arg__"):
            td_type = td_type.__forward_arg__
        # Dataclass may have " | None" suffix that TypedDict omits
        dc_type_stripped = dc_type.removesuffix(" | None")
        assert dc_type_stripped == td_type, (
            f"Type mismatch for field '{field_name}': dataclass={dc_type}, typeddict={td_type}"
        )


# `parent=self` lives in ResourceOptions (invisible to input mocks), but it surfaces
# in the client component's URN as the `stelvio:aws:UserPool$` parent segment.


@pulumi.runtime.test
def test_user_pool_client_parented_to_user_pool(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client("web")
    _ = client.resources

    def check(urn):
        assert_urn(urn, "stelvio:aws:UserPool", "stelvio:aws:UserPoolClient", "users-web")

    return client.urn.apply(check)


def test_client_long_name_truncates_logical_name(pulumi_mocks):
    @pulumi.runtime.test
    def deploy():
        return UserPool("users", usernames=["email"]).add_client("c" * 150).resources

    deploy()

    [client] = pulumi_mocks.created(R.USER_POOL_CLIENT)
    assert_hash_truncated(client.name, 120)  # Cognito 128 minus the 8-char Pulumi suffix
