from pathlib import Path

import pulumi
import pytest
from pulumi.runtime import set_mocks

from stelvio.aws.acm import AcmValidatedDomain
from stelvio.component import ComponentRegistry
from stelvio.config import AwsConfig
from stelvio.context import AppContext, _ContextStore
from stelvio.dns import Dns, Record

from ..pulumi_mocks import ACCOUNT_ID, DEFAULT_REGION, PulumiTestMocks, tid

# Test prefix - matching the pattern from other tests
TP = "test-test-"


class MockDnsRecord(Record):
    """Mock DNS record for testing"""

    def __init__(self, name: str, record_type: str, value: str):
        # Create a mock pulumi resource
        from unittest.mock import Mock

        mock_resource = Mock()
        mock_resource.name = name
        mock_resource.type = record_type
        mock_resource.content = value
        super().__init__(mock_resource)

    @property
    def name(self):
        return self._pulumi_resource.name

    @property
    def type(self):
        return self._pulumi_resource.type

    @property
    def value(self):
        return self._pulumi_resource.content


class MockDns(Dns):
    """Mock DNS provider for testing ACM functionality"""

    def __init__(self):
        self.created_records = []

    def create_record(
        self, resource_name: str, name: str, record_type: str, value: str, ttl: int = 1
    ) -> Record:
        """Create a mock DNS record"""
        import pulumi_cloudflare

        record = pulumi_cloudflare.Record(
            resource_name, zone_id="test-zone-id", name=name, type=record_type, content=value, ttl=ttl
        )
        mock_record = MockDnsRecord(name, record_type, value)
        mock_record._pulumi_resource = record
        self.created_records.append((resource_name, name, record_type, value, ttl))
        return mock_record

    def create_caa_record(
        self, resource_name: str, name: str, record_type: str, content: str, ttl: int = 1
    ) -> Record:
        """Create a mock CAA DNS record"""
        import pulumi_cloudflare

        record = pulumi_cloudflare.Record(
            resource_name, zone_id="test-zone-id", name=name, type=record_type, content=content, ttl=ttl
        )
        mock_record = MockDnsRecord(name, record_type, content)
        mock_record._pulumi_resource = record
        self.created_records.append((resource_name, name, record_type, content, ttl))
        return mock_record


def delete_files(directory: Path, filename: str):
    directory_path = directory
    for file_path in directory_path.rglob(filename):
        file_path.unlink()


@pytest.fixture
def mock_dns():
    return MockDns()


@pytest.fixture(autouse=True)
def project_cwd(monkeypatch, pytestconfig):
    rootpath = pytestconfig.rootpath
    test_project_dir = rootpath / "tests" / "aws" / "sample_test_project"
    monkeypatch.chdir(test_project_dir)
    yield test_project_dir
    delete_files(test_project_dir, "stlv_resources.py")


@pytest.fixture
def app_context_with_dns(mock_dns):
    """App context with DNS provider configured"""
    _ContextStore.clear()
    _ContextStore.set(
        AppContext(
            name="test",
            env="test",
            aws=AwsConfig(profile="default", region="us-east-1"),
            dns=mock_dns,
        )
    )
    yield mock_dns
    _ContextStore.clear()


@pytest.fixture
def pulumi_mocks():
    mocks = PulumiTestMocks()
    set_mocks(mocks)
    return mocks


@pytest.fixture
def component_registry():
    ComponentRegistry._instances.clear()
    ComponentRegistry._registered_names.clear()
    yield ComponentRegistry
    ComponentRegistry._instances.clear()
    ComponentRegistry._registered_names.clear()


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
    assert acm_domain.domain_name == domain_name


def test_acm_without_dns_provider(component_registry):
    """Test that ACM component requires DNS provider in context"""
    # Arrange - context without DNS provider
    _ContextStore.clear()
    _ContextStore.set(
        AppContext(
            name="test",
            env="test",
            aws=AwsConfig(profile="default", region="us-east-1"),
            dns=None,  # No DNS provider
        )
    )

    acm_domain = AcmValidatedDomain("test-cert", domain_name="api.example.com")

    # Act & Assert - This should fail when trying to access context().dns.create_caa_record
    with pytest.raises(AttributeError):
        _ = acm_domain.resources

    _ContextStore.clear()
