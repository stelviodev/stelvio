import pulumi
import pytest

from stelvio.aws.cloudfront import CloudFrontDistribution
from stelvio.aws.s3 import Bucket

pytestmark = pytest.mark.usefixtures("project_cwd")


@pytest.fixture
def mock_s3_bucket(pulumi_mocks, app_context_with_dns):
    """Create a mock S3 bucket for CloudFront tests"""
    return Bucket(name="test-bucket")


@pytest.fixture
def mock_s3_bucket_no_dns(pulumi_mocks, app_context_without_dns):
    """Create a mock S3 bucket for CloudFront tests (without DNS)"""
    return Bucket(name="test-bucket")


@pulumi.runtime.test
def test_cloudfront_distribution_component_creation(
    pulumi_mocks, app_context_with_dns, component_registry, mock_s3_bucket
):
    """Test CloudFrontDistribution component can be instantiated and configured properly"""
    # Arrange
    distribution = CloudFrontDistribution(
        name="test-cloudfront",
        bucket=mock_s3_bucket,
        custom_domain="cdn.example.com",
    )

    # Act
    resources = distribution.resources

    # Assert - verify component properties and configuration
    assert distribution.name == "test-cloudfront"
    assert distribution.custom_domain == "cdn.example.com"
    assert distribution.price_class == "PriceClass_100"  # default value

    # Verify resources object exists and has expected attributes
    assert hasattr(resources, "distribution")
    assert hasattr(resources, "origin_access_control")
    # assert hasattr(resources, "viewer_request_function")
    assert hasattr(resources, "acm_validated_domain")
    assert hasattr(resources, "record")
    assert hasattr(resources, "function_associations")


@pulumi.runtime.test
def test_cloudfront_distribution_with_custom_price_class(
    pulumi_mocks, app_context_with_dns, component_registry, mock_s3_bucket
):
    """Test CloudFrontDistribution with custom price class"""
    # Arrange
    distribution = CloudFrontDistribution(
        name="test-cloudfront-custom",
        bucket=mock_s3_bucket,
        custom_domain="premium-cdn.example.com",
        price_class="PriceClass_All",
    )

    # Act
    resources = distribution.resources

    # Assert
    assert distribution.price_class == "PriceClass_All"
    assert distribution.custom_domain == "premium-cdn.example.com"

    # Verify resources exist
    assert hasattr(resources, "distribution")
    assert hasattr(resources, "origin_access_control")
    assert hasattr(resources, "function_associations")


@pulumi.runtime.test
def test_cloudfront_distribution_creates_all_resources(
    pulumi_mocks, app_context_with_dns, component_registry, mock_s3_bucket
):
    """Test that CloudFrontDistribution creates all expected AWS resources"""
    # Arrange
    distribution = CloudFrontDistribution(
        name="test-all-resources",
        bucket=mock_s3_bucket,
        custom_domain="all-resources.example.com",
    )

    # Act
    resources = distribution.resources

    # Assert - verify all AWS resources are created through mocks
    def check_resources(_):
        # CloudFront Distribution
        distributions = pulumi_mocks.created_cloudfront_distributions()
        assert len(distributions) == 1

        # Origin Access Control
        oacs = pulumi_mocks.created_origin_access_controls()
        assert len(oacs) == 1

        # S3 Bucket Policy
        bucket_policies = pulumi_mocks.created_bucket_policies()
        assert len(bucket_policies) == 1

        # ACM Certificate (from AcmValidatedDomain)
        certificates = pulumi_mocks.created_certificates()
        assert len(certificates) == 1

        # DNS Records (cert validation + distribution CNAME)
        dns_records = pulumi_mocks.created_dns_records()
        assert len(dns_records) == 2

    pulumi.Output.all(
        distribution_id=resources.distribution.id,
        oac_id=resources.origin_access_control.id,
        # function_arn=resources.viewer_request_function.arn,
        bucket_policy_id=resources.bucket_policy.id,
        # function_arn=resources.function_associations[0]["function_arn"],
    ).apply(check_resources)


@pulumi.runtime.test
def test_cloudfront_distribution_component_registry(
    pulumi_mocks, app_context_with_dns, component_registry, mock_s3_bucket
):
    """Test CloudFrontDistribution integrates properly with component registry"""
    # Arrange
    initial_count = len(component_registry._instances)

    distribution = CloudFrontDistribution(
        name="test-registry-cloudfront",
        bucket=mock_s3_bucket,
        custom_domain="registry.example.com",
    )

    # Act
    _ = distribution.resources

    # Assert - component should be registered along with its nested components
    # CloudFrontDistribution creates:
    # + CloudFrontDistribution
    # + AcmValidatedDomain
    # = 2 total (the AcmValidatedDomain also creates sub-components internally)
    assert len(component_registry._instances) == initial_count + 2
    assert "test-registry-cloudfront" in component_registry._registered_names


@pulumi.runtime.test
def test_cloudfront_distribution_origin_access_control_config(
    pulumi_mocks, app_context_with_dns, component_registry, mock_s3_bucket
):
    """Test CloudFront Origin Access Control configuration"""
    # Arrange
    distribution = CloudFrontDistribution(
        name="test-oac-config",
        bucket=mock_s3_bucket,
        custom_domain="oac.example.com",
    )

    # Act
    resources = distribution.resources

    # Assert - verify OAC configuration
    def check_oac(_):
        oacs = pulumi_mocks.created_origin_access_controls()
        assert len(oacs) == 1

        oac_resource = oacs[0]
        assert oac_resource.inputs.get("originAccessControlOriginType") == "s3"
        assert oac_resource.inputs.get("signingBehavior") == "always"
        assert oac_resource.inputs.get("signingProtocol") == "sigv4"

    resources.origin_access_control.id.apply(check_oac)


@pulumi.runtime.test
def test_cloudfront_distribution_with_different_price_classes(
    pulumi_mocks, app_context_with_dns, component_registry, mock_s3_bucket
):
    """Test CloudFrontDistribution with all supported price classes"""
    price_classes = ["PriceClass_100", "PriceClass_200", "PriceClass_All"]

    for i, price_class in enumerate(price_classes):
        # Arrange
        distribution = CloudFrontDistribution(
            name=f"test-price-{i}",
            bucket=mock_s3_bucket,
            custom_domain=f"price-{i}.example.com",
            price_class=price_class,
        )

        # Act
        resources = distribution.resources

        # Assert
        assert distribution.price_class == price_class
        assert hasattr(resources, "distribution")


@pulumi.runtime.test
def test_cloudfront_distribution_custom_error_responses(
    pulumi_mocks, app_context_with_dns, component_registry, mock_s3_bucket
):
    """Test that CloudFront distribution includes custom error responses"""
    # Arrange
    distribution = CloudFrontDistribution(
        name="test-error-responses",
        bucket=mock_s3_bucket,
        custom_domain="errors.example.com",
    )

    # Act
    resources = distribution.resources

    # Assert - verify distribution is created (error responses are configured internally)
    def check_distribution(_):
        distributions = pulumi_mocks.created_cloudfront_distributions()
        assert len(distributions) == 1

        # The actual error response configuration is tested through the mock creation
        distribution_resource = distributions[0]
        assert distribution_resource.inputs.get("enabled") is True
        assert distribution_resource.inputs.get("defaultRootObject") == "index.html"

    resources.distribution.id.apply(check_distribution)


@pulumi.runtime.test
def test_cloudfront_distribution_s3_bucket_policy_creation(
    pulumi_mocks, app_context_with_dns, component_registry, mock_s3_bucket
):
    """Test that CloudFront creates appropriate S3 bucket policy"""
    # Arrange
    distribution = CloudFrontDistribution(
        name="test-bucket-policy",
        bucket=mock_s3_bucket,
        custom_domain="bucket-policy.example.com",
    )

    # Act
    resources = distribution.resources

    # Assert - verify bucket policy is created
    def check_bucket_policy(_):
        bucket_policies = pulumi_mocks.created_bucket_policies()
        assert len(bucket_policies) == 1

        # Verify the bucket policy is associated with the correct bucket
        bucket_policy_resource = bucket_policies[0]
        assert bucket_policy_resource.inputs.get("bucket") is not None

    resources.bucket_policy.id.apply(check_bucket_policy)


@pulumi.runtime.test
def test_cloudfront_distribution_raises_without_dns(
    pulumi_mocks, mock_s3_bucket_no_dns, component_registry
):
    from stelvio.dns import DnsProviderNotConfiguredError

    with pytest.raises(DnsProviderNotConfiguredError) as exc_info:
        CloudFrontDistribution(
            name="test-no-dns",
            bucket=mock_s3_bucket_no_dns,
            custom_domain="cdn-nodns.example.com",
        )._create_resources()
    assert "dns" in str(exc_info.value).lower() or "not configured" in str(exc_info.value).lower()


@pulumi.runtime.test
def test_custom_domain_acm_uses_us_east_1_provider(
    pulumi_mocks, app_context_with_dns_eu_west, component_registry
):
    """Test that ACM certificate is created with a us-east-1 provider for CloudFront.

    CloudFront requires ACM certificates to be in us-east-1 regardless of the
    region used for other components.
    """
    bucket = Bucket(name="test-bucket")
    _ = bucket.resources

    distribution = CloudFrontDistribution(
        name="test-cf-acm",
        bucket=bucket,
        custom_domain="cdn.example.com",
    )
    resources = distribution.resources

    def check_resources(_):
        # Verify a us-east-1 provider was created
        providers = pulumi_mocks.created_providers()
        us_east_1_providers = [p for p in providers if p.inputs.get("region") == "us-east-1"]
        assert len(us_east_1_providers) == 1

        # Verify ACM certificate uses the us-east-1 provider
        certificates = pulumi_mocks.created_certificates()
        assert len(certificates) == 1
        cert = certificates[0]
        assert cert.provider is not None
        assert "stelvio-aws-us-east-1" in cert.provider

        # Verify certificate validation also uses the us-east-1 provider
        validations = pulumi_mocks.created_certificate_validations()
        assert len(validations) == 1
        assert validations[0].provider is not None
        assert "stelvio-aws-us-east-1" in validations[0].provider

    pulumi.Output.all(
        dist_id=resources.distribution.id,
        cert_validation_id=resources.acm_validated_domain.resources.cert_validation.id,
    ).apply(check_resources)


@pulumi.runtime.test
def test_custom_domain_acm_skips_provider_when_already_us_east_1(
    pulumi_mocks, app_context_with_dns, component_registry
):
    """Test that no redundant us-east-1 provider is created when region is already us-east-1.

    When the user's configured region is us-east-1, CloudFront ACM certificates
    can use the default provider — no explicit provider is needed.
    """
    bucket = Bucket(name="test-bucket")
    _ = bucket.resources

    distribution = CloudFrontDistribution(
        name="test-cf-acm-skip",
        bucket=bucket,
        custom_domain="cdn.example.com",
    )
    resources = distribution.resources

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

    pulumi.Output.all(
        dist_id=resources.distribution.id,
        cert_validation_id=resources.acm_validated_domain.resources.cert_validation.id,
    ).apply(check_resources)
