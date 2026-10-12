import pulumi
import pytest

from stelvio.aws.cognito.types import UserPoolConfig
from stelvio.aws.cognito.user_pool import UserPool
from stelvio.dns import DnsProviderNotConfiguredError

from ...conftest import TP
from ..conftest import assert_urn
from ..pulumi_mocks import ACCOUNT_ID, DEFAULT_REGION, R, tid

CERT_ARN = (
    f"arn:aws:acm:{DEFAULT_REGION}:{ACCOUNT_ID}:certificate/"
    f"{tid(f'{TP}users-acm-validated-domain-certificate')}"
)

# =========================================================================
# Domain validation
# =========================================================================


def test_empty_domain_rejected():
    with pytest.raises(ValueError, match="Domain cannot be empty"):
        UserPool("users", usernames=["email"], domain="")


def test_whitespace_domain_rejected():
    with pytest.raises(ValueError, match="Domain cannot be empty"):
        UserPool("users", usernames=["email"], domain="   ")


def test_uppercase_prefix_rejected():
    with pytest.raises(ValueError, match="Invalid prefix domain"):
        UserPool("users", usernames=["email"], domain="MyApp")


def test_prefix_starting_with_hyphen_rejected():
    with pytest.raises(ValueError, match="Invalid prefix domain"):
        UserPool("users", usernames=["email"], domain="-myapp")


def test_prefix_ending_with_hyphen_rejected():
    with pytest.raises(ValueError, match="Invalid prefix domain"):
        UserPool("users", usernames=["email"], domain="myapp-")


def test_custom_domain_empty_label_rejected():
    with pytest.raises(ValueError, match="Invalid custom domain"):
        UserPool("users", usernames=["email"], domain="auth..example.com")


def test_custom_domain_label_starting_with_hyphen_rejected():
    with pytest.raises(ValueError, match="Invalid custom domain"):
        UserPool("users", usernames=["email"], domain="-auth.example.com")


def test_custom_domain_label_with_a_newline_rejected():
    with pytest.raises(ValueError, match="Invalid custom domain"):
        UserPool("users", usernames=["email"], domain="auth\n.example.com")


def test_validation_via_config_dataclass():
    with pytest.raises(ValueError, match="Domain cannot be empty"):
        UserPoolConfig(usernames=["email"], domain="")


# =========================================================================
# No domain — verify no UserPoolDomain resource created
# =========================================================================


def test_no_domain_creates_no_domain_resource(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])

    @pulumi.runtime.test
    def deploy():
        return pool.resources

    deploy()

    pulumi_mocks.assert_no_res(R.USER_POOL_DOMAIN)


@pulumi.runtime.test
def test_no_domain_resources_are_none(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])

    def check(_):
        assert pool.resources.user_pool_domain is None
        assert pool.resources.acm_validated_domain is None
        assert pool.resources.domain_record is None

    pool.arn.apply(check)


# =========================================================================
# Prefix domain (no dots — Amazon Cognito domain)
# =========================================================================


def test_prefix_domain_creates_domain_resource(pulumi_mocks):
    pool = UserPool("users", usernames=["email"], domain="myapp-auth")

    @pulumi.runtime.test
    def deploy():
        return pool.resources

    deploy()

    pulumi_mocks.assert_res(
        "users-domain",
        R.USER_POOL_DOMAIN,
        {"domain": "myapp-auth", "userPoolId": tid(f"{TP}users")},
    )
    pulumi_mocks.assert_res_counts({R.USER_POOL: 1, R.USER_POOL_DOMAIN: 1})


def test_prefix_domain_references_pool_id(pulumi_mocks):
    pool = UserPool("users", usernames=["email"], domain="myapp-auth")

    @pulumi.runtime.test
    def deploy():
        return pool.resources

    deploy()

    pulumi_mocks.assert_res(
        "users-domain", R.USER_POOL_DOMAIN, {"userPoolId": tid(f"{TP}users")}, partial=True
    )


def test_prefix_domain_no_certificate_arn(pulumi_mocks):
    pool = UserPool("users", usernames=["email"], domain="myapp-auth")

    @pulumi.runtime.test
    def deploy():
        return pool.resources

    deploy()

    pulumi_mocks.assert_res(
        "users-domain", R.USER_POOL_DOMAIN, {"certificateArn": None}, partial=True
    )


def test_prefix_domain_no_acm_resources(pulumi_mocks):
    pool = UserPool("users", usernames=["email"], domain="myapp-auth")

    @pulumi.runtime.test
    def deploy():
        return pool.resources

    deploy()

    pulumi_mocks.assert_no_res(R.CERTIFICATE)


@pulumi.runtime.test
def test_prefix_domain_no_dns_record(pulumi_mocks):
    pool = UserPool("users", usernames=["email"], domain="myapp-auth")
    resources = pool.resources

    def check(_):
        assert resources.domain_record is None

    resources.user_pool_domain.domain.apply(check)


@pulumi.runtime.test
def test_prefix_domain_resources_populated(pulumi_mocks):
    pool = UserPool("users", usernames=["email"], domain="myapp-auth")
    resources = pool.resources

    def check(_):
        assert resources.user_pool_domain is not None
        assert resources.acm_validated_domain is None
        assert resources.domain_record is None

    resources.user_pool_domain.domain.apply(check)


# =========================================================================
# Custom domain (contains dots — requires DNS + ACM)
# =========================================================================


def test_custom_domain_creates_domain_resource(pulumi_mocks, app_context_with_dns):
    pool = UserPool("users", usernames=["email"], domain="auth.myapp.com")

    @pulumi.runtime.test
    def deploy():
        return pool.resources

    deploy()

    pulumi_mocks.assert_res(
        "users-domain", R.USER_POOL_DOMAIN, {"domain": "auth.myapp.com"}, partial=True
    )


def test_custom_domain_has_certificate_arn(pulumi_mocks, app_context_with_dns):
    pool = UserPool("users", usernames=["email"], domain="auth.myapp.com")

    @pulumi.runtime.test
    def deploy():
        return pool.resources

    deploy()

    pulumi_mocks.assert_res(
        "users-domain", R.USER_POOL_DOMAIN, {"certificateArn": CERT_ARN}, partial=True
    )


def test_custom_domain_creates_acm_certificate(pulumi_mocks, app_context_with_dns):
    pool = UserPool("users", usernames=["email"], domain="auth.myapp.com")

    @pulumi.runtime.test
    def deploy():
        return pool.resources

    deploy()

    pulumi_mocks.assert_res(
        "users-acm-validated-domain-certificate",
        R.CERTIFICATE,
        {"domainName": "auth.myapp.com"},
        partial=True,
    )


def test_custom_domain_creates_dns_record(pulumi_mocks, app_context_with_dns):
    pool = UserPool("users", usernames=["email"], domain="auth.myapp.com")

    @pulumi.runtime.test
    def deploy():
        return pool.resources

    deploy()

    pulumi_mocks.assert_res(
        "users-domain-record",
        R.CLOUDFLARE_RECORD,
        {
            "name": "auth.myapp.com",
            "type": "CNAME",
            "content": "d111111abcdef8.cloudfront.net",  # the domain's cloudfrontDistribution
            "ttl": 3600,
        },
        partial=True,
    )


def test_custom_domain_record_customize(pulumi_mocks, app_context_with_dns):
    pool = UserPool(
        "users",
        usernames=["email"],
        domain="auth.myapp.com",
        customize={"domain_record": {"ttl": 300}},
    )

    @pulumi.runtime.test
    def deploy():
        return pool.resources

    deploy()

    pulumi_mocks.assert_res(
        "users-domain-record",
        R.CLOUDFLARE_RECORD,
        {"name": "auth.myapp.com", "type": "CNAME", "ttl": 300},
        partial=True,
    )


@pulumi.runtime.test
def test_custom_domain_parented(pulumi_mocks, app_context_with_dns):
    pool = UserPool("users", usernames=["email"], domain="auth.myapp.com")
    record = pool.resources.domain_record
    acm = pool.resources.acm_validated_domain
    assert record is not None
    assert acm is not None

    def check(urns):
        record_urn, acm_urn = urns
        assert_urn(
            record_urn, "stelvio:aws:UserPool", R.CLOUDFLARE_RECORD, TP + "users-domain-record"
        )
        assert_urn(
            acm_urn,
            "stelvio:aws:UserPool",
            "stelvio:aws:AcmValidatedDomain",
            "users-acm-validated-domain",
        )

    return pulumi.Output.all(record.pulumi_resource.urn, acm.urn).apply(check)


@pulumi.runtime.test
def test_custom_domain_resources_populated(pulumi_mocks, app_context_with_dns):
    pool = UserPool("users", usernames=["email"], domain="auth.myapp.com")
    resources = pool.resources

    def check(_):
        assert resources.user_pool_domain is not None
        assert resources.acm_validated_domain is not None
        assert resources.domain_record is not None

    pulumi.Output.all(
        domain_id=resources.user_pool_domain.domain,
        cert_id=resources.acm_validated_domain.resources.cert_validation.id,
        dns_id=resources.domain_record.pulumi_resource.id,
    ).apply(check)


# =========================================================================
# Custom domain without DNS — error
# =========================================================================


def test_custom_domain_without_dns_raises_error():
    pool = UserPool("users", usernames=["email"], domain="auth.myapp.com")

    with pytest.raises(DnsProviderNotConfiguredError, match="requires a DNS provider"):
        pool._create_resources()


# =========================================================================
# Prefix domain does NOT require DNS
# =========================================================================


def test_prefix_domain_works_without_dns(pulumi_mocks):
    """Prefix domains don't need DNS — they use Amazon's cognito subdomain."""
    pool = UserPool("users", usernames=["email"], domain="myapp-auth")

    @pulumi.runtime.test
    def deploy():
        return pool.resources

    deploy()

    pulumi_mocks.assert_res(
        "users-domain", R.USER_POOL_DOMAIN, {"domain": "myapp-auth"}, partial=True
    )


# =========================================================================
# Domain via config dict / dataclass
# =========================================================================


def test_domain_via_config_dict(pulumi_mocks):
    pool = UserPool("users", config={"usernames": ["email"], "domain": "myapp-auth"})

    @pulumi.runtime.test
    def deploy():
        return pool.resources

    deploy()

    pulumi_mocks.assert_res(
        "users-domain", R.USER_POOL_DOMAIN, {"domain": "myapp-auth"}, partial=True
    )


# =========================================================================
# Customization
# =========================================================================


def test_customization_overrides_domain_config(pulumi_mocks):
    pool = UserPool(
        "users",
        usernames=["email"],
        domain="myapp-auth",
        customize={"user_pool_domain": {"managed_login_version": 2}},
    )

    @pulumi.runtime.test
    def deploy():
        return pool.resources

    deploy()

    pulumi_mocks.assert_res(
        "users-domain",
        R.USER_POOL_DOMAIN,
        {"domain": "myapp-auth", "managedLoginVersion": 2},
        partial=True,
    )


def test_customization_overrides_domain(pulumi_mocks):
    pool = UserPool(
        "users",
        usernames=["email"],
        domain="myapp-auth",
        customize={"user_pool_domain": {"domain": "override-auth"}},
    )

    @pulumi.runtime.test
    def deploy():
        return pool.resources

    deploy()

    pulumi_mocks.assert_res(
        "users-domain", R.USER_POOL_DOMAIN, {"domain": "override-auth"}, partial=True
    )
