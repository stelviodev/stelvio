import pulumi
import pytest

from stelvio.aws.acm import AcmValidatedDomain
from stelvio.config import AwsConfig
from stelvio.context import AppContext, _ContextStore
from stelvio.dns import DnsProviderNotConfiguredError

from ...conftest import TP
from ..pulumi_mocks import ACCOUNT_ID, DEFAULT_REGION, tid

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
