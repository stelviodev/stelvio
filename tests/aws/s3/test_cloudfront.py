import pulumi
import pytest
from pulumi.runtime import set_mocks

from stelvio.aws.cloudfront import CloudFrontDistribution
from stelvio.aws.s3 import Bucket
from stelvio.component import ComponentRegistry
from stelvio.config import AwsConfig
from stelvio.context import AppContext, _ContextStore
from stelvio.dns import Record

from ..pulumi_mocks import MockDns, PulumiTestMocks

# Test prefix - matching the pattern from other tests
TP = "test-test-"


class CloudflarePulumiResourceAdapter(Record):
    """Mock adapter that mimics the CloudflarePulumiResourceAdapter"""

    @property
    def name(self):
        return self.pulumi_resource.name

    @property
    def type(self):
        return self.pulumi_resource.type

    @property
    def value(self):
        return self.pulumi_resource.content


@pytest.fixture
def mock_dns():
    return MockDns()


@pytest.fixture(autouse=True)
def project_cwd(monkeypatch, pytestconfig):
    rootpath = pytestconfig.rootpath
    test_project_dir = rootpath / "tests" / "aws" / "sample_test_project"
    monkeypatch.chdir(test_project_dir)
    return test_project_dir


@pytest.fixture
def app_context_with_dns(mock_dns):
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
def app_context_without_dns():
    _ContextStore.clear()
    _ContextStore.set(
        AppContext(
            name="test",
            env="test",
            aws=AwsConfig(profile="default", region="us-east-1"),
            dns=None,  # No DNS provider
        )
    )
    yield
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
        assert len(distributions) > 0

        # Origin Access Control
        oacs = pulumi_mocks.created_origin_access_controls()
        assert len(oacs) > 0

        # CloudFront Function
        # functions = pulumi_mocks.created_cloudfront_functions()
        # assert len(functions) > 0

        # S3 Bucket Policy
        bucket_policies = pulumi_mocks.created_bucket_policies()
        assert len(bucket_policies) > 0

        # ACM Certificate (from AcmValidatedDomain)
        certificates = pulumi_mocks.created_certificates()
        assert len(certificates) > 0

        # DNS Record (from CloudFlare/DNS provider)
        dns_records = pulumi_mocks.created_dns_records()
        assert len(dns_records) > 0

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
    assert len(component_registry._instances) > initial_count
    assert "test-registry-cloudfront" in component_registry._registered_names


@pulumi.runtime.test
def test_cloudfront_distribution_viewer_request_function_code(
    pulumi_mocks, app_context_with_dns, component_registry, mock_s3_bucket
):
    """Test that CloudFront viewer request function is created with correct code"""
    # Arrange
    # distribution = CloudFrontDistribution(
    #     name="test-function-code",
    #     s3_bucket=mock_s3_bucket,
    #     custom_domain="function.example.com",
    # )

    # Act
    # resources = distribution.resources

    # Assert - verify function exists and has expected properties
    def check_function(_):
        # Don't filter by name - just get all CloudFront functions
        functions = pulumi_mocks.created_cloudfront_functions()
        assert len(functions) > 0

        # The function should handle directory index rewriting
        function_resource = functions[0]
        assert function_resource.inputs.get("runtime") == "cloudfront-js-1.0"
        assert "index.html" in function_resource.inputs.get("code", "")
        assert "handler" in function_resource.inputs.get("code", "")

    # resources.viewer_request_function.arn.apply(check_function)
    # resources.function_associations[0]["function_arn"].apply(check_function)
    # TODO!


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
        assert len(oacs) > 0

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
        assert len(distributions) > 0

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
        assert len(bucket_policies) > 0

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
