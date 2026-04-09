import pulumi
import pytest

from stelvio.aws.api_gateway import Api
from stelvio.config import AwsConfig
from stelvio.context import AppContext, _ContextStore
from stelvio.dns import DnsProviderNotConfiguredError

from ...conftest import TP

pytestmark = pytest.mark.usefixtures("project_cwd")


@pulumi.runtime.test
def test_api_without_custom_domain(pulumi_mocks, app_context_with_dns, component_registry):
    """Test that API without custom domain works as before"""
    # Arrange
    api = Api("test-api-no-domain")
    api.route("GET", "/users", "functions/simple.handler")

    # Act
    _ = api.resources

    # Assert
    def check_resources(_):
        # Verify no custom domain resources were created
        assert len(pulumi_mocks.created_certificates()) == 0
        assert len(pulumi_mocks.created_domain_names()) == 0
        assert len(pulumi_mocks.created_base_path_mappings()) == 0

        # Verify normal API resources were created
        assert len(pulumi_mocks.created_rest_apis()) == 1
        assert len(pulumi_mocks.created_stages()) == 1

    api.resources.stage.id.apply(check_resources)


def test_api_custom_domain_validation_errors(app_context_with_dns, component_registry):
    """Test validation errors for custom domain configuration"""
    # Test non-string domain name - this should fail in __init__ before resources are created
    with pytest.raises(TypeError, match="Domain name must be a string"):  # noqa: PT012
        api = Api("test-api-1", domain_name=123)
        _ = api.resources

    # Test empty domain name - this should fail in __init__ before resources are created
    with pytest.raises(ValueError, match="Domain name cannot be empty"):  # noqa: PT012
        api = Api("test-api-2", domain_name="")
        _ = api.resources


@pulumi.runtime.test
def test_api_custom_domain_with_custom_domain(
    pulumi_mocks, app_context_with_dns, component_registry
):
    """Test that API with custom domain works as expected"""
    # Arrange
    mock_dns = app_context_with_dns
    api = Api("test-api-1", domain_name="api.example.com")
    api.route("GET", "/users", "functions/simple.handler")

    # Act
    _ = api.resources

    # Assert
    def check_resources(_):
        # Verify custom domain resources were created
        assert len(pulumi_mocks.created_certificates()) == 1
        assert len(pulumi_mocks.created_domain_names()) == 1

        # Verify certificate was created with correct properties
        certs = pulumi_mocks.created_certificates(TP + "test-api-1-acm-custom-domain-certificate")
        assert len(certs) == 1
        cert = certs[0]
        assert cert.inputs["domainName"] == "api.example.com", (
            "Certificate domainName should be 'api.example.com', got {}".format(
                cert.inputs["domainName"]
            )
        )

        # Verify normal API resources were created
        assert len(pulumi_mocks.created_rest_apis()) == 1
        assert len(pulumi_mocks.created_stages()) == 1

        # Verify DNS records were created via mock DNS
        assert len(mock_dns.created_records) == 2, (
            "Should have 2 DNS records (validation + API domain)"
        )

        # Verify that we have both types of records by checking resource names
        record_names = [r[0] for r in mock_dns.created_records]
        validation_records = [name for name in record_names if "validation-record" in name]
        api_records = [name for name in record_names if "custom-domain-record" in name]

        assert len(validation_records) == 1
        assert len(api_records) == 1

        # Check that API domain records have the correct domain name
        api_domain_records = [
            r for r in mock_dns.created_records if "custom-domain-record" in r[0]
        ]

        assert len(api_domain_records) == 1

        # For API domain records, the name should be the custom domain
        for record in api_domain_records:
            record_name = record[1]  # This is the name field
            assert record_name == "api.example.com", (
                f"API domain record should have name 'api.example.com', got {record_name}"
            )

    api.resources.base_path_mapping.id.apply(check_resources)


def test_api_custom_domain_without_dns_provider(component_registry):
    """Test that API with custom domain but no DNS provider raises error"""
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

    api = Api("test-api-3", domain_name="api.example.com")
    api.route("GET", "/users", "functions/simple.handler")

    # Act & Assert - This should fail when trying to access context().dns
    with pytest.raises(DnsProviderNotConfiguredError):
        _ = api.resources

    _ContextStore.clear()


@pulumi.runtime.test
def test_edge_endpoint_acm_uses_us_east_1_provider(
    pulumi_mocks, app_context_with_dns_eu_west, component_registry
):
    """Test that edge endpoint creates ACM certificate with a us-east-1 provider.

    CloudFront (used internally by edge endpoints) requires ACM certificates
    to be in us-east-1 regardless of the region used for other components.
    """
    api = Api("test-api-edge", domain_name="api.example.com", endpoint_type="edge")
    api.route("GET", "/users", "functions/simple.handler")
    _ = api.resources

    def check_resources(_):
        # Verify a us-east-1 provider was created
        providers = pulumi_mocks.created_providers()
        us_east_1_providers = [p for p in providers if p.inputs.get("region") == "us-east-1"]
        assert len(us_east_1_providers) == 1

        # Verify ACM certificate uses the us-east-1 provider
        certificates = pulumi_mocks.created_certificates()
        assert len(certificates) == 1
        cert = certificates[0]
        assert cert.provider is not None, (
            "ACM certificate should have an explicit provider for edge endpoints"
        )
        assert "stelvio-aws-us-east-1" in cert.provider

        # Verify certificate validation also uses the us-east-1 provider
        validations = pulumi_mocks.created_certificate_validations()
        assert len(validations) == 1
        assert validations[0].provider is not None
        assert "stelvio-aws-us-east-1" in validations[0].provider

        # Verify DomainName uses certificate_arn (edge attribute) and has endpoint config
        domain_names = pulumi_mocks.created_domain_names()
        assert len(domain_names) == 1
        dn = domain_names[0]
        assert "certificateArn" in dn.inputs, "Edge endpoint DomainName should use certificate_arn"
        assert dn.inputs.get("endpointConfiguration", {}).get("types") == "EDGE"

    api.resources.base_path_mapping.id.apply(check_resources)


@pulumi.runtime.test
def test_regional_endpoint_acm_uses_default_provider(
    pulumi_mocks, app_context_with_dns_eu_west, component_registry
):
    """Test that regional endpoint creates ACM certificate without a special provider.

    Regional endpoints require ACM certificates in the same region as the API,
    so the default provider (user's configured region) should be used.
    """
    api = Api("test-api-regional", domain_name="api.example.com", endpoint_type="regional")
    api.route("GET", "/users", "functions/simple.handler")
    _ = api.resources

    def check_resources(_):
        # Verify no us-east-1 provider was created — regional ACM stays in the API's region
        providers = pulumi_mocks.created_providers()
        us_east_1_providers = [p for p in providers if p.name.startswith("stelvio-aws-us-east-1")]
        assert len(us_east_1_providers) == 0, (
            "Regional endpoint should not create a us-east-1 provider"
        )

        # Verify ACM certificate does not use a us-east-1 provider
        certificates = pulumi_mocks.created_certificates()
        assert len(certificates) == 1
        assert "stelvio-aws-us-east-1" not in (certificates[0].provider or ""), (
            "Regional ACM certificate should not use us-east-1 provider"
        )

        # Verify DomainName uses regional_certificate_arn and has endpoint config
        domain_names = pulumi_mocks.created_domain_names()
        assert len(domain_names) == 1
        dn = domain_names[0]
        assert "regionalCertificateArn" in dn.inputs, (
            "Regional endpoint DomainName should use regional_certificate_arn"
        )

    api.resources.base_path_mapping.id.apply(check_resources)


@pulumi.runtime.test
def test_edge_endpoint_acm_skips_provider_when_already_us_east_1(
    pulumi_mocks, app_context_with_dns, component_registry
):
    """Test that no redundant us-east-1 provider is created when region is already us-east-1.

    When the user's configured region is us-east-1, edge endpoint ACM certificates
    can use the default provider — no explicit provider is needed.
    """
    api = Api("test-api-edge-skip", domain_name="api.example.com", endpoint_type="edge")
    api.route("GET", "/users", "functions/simple.handler")
    _ = api.resources

    def check_resources(_):
        # Verify no extra us-east-1 provider was created (default is already us-east-1)
        providers = pulumi_mocks.created_providers()
        us_east_1_providers = [p for p in providers if p.name.startswith("stelvio-aws-us-east-1")]
        assert len(us_east_1_providers) == 0, (
            "Should not create a separate us-east-1 provider when region is already us-east-1"
        )

        # Verify ACM certificate does not use a separate us-east-1 provider
        certificates = pulumi_mocks.created_certificates()
        assert len(certificates) == 1
        assert "stelvio-aws-us-east-1" not in (certificates[0].provider or ""), (
            "ACM certificate should use default provider when region is already us-east-1"
        )

        # Verify certificate validation also does not use a separate us-east-1 provider
        validations = pulumi_mocks.created_certificate_validations()
        assert len(validations) == 1
        assert "stelvio-aws-us-east-1" not in (validations[0].provider or ""), (
            "ACM cert validation should use default provider when region is already us-east-1"
        )

        # Verify DomainName still uses certificate_arn for edge and has correct endpoint config
        domain_names = pulumi_mocks.created_domain_names()
        assert len(domain_names) == 1
        dn = domain_names[0]
        assert "certificateArn" in dn.inputs, "Edge endpoint DomainName should use certificate_arn"
        assert dn.inputs.get("endpointConfiguration", {}).get("types") == "EDGE"

    api.resources.base_path_mapping.id.apply(check_resources)
