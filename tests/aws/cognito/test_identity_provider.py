import pulumi
import pytest

from stelvio.aws.cognito.user_pool import UserPool


@pulumi.runtime.test
def test_google_identity_provider_creates_cognito_resource(pulumi_mocks):
    pool = UserPool("users")
    provider = pool.add_identity_provider(
        "google",
        type="google",
        details={
            "client_id": "google-client-id",
            "client_secret": "google-client-secret",
            "authorize_scopes": "email profile",
        },
    )

    def check_resources(_):
        providers = pulumi_mocks.created_identity_providers()
        assert len(providers) == 1
        created = providers[0]
        assert created.typ == "aws:cognito/identityProvider:IdentityProvider"
        assert created.inputs["providerType"] == "Google"
        assert created.inputs["providerName"] == "Google"

    return pool.id.apply(lambda _: provider.provider_name.apply(check_resources))


@pulumi.runtime.test
@pytest.mark.parametrize(
    ("provider_type", "expected_name"),
    [
        ("google", "Google"),
        ("facebook", "Facebook"),
        ("apple", "SignInWithApple"),
        ("amazon", "LoginWithAmazon"),
    ],
)
def test_social_identity_provider_name_mapping(
    pulumi_mocks,
    provider_type,
    expected_name,
):
    pool = UserPool("users")
    provider = pool.add_identity_provider(
        provider_type,
        type=provider_type,
        details={"client_id": "id", "client_secret": "secret"},
    )

    def check_resources(_):
        created = pulumi_mocks.created_identity_providers()[0]
        assert created.inputs["providerName"] == expected_name

    return pool.id.apply(lambda _: provider.provider_name.apply(check_resources))


@pulumi.runtime.test
def test_oidc_identity_provider_uses_user_provided_name(pulumi_mocks):
    pool = UserPool("users")
    provider = pool.add_identity_provider(
        "auth0",
        type="oidc",
        details={
            "client_id": "oidc-client-id",
            "client_secret": "oidc-client-secret",
            "oidc_issuer": "https://example.auth0.com",
        },
    )

    def check_resources(_):
        created = pulumi_mocks.created_identity_providers()[0]
        assert created.inputs["providerType"] == "OIDC"
        assert created.inputs["providerName"] == "auth0"

    return pool.id.apply(lambda _: provider.provider_name.apply(check_resources))


@pulumi.runtime.test
def test_saml_identity_provider_uses_user_provided_name(pulumi_mocks):
    pool = UserPool("users")
    provider = pool.add_identity_provider(
        "okta-saml",
        type="saml",
        details={
            "metadata_url": "https://example.okta.com/app/metadata",
        },
    )

    def check_resources(_):
        created = pulumi_mocks.created_identity_providers()[0]
        assert created.inputs["providerType"] == "SAML"
        assert created.inputs["providerName"] == "okta-saml"

    return pool.id.apply(lambda _: provider.provider_name.apply(check_resources))


@pulumi.runtime.test
def test_identity_provider_attribute_mapping_is_passed_to_resource(pulumi_mocks):
    pool = UserPool("users")
    provider = pool.add_identity_provider(
        "google",
        type="google",
        details={"client_id": "id", "client_secret": "secret"},
        attributes={"email": "email", "username": "sub"},
    )

    def check_resources(_):
        created = pulumi_mocks.created_identity_providers()[0]
        assert created.inputs["attributeMapping"] == {
            "email": "email",
            "username": "sub",
        }

    return pool.id.apply(lambda _: provider.provider_name.apply(check_resources))


def test_user_pool_rejects_duplicate_identity_provider_names():
    pool = UserPool("users")
    _ = pool.add_identity_provider(
        "google",
        type="google",
        details={"client_id": "id", "client_secret": "secret"},
    )

    with pytest.raises(ValueError, match="already exists"):
        pool.add_identity_provider(
            "google",
            type="google",
            details={"client_id": "id2", "client_secret": "secret2"},
        )


@pulumi.runtime.test
def test_identity_provider_name_available_before_pool_resources_created(pulumi_mocks):
    pool = UserPool("users")
    provider = pool.add_identity_provider(
        "google",
        type="google",
        details={"client_id": "id", "client_secret": "secret"},
    )

    def check_name(value):
        assert value == "Google"
        assert pulumi_mocks.created_identity_providers() == []

    return provider.provider_name.apply(check_name)


@pulumi.runtime.test
def test_identity_provider_name_can_be_used_for_client_wiring(pulumi_mocks):
    pool = UserPool("users")
    google = pool.add_identity_provider(
        "google",
        type="google",
        details={"client_id": "id", "client_secret": "secret"},
    )
    web_client = pool.add_client("web", providers=[google.provider_name, "COGNITO"])

    def check_resources(_):
        providers = pulumi_mocks.created_identity_providers()
        clients = pulumi_mocks.created_user_pool_clients()

        assert len(providers) == 1
        assert len(clients) == 1
        assert clients[0].inputs["supportedIdentityProviders"] == ["Google", "COGNITO"]

    return pool.id.apply(lambda _: web_client.client_id.apply(check_resources))


@pulumi.runtime.test
def test_identity_provider_customization_overrides_args(pulumi_mocks):
    pool = UserPool("users")
    provider = pool.add_identity_provider(
        "google",
        type="google",
        details={"client_id": "id", "client_secret": "secret"},
        customize={
            "identity_provider": {
                "provider_name": "CustomGoogle",
            }
        },
    )

    def check_resources(_):
        created = pulumi_mocks.created_identity_providers()[0]
        assert created.inputs["providerName"] == "CustomGoogle"

    return pool.id.apply(lambda _: provider.provider_name.apply(check_resources))


def test_user_pool_rejects_add_identity_provider_after_resources_created():
    pool = UserPool("users")
    _ = pool.resources

    with pytest.raises(RuntimeError, match="after resources have been created"):
        pool.add_identity_provider(
            "google",
            type="google",
            details={"client_id": "id", "client_secret": "secret"},
        )
