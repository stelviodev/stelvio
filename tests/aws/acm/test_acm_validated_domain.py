from dataclasses import replace

import pulumi
import pytest
from pulumi import ResourceOptions

from stelvio import context
from stelvio.aws.acm import AcmValidatedDomain
from stelvio.config import AwsConfig
from stelvio.context import AppContext, _ContextStore
from stelvio.dns import DnsProviderNotConfiguredError

from ...conftest import TP
from ..pulumi_mocks import ACCOUNT_ID, DEFAULT_REGION, R, tid

pytestmark = pytest.mark.usefixtures("project_cwd")


@pulumi.runtime.test
def test_acm_validated_domain_basic(pulumi_mocks, app_context_with_dns, component_registry):
    """Test basic ACM validated domain creation"""
    # Arrange
    domain_name = "api.example.com"
    acm_domain = AcmValidatedDomain("test-cert", domain_name=domain_name)

    # Act
    _ = acm_domain.resources

    # Assert
    def check_resources(_):
        # Check certificate was created
        certificates = pulumi_mocks.created_certificates(TP + "test-cert-certificate")
        assert len(certificates) == 1
        cert = certificates[0]
        assert cert.inputs["domainName"] == domain_name  # ACM uses camelCase
        assert cert.inputs["validationMethod"] == "DNS"

    # Use a simpler approach - just check that the certificate was created
    acm_domain.resources.certificate.id.apply(check_resources)


@pulumi.runtime.test
def test_acm_validated_domain_properties(pulumi_mocks, app_context_with_dns, component_registry):
    """Test ACM validated domain resource properties"""
    # Arrange
    domain_name = "test.example.com"
    acm_domain = AcmValidatedDomain("my-cert", domain_name=domain_name)

    # Act
    _ = acm_domain.resources

    # Assert
    def check_properties(args):
        cert_id, cert_arn, validation_arn = args

        # Check certificate properties
        assert cert_id == tid(TP + "my-cert-certificate")
        expected_cert_arn = (
            f"arn:aws:acm:{DEFAULT_REGION}:{ACCOUNT_ID}:certificate/"
            f"{tid(TP + 'my-cert-certificate')}"
        )
        assert cert_arn == expected_cert_arn

        # Check validation properties
        assert validation_arn == expected_cert_arn  # Validation should reference certificate ARN

    pulumi.Output.all(
        acm_domain.resources.certificate.id,
        acm_domain.resources.certificate.arn,
        acm_domain.resources.cert_validation.certificate_arn,
    ).apply(check_properties)


def test_acm_domain_name_property(app_context_with_dns, component_registry):
    """Test that domain_name property is accessible"""
    # Arrange & Act
    domain_name = "test.example.com"
    acm_domain = AcmValidatedDomain("test-cert", domain_name=domain_name)

    # Assert
    assert acm_domain._domain_name == domain_name


def test_acm_without_dns_provider(component_registry):
    """Test that ACM component requires DNS provider in context"""
    # Arrange - context without DNS provider
    _ContextStore.clear()
    _ContextStore.set(
        AppContext(
            name="test",
            env="test",
            aws=AwsConfig(profile="default", region="us-east-1"),
            home="aws",
            dns=None,  # No DNS provider
        )
    )

    acm_domain = AcmValidatedDomain("test-cert", domain_name="api.example.com")

    # Act & Assert - This should fail when trying to access context().dns.create_caa_record
    with pytest.raises(DnsProviderNotConfiguredError):
        _ = acm_domain.resources

    _ContextStore.clear()


@pulumi.runtime.test
def test_acm_validation_record_parented_to_acm_component(
    pulumi_mocks, app_context_with_dns, component_registry
):
    domain_name = "api.example.com"
    acm = AcmValidatedDomain("test-cert", domain_name=domain_name)
    r = acm.resources

    def check(urns):
        cert_urn, validation_urn, cert_validation_urn = urns
        for urn in (cert_urn, validation_urn, cert_validation_urn):
            assert "::stelvio:aws:AcmValidatedDomain$" in urn
        pulumi_mocks.assert_res(
            "test-cert-certificate-validation-record",
            R.CLOUDFLARE_RECORD,
            {
                "name": f"_test.{domain_name}",
                "type": "CNAME",
                "content": f"test-validation.{domain_name}",
                "ttl": 1.0,
            },
            partial=True,
        )

    return pulumi.Output.all(
        r.certificate.urn,
        r.validation_record.pulumi_resource.urn,
        r.cert_validation.urn,
    ).apply(check)


@pulumi.runtime.test
def test_acm_supports_legacy_dns_provider_without_opts(
    pulumi_mocks, app_context_with_dns, component_registry
):
    class LegacyDns:
        def create_record(self, resource_name, name, record_type, value, ttl=1):
            return app_context_with_dns.create_record(resource_name, name, record_type, value, ttl)

        def create_caa_record(self, resource_name, name, record_type, content, ttl=1):
            return app_context_with_dns.create_caa_record(
                resource_name, name, record_type, content, ttl
            )

    current_context = context()
    _ContextStore.clear()
    _ContextStore.set(replace(current_context, dns=LegacyDns()))

    acm = AcmValidatedDomain("legacy-cert", domain_name="api.example.com")
    r = acm.resources

    def check(_):
        assert len(app_context_with_dns.created_records) == 1
        pulumi_mocks.assert_res(
            "legacy-cert-certificate-validation-record",
            R.CLOUDFLARE_RECORD,
            {
                "name": "_test.api.example.com",
                "type": "CNAME",
                "content": "test-validation.api.example.com",
                "ttl": 1.0,
            },
            partial=True,
        )

    return r.cert_validation.id.apply(check)


@pulumi.runtime.test
def test_acm_passes_opts_to_kwargs_dns_adapter(
    pulumi_mocks, app_context_with_dns, component_registry
):
    received_opts: list[ResourceOptions | None] = []

    class KwargsDns:
        def create_record(self, resource_name, name, record_type, value, ttl=1, **kwargs):
            received_opts.append(kwargs.get("opts"))
            return app_context_with_dns.create_record(
                resource_name, name, record_type, value, ttl, **kwargs
            )

        def create_caa_record(self, resource_name, name, record_type, content, ttl=1, **kwargs):
            received_opts.append(kwargs.get("opts"))
            return app_context_with_dns.create_caa_record(
                resource_name, name, record_type, content, ttl, **kwargs
            )

    current_context = context()
    _ContextStore.clear()
    _ContextStore.set(replace(current_context, dns=KwargsDns()))

    acm = AcmValidatedDomain("kwargs-cert", domain_name="api.example.com")
    r = acm.resources

    def check(urn):
        assert len(received_opts) == 1
        assert received_opts[0] is not None
        assert received_opts[0].parent is acm
        assert "::stelvio:aws:AcmValidatedDomain$" in urn

    return r.validation_record.pulumi_resource.urn.apply(check)
