import pulumi
import pytest

from stelvio.aws.cognito.identity_provider import IdentityProvider
from stelvio.aws.cognito.user_pool import UserPool

from ...conftest import TP
from ..conftest import assert_urn
from ..pulumi_mocks import R, tid


def _force_idp(pool, idp_result):
    """Create the pool and the IdP (each on its own `.resources`), return Outputs to wait on."""
    pool_arn = pool.arn
    idp_output = idp_result.resources.identity_provider.provider_name
    return pool_arn, idp_output


# =========================================================================
# Basic identity provider creation
# =========================================================================


def test_google_provider_creates_identity_provider(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    google = pool.add_identity_provider(
        "google",
        provider_type="google",
        details={"client_id": "xxx", "client_secret": "yyy"},
    )

    @pulumi.runtime.test
    def deploy():
        return [pool.resources, google.resources]

    deploy()

    pulumi_mocks.assert_res(
        "users-idp-Google",
        R.IDENTITY_PROVIDER,
        {
            "userPoolId": tid(f"{TP}users"),
            "providerName": "Google",
            "providerType": "Google",
            "providerDetails": {"client_id": "xxx", "client_secret": "yyy"},
        },
    )
    pulumi_mocks.assert_res_counts({R.USER_POOL: 1, R.IDENTITY_PROVIDER: 1})


def test_google_provider_type_mapping(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    google = pool.add_identity_provider(
        "google",
        provider_type="google",
        details={"client_id": "xxx", "client_secret": "yyy"},
    )

    @pulumi.runtime.test
    def deploy():
        return [pool.resources, google.resources]

    deploy()

    pulumi_mocks.assert_res(
        "users-idp-Google",
        R.IDENTITY_PROVIDER,
        {"providerName": "Google", "providerType": "Google"},
        partial=True,
    )


def test_facebook_provider_type_mapping(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    fb = pool.add_identity_provider(
        "facebook",
        provider_type="facebook",
        details={"client_id": "xxx", "client_secret": "yyy"},
    )

    @pulumi.runtime.test
    def deploy():
        return [pool.resources, fb.resources]

    deploy()

    pulumi_mocks.assert_res(
        "users-idp-Facebook",
        R.IDENTITY_PROVIDER,
        {"providerName": "Facebook", "providerType": "Facebook"},
        partial=True,
    )


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

    @pulumi.runtime.test
    def deploy():
        return [pool.resources, apple.resources]

    deploy()

    pulumi_mocks.assert_res(
        "users-idp-SignInWithApple",
        R.IDENTITY_PROVIDER,
        {"providerName": "SignInWithApple", "providerType": "SignInWithApple"},
        partial=True,
    )


def test_amazon_provider_type_mapping(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    amazon = pool.add_identity_provider(
        "amazon",
        provider_type="amazon",
        details={"client_id": "xxx", "client_secret": "yyy"},
    )

    @pulumi.runtime.test
    def deploy():
        return [pool.resources, amazon.resources]

    deploy()

    pulumi_mocks.assert_res(
        "users-idp-LoginWithAmazon",
        R.IDENTITY_PROVIDER,
        {"providerName": "LoginWithAmazon", "providerType": "LoginWithAmazon"},
        partial=True,
    )


# =========================================================================
# OIDC and SAML providers (user-provided names)
# =========================================================================


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

    @pulumi.runtime.test
    def deploy():
        return [pool.resources, oidc.resources]

    deploy()

    pulumi_mocks.assert_res(
        "users-idp-my-oidc",
        R.IDENTITY_PROVIDER,
        {"providerName": "my-oidc", "providerType": "OIDC"},
        partial=True,
    )


def test_saml_provider_uses_user_provided_name(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    saml = pool.add_identity_provider(
        "corp-saml",
        provider_type="saml",
        details={"MetadataURL": "https://idp.example.com/metadata"},
    )

    @pulumi.runtime.test
    def deploy():
        return [pool.resources, saml.resources]

    deploy()

    pulumi_mocks.assert_res(
        "users-idp-corp-saml",
        R.IDENTITY_PROVIDER,
        {"providerName": "corp-saml", "providerType": "SAML"},
        partial=True,
    )


# =========================================================================
# Provider details and attribute mapping
# =========================================================================


def test_provider_details_passed_through(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    details = {"client_id": "my-client-id", "client_secret": "my-secret"}
    google = pool.add_identity_provider("google", provider_type="google", details=details)

    @pulumi.runtime.test
    def deploy():
        return [pool.resources, google.resources]

    deploy()

    pulumi_mocks.assert_res(
        "users-idp-Google", R.IDENTITY_PROVIDER, {"providerDetails": details}, partial=True
    )


def test_attribute_mapping_passed_through(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    attrs = {"email": "email", "name": "name", "picture": "picture"}
    google = pool.add_identity_provider(
        "google",
        provider_type="google",
        details={"client_id": "xxx", "client_secret": "yyy"},
        attributes=attrs,
    )

    @pulumi.runtime.test
    def deploy():
        return [pool.resources, google.resources]

    deploy()

    pulumi_mocks.assert_res(
        "users-idp-Google", R.IDENTITY_PROVIDER, {"attributeMapping": attrs}, partial=True
    )


def test_no_attribute_mapping_when_none(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    google = pool.add_identity_provider(
        "google",
        provider_type="google",
        details={"client_id": "xxx", "client_secret": "yyy"},
    )

    @pulumi.runtime.test
    def deploy():
        return [pool.resources, google.resources]

    deploy()

    pulumi_mocks.assert_res(
        "users-idp-Google", R.IDENTITY_PROVIDER, {"attributeMapping": None}, partial=True
    )


# =========================================================================
# User pool ID reference
# =========================================================================


def test_provider_references_pool_id(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    google = pool.add_identity_provider(
        "google",
        provider_type="google",
        details={"client_id": "xxx", "client_secret": "yyy"},
    )

    @pulumi.runtime.test
    def deploy():
        return [pool.resources, google.resources]

    deploy()

    pulumi_mocks.assert_res(
        "users-idp-Google", R.IDENTITY_PROVIDER, {"userPoolId": tid(f"{TP}users")}, partial=True
    )


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


def test_provider_name_can_wire_to_client(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    google = pool.add_identity_provider(
        "google",
        provider_type="google",
        details={"client_id": "xxx", "client_secret": "yyy"},
    )
    client = pool.add_client("web", providers=[google.provider_name, "COGNITO"])

    @pulumi.runtime.test
    def deploy():
        return [pool.resources, google.resources, client.resources]

    deploy()

    pulumi_mocks.assert_res("users-idp-Google", R.IDENTITY_PROVIDER)
    pulumi_mocks.assert_res(
        "users-web",
        R.USER_POOL_CLIENT,
        {"supportedIdentityProviders": ["Google", "COGNITO"]},
        partial=True,
    )


# =========================================================================
# Multiple providers
# =========================================================================


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

    @pulumi.runtime.test
    def deploy():
        return [pool.resources, google.resources, fb.resources]

    deploy()

    pulumi_mocks.assert_res("users-idp-Google", R.IDENTITY_PROVIDER)
    pulumi_mocks.assert_res("users-idp-Facebook", R.IDENTITY_PROVIDER)
    pulumi_mocks.assert_res_counts({R.USER_POOL: 1, R.IDENTITY_PROVIDER: 2})


# =========================================================================
# Customization
# =========================================================================


def test_provider_customization(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    google = pool.add_identity_provider(
        "google",
        provider_type="google",
        details={"client_id": "xxx", "client_secret": "yyy"},
        customize={"identity_provider": {"idp_identifiers": ["custom-id"]}},
    )

    @pulumi.runtime.test
    def deploy():
        return [pool.resources, google.resources]

    deploy()

    pulumi_mocks.assert_res(
        "users-idp-Google", R.IDENTITY_PROVIDER, {"idpIdentifiers": ["custom-id"]}, partial=True
    )


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

    @pulumi.runtime.test
    def deploy():
        return [pool.resources, google.resources]

    deploy()

    pulumi_mocks.assert_res(
        "users-idp-Google",
        R.IDENTITY_PROVIDER,
        {"providerDetails": {"client_id": "overridden"}},
        partial=True,
    )


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


def test_resources_without_pool_creation_create_pool_first(pulumi_mocks):
    """Resources can be accessed independently - they trigger parent creation automatically."""
    pool = UserPool("users", usernames=["email"])
    google = pool.add_identity_provider(
        "google",
        provider_type="google",
        details={"client_id": "xxx", "client_secret": "yyy"},
    )

    # Only the IdP's resources are read: they trigger pool creation first via lazy evaluation
    @pulumi.runtime.test
    def deploy():
        return google.resources

    deploy()

    pulumi_mocks.assert_res_counts({R.USER_POOL: 1, R.IDENTITY_PROVIDER: 1})


# `parent=self` lives in ResourceOptions (invisible to input mocks), but it surfaces
# in the identity provider component's URN as the `stelvio:aws:UserPool$` parent segment.


@pulumi.runtime.test
def test_identity_provider_parented_to_user_pool(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    google = pool.add_identity_provider(
        "google",
        provider_type="google",
        details={"client_id": "xxx", "client_secret": "yyy"},
    )
    _ = google.resources

    def check(urn):
        assert_urn(urn, "stelvio:aws:UserPool", "stelvio:aws:IdentityProvider", "users-idp-Google")

    return google.urn.apply(check)
