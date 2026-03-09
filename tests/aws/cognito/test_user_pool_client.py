import pulumi
import pytest

from stelvio.aws.cognito.types import UserPoolClientConfig
from stelvio.aws.cognito.user_pool import UserPool
from stelvio.aws.cognito.user_pool_client import UserPoolClient

from ...conftest import TP
from ..pulumi_mocks import tid

# =========================================================================
# Basic client creation
# =========================================================================


@pulumi.runtime.test
def test_basic_client_creation(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client("web")

    def check(_):
        clients = pulumi_mocks.created_user_pool_clients()
        assert len(clients) == 1
        assert clients[0].typ == "aws:cognito/userPoolClient:UserPoolClient"

    pulumi.Output.all(pool.arn, client.client_id).apply(check)


@pulumi.runtime.test
def test_client_naming(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client("web")

    def check(_):
        client_name = f"{TP}users-web"
        mock = pulumi_mocks.assert_user_pool_client_created(client_name)
        assert mock.inputs["name"] == client_name

    pulumi.Output.all(pool.arn, client.client_id).apply(check)


@pulumi.runtime.test
def test_client_user_pool_id_reference(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client("web")

    def check(_):
        client_name = f"{TP}users-web"
        mock = pulumi_mocks.assert_user_pool_client_created(client_name)
        # user_pool_id should reference the pool's id (Pulumi resource ID from mock)
        pool_resource_name = f"{TP}users"
        assert mock.inputs["userPoolId"] == tid(pool_resource_name)

    pulumi.Output.all(pool.arn, client.client_id).apply(check)


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


@pulumi.runtime.test
def test_client_with_callback_urls(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client(
        "web",
        callback_urls=["https://app.example.com/callback"],
    )

    def check(_):
        mock = pulumi_mocks.assert_user_pool_client_created(f"{TP}users-web")
        assert mock.inputs["callbackUrls"] == ["https://app.example.com/callback"]
        assert mock.inputs["allowedOauthFlowsUserPoolClient"] is True
        assert mock.inputs["allowedOauthFlows"] == ["code"]
        assert mock.inputs["allowedOauthScopes"] == ["openid", "email", "profile"]

    pulumi.Output.all(pool.arn, client.client_id).apply(check)


@pulumi.runtime.test
def test_client_with_logout_urls(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client(
        "web",
        logout_urls=["https://app.example.com/logout"],
    )

    def check(_):
        mock = pulumi_mocks.assert_user_pool_client_created(f"{TP}users-web")
        assert mock.inputs["logoutUrls"] == ["https://app.example.com/logout"]
        assert mock.inputs["allowedOauthFlowsUserPoolClient"] is True

    pulumi.Output.all(pool.arn, client.client_id).apply(check)


@pulumi.runtime.test
def test_client_with_both_callback_and_logout_urls(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client(
        "web",
        callback_urls=["https://app.example.com/callback"],
        logout_urls=["https://app.example.com/logout"],
    )

    def check(_):
        mock = pulumi_mocks.assert_user_pool_client_created(f"{TP}users-web")
        assert mock.inputs["callbackUrls"] == ["https://app.example.com/callback"]
        assert mock.inputs["logoutUrls"] == ["https://app.example.com/logout"]
        assert mock.inputs["allowedOauthFlowsUserPoolClient"] is True
        assert mock.inputs["allowedOauthFlows"] == ["code"]
        assert mock.inputs["allowedOauthScopes"] == ["openid", "email", "profile"]

    pulumi.Output.all(pool.arn, client.client_id).apply(check)


@pulumi.runtime.test
def test_client_without_callbacks_no_oauth(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client("web")

    def check(_):
        mock = pulumi_mocks.assert_user_pool_client_created(f"{TP}users-web")
        assert "callbackUrls" not in mock.inputs
        assert "logoutUrls" not in mock.inputs
        assert "allowedOauthFlowsUserPoolClient" not in mock.inputs
        assert "allowedOauthFlows" not in mock.inputs
        assert "allowedOauthScopes" not in mock.inputs

    pulumi.Output.all(pool.arn, client.client_id).apply(check)


# =========================================================================
# Generate secret
# =========================================================================


@pulumi.runtime.test
def test_generate_secret_true(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client("backend", generate_secret=True)

    def check(_):
        mock = pulumi_mocks.assert_user_pool_client_created(f"{TP}users-backend")
        assert mock.inputs["generateSecret"] is True

    pulumi.Output.all(pool.arn, client.client_id).apply(check)


@pulumi.runtime.test
def test_generate_secret_false_by_default(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client("web")

    def check(_):
        mock = pulumi_mocks.assert_user_pool_client_created(f"{TP}users-web")
        assert mock.inputs["generateSecret"] is False

    pulumi.Output.all(pool.arn, client.client_id).apply(check)


# =========================================================================
# Providers
# =========================================================================


@pulumi.runtime.test
def test_default_provider_cognito(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client("web")

    def check(_):
        mock = pulumi_mocks.assert_user_pool_client_created(f"{TP}users-web")
        assert mock.inputs["supportedIdentityProviders"] == ["COGNITO"]

    pulumi.Output.all(pool.arn, client.client_id).apply(check)


@pulumi.runtime.test
def test_custom_providers_list(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client("web", providers=["Google", "COGNITO"])

    def check(_):
        mock = pulumi_mocks.assert_user_pool_client_created(f"{TP}users-web")
        assert mock.inputs["supportedIdentityProviders"] == ["Google", "COGNITO"]

    pulumi.Output.all(pool.arn, client.client_id).apply(check)


# =========================================================================
# Multiple clients
# =========================================================================


@pulumi.runtime.test
def test_multiple_clients(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    web = pool.add_client("web")
    mobile = pool.add_client("mobile")

    def check(_):
        clients = pulumi_mocks.created_user_pool_clients()
        assert len(clients) == 2
        names = {c.name for c in clients}
        assert f"{TP}users-web" in names
        assert f"{TP}users-mobile" in names

    pulumi.Output.all(pool.arn, web.client_id, mobile.client_id).apply(check)


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

    def check(secret):
        assert "client-secret" in secret

    pulumi.Output.all(pool.arn).apply(lambda _: None)
    client.client_secret.apply(check)


def test_client_secret_property_without_secret(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client("web")
    assert client.generate_secret is False
    assert client.client_secret is None


# =========================================================================
# Client customization
# =========================================================================


@pulumi.runtime.test
def test_client_customization(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client(
        "web",
        customize={"client": {"explicit_auth_flows": ["ALLOW_USER_PASSWORD_AUTH"]}},
    )

    def check(_):
        mock = pulumi_mocks.assert_user_pool_client_created(f"{TP}users-web")
        assert mock.inputs["explicitAuthFlows"] == ["ALLOW_USER_PASSWORD_AUTH"]

    pulumi.Output.all(pool.arn, client.client_id).apply(check)


@pulumi.runtime.test
def test_client_customization_overrides_defaults(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client(
        "web",
        customize={"client": {"generate_secret": True}},
    )

    def check(_):
        mock = pulumi_mocks.assert_user_pool_client_created(f"{TP}users-web")
        # Customizer overrides the default generate_secret=False
        assert mock.inputs["generateSecret"] is True

    pulumi.Output.all(pool.arn, client.client_id).apply(check)


# =========================================================================
# Lazy pool creation
# =========================================================================


@pulumi.runtime.test
def test_create_resources_without_pool_creates_pool_first(pulumi_mocks):
    """Client resources can be created independently.

    They trigger pool creation automatically.
    """
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client("web")
    # Accessing client.resources automatically triggers pool creation first
    # via lazy evaluation
    client_resource = client.resources.client

    def check(_):
        # Verify both pool and client were created
        assert len(pulumi_mocks.created_user_pools()) == 1
        assert len(pulumi_mocks.created_user_pool_clients()) == 1

    client_resource.id.apply(check)


# =========================================================================
# Config object
# =========================================================================


@pulumi.runtime.test
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

    def check(_):
        mock = pulumi_mocks.assert_user_pool_client_created(f"{TP}users-web")
        assert mock.inputs["callbackUrls"] == ["https://app.example.com/callback"]
        assert mock.inputs["logoutUrls"] == ["https://app.example.com/logout"]
        assert mock.inputs["generateSecret"] is True
        assert mock.inputs["allowedOauthFlowsUserPoolClient"] is True

    pulumi.Output.all(pool.arn, client.client_id).apply(check)


def test_add_client_config_and_opts_raises(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    with pytest.raises(ValueError, match="cannot combine"):
        pool.add_client(
            "web",
            config=UserPoolClientConfig(generate_secret=True),
            callback_urls=["https://app.example.com/callback"],
        )
