import pulumi
import pytest

from stelvio.aws.cognito.user_pool import UserPool

from ...conftest import TP


@pulumi.runtime.test
def test_user_pool_add_client_creates_user_pool_client_resource(pulumi_mocks):
    pool = UserPool("users")
    web_client = pool.add_client("web")

    def check_resources(_):
        clients = pulumi_mocks.created_user_pool_clients()
        assert len(clients) == 1
        client = clients[0]
        assert client.typ == "aws:cognito/userPoolClient:UserPoolClient"
        assert client.inputs["name"] == "web"

    return pool.id.apply(lambda _: web_client.client_id.apply(check_resources))


@pulumi.runtime.test
def test_user_pool_client_with_callbacks_sets_oauth_configuration(pulumi_mocks):
    pool = UserPool("users")
    web_client = pool.add_client(
        "web",
        callback_urls=["https://app.example.com/callback"],
    )

    def check_resources(_):
        client = pulumi_mocks.created_user_pool_clients()[0]
        assert client.inputs["allowedOauthFlowsUserPoolClient"] is True
        assert client.inputs["allowedOauthFlows"] == ["code"]
        assert client.inputs["allowedOauthScopes"] == ["openid", "email", "profile"]

    return pool.id.apply(lambda _: web_client.client_id.apply(check_resources))


@pulumi.runtime.test
def test_user_pool_client_without_callbacks_has_no_oauth_configuration(pulumi_mocks):
    pool = UserPool("users")
    web_client = pool.add_client("web")

    def check_resources(_):
        client = pulumi_mocks.created_user_pool_clients()[0]
        assert client.inputs.get("allowedOauthFlowsUserPoolClient") is None
        assert client.inputs.get("allowedOauthFlows") is None
        assert client.inputs.get("allowedOauthScopes") is None

    return pool.id.apply(lambda _: web_client.client_id.apply(check_resources))


@pulumi.runtime.test
def test_user_pool_client_generate_secret_is_passed_to_resource(pulumi_mocks):
    pool = UserPool("users")
    web_client = pool.add_client("web", generate_secret=True)

    def check_resources(_):
        client = pulumi_mocks.created_user_pool_clients()[0]
        assert client.inputs["generateSecret"] is True

    return pool.id.apply(lambda _: web_client.client_id.apply(check_resources))


@pulumi.runtime.test
def test_user_pool_with_multiple_clients_creates_distinct_resources(pulumi_mocks):
    pool = UserPool("users")
    web_client = pool.add_client("web")
    mobile_client = pool.add_client("mobile")

    def check_resources(_):
        clients = pulumi_mocks.created_user_pool_clients()
        assert len(clients) == 2

        names = {client.inputs["name"] for client in clients}
        assert names == {"web", "mobile"}

    return pool.id.apply(
        lambda _: pulumi.Output.all(web_client.client_id, mobile_client.client_id).apply(
            check_resources
        )
    )


def test_user_pool_rejects_duplicate_client_names():
    pool = UserPool("users")
    _ = pool.add_client("web")

    with pytest.raises(ValueError, match="already exists"):
        pool.add_client("web")


@pulumi.runtime.test
def test_user_pool_client_with_providers_sets_supported_identity_providers(pulumi_mocks):
    pool = UserPool("users")
    web_client = pool.add_client("web", providers=["Google", "COGNITO"])

    def check_resources(_):
        client = pulumi_mocks.created_user_pool_clients()[0]
        assert client.inputs["supportedIdentityProviders"] == ["Google", "COGNITO"]

    return pool.id.apply(lambda _: web_client.client_id.apply(check_resources))


@pulumi.runtime.test
def test_user_pool_client_defaults_supported_identity_providers_to_cognito(pulumi_mocks):
    pool = UserPool("users")
    web_client = pool.add_client("web")

    def check_resources(_):
        client = pulumi_mocks.created_user_pool_clients()[0]
        assert client.inputs["supportedIdentityProviders"] == ["COGNITO"]

    return pool.id.apply(lambda _: web_client.client_id.apply(check_resources))


@pulumi.runtime.test
def test_user_pool_client_resource_name_follows_pool_client_pattern(pulumi_mocks):
    pool = UserPool("users")
    admin_client = pool.add_client("admin")

    def check_resources(_):
        client = pulumi_mocks.created_user_pool_clients()[0]
        assert client.typ == "aws:cognito/userPoolClient:UserPoolClient"
        assert client.name.startswith(TP)
        assert "users-admin" in client.name

    return pool.id.apply(lambda _: admin_client.client_id.apply(check_resources))


def test_user_pool_add_client_after_resource_creation_is_rejected():
    pool = UserPool("users")
    _ = pool.resources

    with pytest.raises(RuntimeError, match="after resources have been created"):
        pool.add_client("web")


@pulumi.runtime.test
def test_user_pool_client_properties_resolve_client_id_and_secret(pulumi_mocks):
    pool = UserPool("users")
    web_client = pool.add_client("web", generate_secret=True)

    def check_values(values):
        client_id, client_secret = values
        client = pulumi_mocks.created_user_pool_clients()[0]
        expected_client_id = f"{client.name}-test-id"
        assert client_id == expected_client_id
        assert client_secret == f"secret-{expected_client_id}"

    return pool.id.apply(
        lambda _: pulumi.Output.all(web_client.client_id, web_client.client_secret).apply(
            check_values
        )
    )


@pulumi.runtime.test
def test_user_pool_client_customization_overrides_args(pulumi_mocks):
    pool = UserPool("users")
    web_client = pool.add_client(
        "web",
        generate_secret=False,
        customize={
            "client": {
                "generate_secret": True,
            }
        },
    )

    def check_resources(_):
        client = pulumi_mocks.created_user_pool_clients()[0]
        assert client.inputs["generateSecret"] is True

    return pool.id.apply(lambda _: web_client.client_id.apply(check_resources))
