import pulumi
import pytest

from stelvio.aws.cognito.identity_provider import IdentityProvider
from stelvio.aws.cognito.user_pool import UserPool

from ...conftest import TP
from ..pulumi_mocks import tid


def _force_idp(pool, idp_result):
    """Access pool.arn to force _create_resources, then return IdP output for waiting."""
    pool_arn = pool.arn  # triggers _create_resources, populates idp_result._resources
    idp_output = idp_result.resources.identity_provider.provider_name
    return pool_arn, idp_output


# =========================================================================
# Basic identity provider creation
# =========================================================================


@pulumi.runtime.test
def test_google_provider_creates_identity_provider(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    google = pool.add_identity_provider(
        "google",
        provider_type="google",
        details={"client_id": "xxx", "client_secret": "yyy"},
    )

    pool_arn, idp_output = _force_idp(pool, google)

    def check(_):
        providers = pulumi_mocks.created_identity_providers()
        assert len(providers) == 1
        assert providers[0].typ == "aws:cognito/identityProvider:IdentityProvider"

    pulumi.Output.all(pool_arn, idp_output).apply(check)


@pulumi.runtime.test
def test_google_provider_type_mapping(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    google = pool.add_identity_provider(
        "google",
        provider_type="google",
        details={"client_id": "xxx", "client_secret": "yyy"},
    )

    pool_arn, idp_output = _force_idp(pool, google)

    def check(_):
        idp_name = f"{TP}users-idp-Google"
        mock = pulumi_mocks.assert_identity_provider_created(idp_name)
        assert mock.inputs["providerName"] == "Google"
        assert mock.inputs["providerType"] == "Google"

    pulumi.Output.all(pool_arn, idp_output).apply(check)


@pulumi.runtime.test
def test_facebook_provider_type_mapping(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    fb = pool.add_identity_provider(
        "facebook",
        provider_type="facebook",
        details={"client_id": "xxx", "client_secret": "yyy"},
    )

    pool_arn, idp_output = _force_idp(pool, fb)

    def check(_):
        idp_name = f"{TP}users-idp-Facebook"
        mock = pulumi_mocks.assert_identity_provider_created(idp_name)
        assert mock.inputs["providerName"] == "Facebook"
        assert mock.inputs["providerType"] == "Facebook"

    pulumi.Output.all(pool_arn, idp_output).apply(check)


@pulumi.runtime.test
def test_apple_provider_type_mapping(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    apple = pool.add_identity_provider(
        "apple",
        provider_type="apple",
        details={
            "client_id": "com.example.app",
            "team_id": "TEAM123",
            "key_id": "KEY123",
            "authorize_scopes": "email name",
        },
    )

    pool_arn, idp_output = _force_idp(pool, apple)

    def check(_):
        idp_name = f"{TP}users-idp-SignInWithApple"
        mock = pulumi_mocks.assert_identity_provider_created(idp_name)
        assert mock.inputs["providerName"] == "SignInWithApple"
        assert mock.inputs["providerType"] == "SignInWithApple"

    pulumi.Output.all(pool_arn, idp_output).apply(check)


@pulumi.runtime.test
def test_amazon_provider_type_mapping(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    amazon = pool.add_identity_provider(
        "amazon",
        provider_type="amazon",
        details={"client_id": "xxx", "client_secret": "yyy"},
    )

    pool_arn, idp_output = _force_idp(pool, amazon)

    def check(_):
        idp_name = f"{TP}users-idp-LoginWithAmazon"
        mock = pulumi_mocks.assert_identity_provider_created(idp_name)
        assert mock.inputs["providerName"] == "LoginWithAmazon"
        assert mock.inputs["providerType"] == "LoginWithAmazon"

    pulumi.Output.all(pool_arn, idp_output).apply(check)


# =========================================================================
# OIDC and SAML providers (user-provided names)
# =========================================================================


@pulumi.runtime.test
def test_oidc_provider_uses_user_provided_name(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    oidc = pool.add_identity_provider(
        "my-oidc",
        provider_type="oidc",
        details={
            "client_id": "xxx",
            "client_secret": "yyy",
            "oidc_issuer": "https://idp.example.com",
        },
    )

    pool_arn, idp_output = _force_idp(pool, oidc)

    def check(_):
        idp_name = f"{TP}users-idp-my-oidc"
        mock = pulumi_mocks.assert_identity_provider_created(idp_name)
        assert mock.inputs["providerName"] == "my-oidc"
        assert mock.inputs["providerType"] == "OIDC"

    pulumi.Output.all(pool_arn, idp_output).apply(check)


@pulumi.runtime.test
def test_saml_provider_uses_user_provided_name(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    saml = pool.add_identity_provider(
        "corp-saml",
        provider_type="saml",
        details={"MetadataURL": "https://idp.example.com/metadata"},
    )

    pool_arn, idp_output = _force_idp(pool, saml)

    def check(_):
        idp_name = f"{TP}users-idp-corp-saml"
        mock = pulumi_mocks.assert_identity_provider_created(idp_name)
        assert mock.inputs["providerName"] == "corp-saml"
        assert mock.inputs["providerType"] == "SAML"

    pulumi.Output.all(pool_arn, idp_output).apply(check)


# =========================================================================
# Provider details and attribute mapping
# =========================================================================


@pulumi.runtime.test
def test_provider_details_passed_through(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    details = {"client_id": "my-client-id", "client_secret": "my-secret"}
    google = pool.add_identity_provider("google", provider_type="google", details=details)

    pool_arn, idp_output = _force_idp(pool, google)

    def check(_):
        idp_name = f"{TP}users-idp-Google"
        mock = pulumi_mocks.assert_identity_provider_created(idp_name)
        assert mock.inputs["providerDetails"] == details

    pulumi.Output.all(pool_arn, idp_output).apply(check)


@pulumi.runtime.test
def test_attribute_mapping_passed_through(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    attrs = {"email": "email", "name": "name", "picture": "picture"}
    google = pool.add_identity_provider(
        "google",
        provider_type="google",
        details={"client_id": "xxx", "client_secret": "yyy"},
        attributes=attrs,
    )

    pool_arn, idp_output = _force_idp(pool, google)

    def check(_):
        idp_name = f"{TP}users-idp-Google"
        mock = pulumi_mocks.assert_identity_provider_created(idp_name)
        assert mock.inputs["attributeMapping"] == attrs

    pulumi.Output.all(pool_arn, idp_output).apply(check)


@pulumi.runtime.test
def test_no_attribute_mapping_when_none(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    google = pool.add_identity_provider(
        "google",
        provider_type="google",
        details={"client_id": "xxx", "client_secret": "yyy"},
    )

    pool_arn, idp_output = _force_idp(pool, google)

    def check(_):
        idp_name = f"{TP}users-idp-Google"
        mock = pulumi_mocks.assert_identity_provider_created(idp_name)
        assert "attributeMapping" not in mock.inputs

    pulumi.Output.all(pool_arn, idp_output).apply(check)


# =========================================================================
# User pool ID reference
# =========================================================================


@pulumi.runtime.test
def test_provider_references_pool_id(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    google = pool.add_identity_provider(
        "google",
        provider_type="google",
        details={"client_id": "xxx", "client_secret": "yyy"},
    )

    pool_arn, idp_output = _force_idp(pool, google)

    def check(_):
        idp_name = f"{TP}users-idp-Google"
        mock = pulumi_mocks.assert_identity_provider_created(idp_name)
        assert mock.inputs["userPoolId"] == tid(f"{TP}users")

    pulumi.Output.all(pool_arn, idp_output).apply(check)


# =========================================================================
# Duplicate name rejection
# =========================================================================


def test_duplicate_provider_name_rejection():
    pool = UserPool("users", usernames=["email"])
    pool.add_identity_provider(
        "google",
        provider_type="google",
        details={"client_id": "xxx", "client_secret": "yyy"},
    )
    with pytest.raises(ValueError, match="Duplicate identity provider name"):
        pool.add_identity_provider(
            "google",
            provider_type="google",
            details={"client_id": "zzz", "client_secret": "www"},
        )


def test_duplicate_oidc_provider_name_rejection():
    pool = UserPool("users", usernames=["email"])
    pool.add_identity_provider(
        "my-oidc",
        provider_type="oidc",
        details={
            "client_id": "xxx",
            "client_secret": "yyy",
            "oidc_issuer": "https://a.example.com",
        },
    )
    with pytest.raises(ValueError, match="Duplicate identity provider name"):
        pool.add_identity_provider(
            "my-oidc",
            provider_type="oidc",
            details={
                "client_id": "zzz",
                "client_secret": "www",
                "oidc_issuer": "https://b.example.com",
            },
        )


def test_different_oidc_provider_names_allowed():
    pool = UserPool("users", usernames=["email"])
    pool.add_identity_provider(
        "my-oidc",
        provider_type="oidc",
        details={
            "client_id": "xxx",
            "client_secret": "yyy",
            "oidc_issuer": "https://a.example.com",
        },
    )
    pool.add_identity_provider(
        "other-oidc",
        provider_type="oidc",
        details={
            "client_id": "zzz",
            "client_secret": "www",
            "oidc_issuer": "https://b.example.com",
        },
    )
    assert len(pool.identity_providers) == 2


# =========================================================================
# Provider name available before and after resources
# =========================================================================


@pulumi.runtime.test
def test_provider_name_available_before_resources(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    google = pool.add_identity_provider(
        "google",
        provider_type="google",
        details={"client_id": "xxx", "client_secret": "yyy"},
    )

    # provider_name should be usable as Output before pool.resources is accessed
    def check(name):
        assert name == "Google"

    google.provider_name.apply(check)


@pulumi.runtime.test
def test_provider_name_available_after_resources(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    google = pool.add_identity_provider(
        "google",
        provider_type="google",
        details={"client_id": "xxx", "client_secret": "yyy"},
    )

    pool_arn, idp_output = _force_idp(pool, google)

    def check(args):
        _, name = args
        assert name == "Google"

    pulumi.Output.all(pool_arn, idp_output).apply(check)


# =========================================================================
# Client wiring
# =========================================================================


@pulumi.runtime.test
def test_provider_name_can_wire_to_client(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    google = pool.add_identity_provider(
        "google",
        provider_type="google",
        details={"client_id": "xxx", "client_secret": "yyy"},
    )
    client = pool.add_client("web", providers=[google.provider_name, "COGNITO"])

    pool_arn, idp_output = _force_idp(pool, google)

    def check(_):
        pulumi_mocks.assert_identity_provider_created(f"{TP}users-idp-Google")
        client_mock = pulumi_mocks.assert_user_pool_client_created(f"{TP}users-web")
        providers = client_mock.inputs["supportedIdentityProviders"]
        assert "Google" in providers
        assert "COGNITO" in providers

    pulumi.Output.all(pool_arn, idp_output, client.client_id).apply(check)


# =========================================================================
# Multiple providers
# =========================================================================


@pulumi.runtime.test
def test_multiple_providers_created(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    google = pool.add_identity_provider(
        "google",
        provider_type="google",
        details={"client_id": "g-id", "client_secret": "g-secret"},
    )
    fb = pool.add_identity_provider(
        "facebook",
        provider_type="facebook",
        details={"client_id": "f-id", "client_secret": "f-secret"},
    )

    _, google_output = _force_idp(pool, google)
    fb_output = fb.resources.identity_provider.provider_name

    def check(_):
        providers = pulumi_mocks.created_identity_providers()
        assert len(providers) == 2

    pulumi.Output.all(pool.arn, google_output, fb_output).apply(check)


# =========================================================================
# Customization
# =========================================================================


@pulumi.runtime.test
def test_provider_customization(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    google = pool.add_identity_provider(
        "google",
        provider_type="google",
        details={"client_id": "xxx", "client_secret": "yyy"},
        customize={"identity_provider": {"idp_identifiers": ["custom-id"]}},
    )

    pool_arn, idp_output = _force_idp(pool, google)

    def check(_):
        idp_name = f"{TP}users-idp-Google"
        mock = pulumi_mocks.assert_identity_provider_created(idp_name)
        assert mock.inputs["idpIdentifiers"] == ["custom-id"]

    pulumi.Output.all(pool_arn, idp_output).apply(check)


@pulumi.runtime.test
def test_provider_customization_overrides_defaults(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    google = pool.add_identity_provider(
        "google",
        provider_type="google",
        details={"client_id": "xxx", "client_secret": "yyy"},
        customize={
            "identity_provider": {
                "provider_details": {"client_id": "overridden"},
            }
        },
    )

    pool_arn, idp_output = _force_idp(pool, google)

    def check(_):
        idp_name = f"{TP}users-idp-Google"
        mock = pulumi_mocks.assert_identity_provider_created(idp_name)
        assert mock.inputs["providerDetails"] == {"client_id": "overridden"}

    pulumi.Output.all(pool_arn, idp_output).apply(check)


# =========================================================================
# Modify after create
# =========================================================================


@pulumi.runtime.test
def test_add_identity_provider_after_resources_created_raises(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])

    def check(_):
        with pytest.raises(RuntimeError, match="Cannot modify"):
            pool.add_identity_provider(
                "google",
                provider_type="google",
                details={"client_id": "xxx", "client_secret": "yyy"},
            )

    pool.arn.apply(check)


# =========================================================================
# Return type
# =========================================================================


@pulumi.runtime.test
def test_add_identity_provider_returns_result(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    result = pool.add_identity_provider(
        "google",
        provider_type="google",
        details={"client_id": "xxx", "client_secret": "yyy"},
    )

    def check(_):
        assert isinstance(result, IdentityProvider)

    pool.arn.apply(check)


# =========================================================================
# Resources property
# =========================================================================


@pulumi.runtime.test
def test_resources_available_after_pool_creation(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    google = pool.add_identity_provider(
        "google",
        provider_type="google",
        details={"client_id": "xxx", "client_secret": "yyy"},
    )

    pool_arn, idp_output = _force_idp(pool, google)

    def check(_):
        resources = google.resources
        assert resources.identity_provider is not None

    pulumi.Output.all(pool_arn, idp_output).apply(check)


@pulumi.runtime.test
def test_resources_without_pool_creation_create_pool_first(pulumi_mocks):
    """Resources can be accessed independently - they trigger parent creation automatically."""
    pool = UserPool("users", usernames=["email"])
    google = pool.add_identity_provider(
        "google",
        provider_type="google",
        details={"client_id": "xxx", "client_secret": "yyy"},
    )
    # Accessing IdP resources automatically triggers pool creation first
    # via lazy evaluation
    idp_resource = google.resources.identity_provider

    def check(_):
        # Verify both pool and IdP were created in correct order
        assert len(pulumi_mocks.created_user_pools()) == 1
        assert len(pulumi_mocks.created_identity_providers()) == 1

    idp_resource.provider_name.apply(check)
