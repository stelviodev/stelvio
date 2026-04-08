import pulumi
import pytest

from stelvio.aws.cognito.types import UserPoolConfig
from stelvio.aws.cognito.user_pool import UserPool
from stelvio.dns import DnsProviderNotConfiguredError

from ...conftest import TP

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


def test_validation_via_config_dataclass():
    with pytest.raises(ValueError, match="Domain cannot be empty"):
        UserPoolConfig(usernames=["email"], domain="")


# =========================================================================
# No domain — verify no UserPoolDomain resource created
# =========================================================================


@pulumi.runtime.test
def test_no_domain_creates_no_domain_resource(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])

    def check(_):
        domains = pulumi_mocks.created_user_pool_domains()
        assert len(domains) == 0

    pool.arn.apply(check)


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


@pulumi.runtime.test
def test_prefix_domain_creates_domain_resource(pulumi_mocks):
    pool = UserPool("users", usernames=["email"], domain="myapp-auth")
    resources = pool.resources

    def check(_):
        domain = pulumi_mocks.assert_user_pool_domain_created(TP + "users-domain")
        assert domain.inputs["domain"] == "myapp-auth"

    resources.user_pool_domain.domain.apply(check)


@pulumi.runtime.test
def test_prefix_domain_references_pool_id(pulumi_mocks):
    pool = UserPool("users", usernames=["email"], domain="myapp-auth")
    resources = pool.resources

    def check(_):
        domain = pulumi_mocks.assert_user_pool_domain_created(TP + "users-domain")
        assert domain.inputs.get("userPoolId") is not None

    resources.user_pool_domain.domain.apply(check)


@pulumi.runtime.test
def test_prefix_domain_no_certificate_arn(pulumi_mocks):
    pool = UserPool("users", usernames=["email"], domain="myapp-auth")
    resources = pool.resources

    def check(_):
        domain = pulumi_mocks.assert_user_pool_domain_created(TP + "users-domain")
        assert domain.inputs.get("certificateArn") is None

    resources.user_pool_domain.domain.apply(check)


@pulumi.runtime.test
def test_prefix_domain_no_acm_resources(pulumi_mocks):
    pool = UserPool("users", usernames=["email"], domain="myapp-auth")
    resources = pool.resources

    def check(_):
        certificates = pulumi_mocks.created_certificates()
        assert len(certificates) == 0

    resources.user_pool_domain.domain.apply(check)


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


@pulumi.runtime.test
def test_custom_domain_creates_domain_resource(pulumi_mocks, app_context_with_dns):
    pool = UserPool("users", usernames=["email"], domain="auth.myapp.com")
    resources = pool.resources

    def check(_):
        domain = pulumi_mocks.assert_user_pool_domain_created(TP + "users-domain")
        assert domain.inputs["domain"] == "auth.myapp.com"

    pulumi.Output.all(
        domain_id=resources.user_pool_domain.domain,
        cert_id=resources.acm_validated_domain.resources.cert_validation.id,
        dns_id=resources.domain_record.pulumi_resource.id,
    ).apply(check)


@pulumi.runtime.test
def test_custom_domain_has_certificate_arn(pulumi_mocks, app_context_with_dns):
    pool = UserPool("users", usernames=["email"], domain="auth.myapp.com")
    resources = pool.resources

    def check(_):
        domain = pulumi_mocks.assert_user_pool_domain_created(TP + "users-domain")
        assert domain.inputs.get("certificateArn") is not None

    pulumi.Output.all(
        domain_id=resources.user_pool_domain.domain,
        cert_id=resources.acm_validated_domain.resources.cert_validation.id,
    ).apply(check)


@pulumi.runtime.test
def test_custom_domain_creates_acm_certificate(pulumi_mocks, app_context_with_dns):
    pool = UserPool("users", usernames=["email"], domain="auth.myapp.com")
    resources = pool.resources

    def check(_):
        certs = [
            cert
            for cert in pulumi_mocks.created_certificates()
            if cert.inputs.get("domainName") == "auth.myapp.com"
        ]
        assert len(certs) == 1

    pulumi.Output.all(
        domain_id=resources.user_pool_domain.domain,
        cert_id=resources.acm_validated_domain.resources.cert_validation.id,
    ).apply(check)


@pulumi.runtime.test
def test_custom_domain_creates_dns_record(pulumi_mocks, app_context_with_dns):
    pool = UserPool("users", usernames=["email"], domain="auth.myapp.com")
    resources = pool.resources

    def check(_):
        domain_records = [
            record
            for record in app_context_with_dns.created_records
            if record[0] == f"{TP}users-domain-record"
        ]
        assert len(domain_records) == 1
        _, dns_name, record_type, _, ttl = domain_records[0]
        assert dns_name == "auth.myapp.com"
        assert record_type == "CNAME"
        assert ttl == 3600

    pulumi.Output.all(
        domain_id=resources.user_pool_domain.domain,
        dns_id=resources.domain_record.pulumi_resource.id,
    ).apply(check)


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


@pulumi.runtime.test
def test_prefix_domain_works_without_dns(pulumi_mocks):
    """Prefix domains don't need DNS — they use Amazon's cognito subdomain."""
    pool = UserPool("users", usernames=["email"], domain="myapp-auth")
    resources = pool.resources

    def check(_):
        domain = pulumi_mocks.assert_user_pool_domain_created(TP + "users-domain")
        assert domain.inputs["domain"] == "myapp-auth"

    resources.user_pool_domain.domain.apply(check)


# =========================================================================
# Domain via config dict / dataclass
# =========================================================================


@pulumi.runtime.test
def test_domain_via_config_dict(pulumi_mocks):
    pool = UserPool("users", config={"usernames": ["email"], "domain": "myapp-auth"})
    resources = pool.resources

    def check(_):
        pulumi_mocks.assert_user_pool_domain_created(TP + "users-domain")

    resources.user_pool_domain.domain.apply(check)


# =========================================================================
# Customization
# =========================================================================


@pulumi.runtime.test
def test_customization_overrides_domain_config(pulumi_mocks):
    pool = UserPool(
        "users",
        usernames=["email"],
        domain="myapp-auth",
        customize={"user_pool_domain": {"managed_login_version": 2}},
    )
    resources = pool.resources

    def check(_):
        domain = pulumi_mocks.assert_user_pool_domain_created(TP + "users-domain")
        assert domain.inputs["domain"] == "myapp-auth"
        assert domain.inputs["managedLoginVersion"] == 2

    resources.user_pool_domain.domain.apply(check)


@pulumi.runtime.test
def test_customization_overrides_domain(pulumi_mocks):
    pool = UserPool(
        "users",
        usernames=["email"],
        domain="myapp-auth",
        customize={"user_pool_domain": {"domain": "override-auth"}},
    )
    resources = pool.resources

    def check(_):
        domain = pulumi_mocks.assert_user_pool_domain_created(TP + "users-domain")
        assert domain.inputs["domain"] == "override-auth"

    resources.user_pool_domain.domain.apply(check)
