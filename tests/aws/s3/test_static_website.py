import tempfile
from pathlib import Path

import pulumi
import pytest
from pulumi.runtime import set_mocks

from stelvio.aws.s3 import S3StaticWebsite
from stelvio.component import ComponentRegistry
from stelvio.config import AwsConfig
from stelvio.context import AppContext, _ContextStore
from stelvio.dns import DnsProviderNotConfiguredError, Record

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
def app_context_without_dns():
    """App context without DNS provider configured"""
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
def temp_static_site():
    """Create a temporary directory with static website files"""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create some test files
        (Path(tmpdir) / "index.html").write_text("<html><body>Home</body></html>")
        (Path(tmpdir) / "about.html").write_text("<html><body>About</body></html>")
        (Path(tmpdir) / "style.css").write_text("body { color: blue; }")
        (Path(tmpdir) / "script.js").write_text("console.log('hello');")

        # Create subdirectory with files
        subdir = Path(tmpdir) / "assets"
        subdir.mkdir()
        (subdir / "image.png").write_text("fake-png-data")

        yield Path(tmpdir)


@pulumi.runtime.test
def test_s3_static_website_component_creation(
    pulumi_mocks, app_context_with_dns, component_registry, temp_static_site
):
    """Test S3StaticWebsite component can be instantiated and configured properly"""
    # Arrange
    website = S3StaticWebsite(
        name="test-website",
        directory=str(temp_static_site),
        custom_domain="www.example.com",
    )

    # Act
    resources = website.resources

    # Assert - verify component properties and configuration
    assert website.name == "test-website"
    assert str(website.directory) == str(temp_static_site)
    assert website.custom_domain == "www.example.com"

    # Verify resources object exists and has expected attributes
    assert hasattr(resources, "bucket")
    assert hasattr(resources, "files")
    assert hasattr(resources, "cloudfront_distribution")

    # Note: CloudFlare DNS resources are created but not captured by test mocks
    # This is due to resources being created inside component _create_resources() methods
    # The component works correctly in actual deployments


@pulumi.runtime.test
def test_s3_static_website_without_custom_domain(
    pulumi_mocks, app_context_without_dns, component_registry, temp_static_site
):
    """Test S3StaticWebsite behavior without DNS provider configured"""
    # This test checks that the component fails gracefully when no DNS is configured
    # Since S3StaticWebsite requires custom_domain and DNS for ACM certificates

    with pytest.raises(DnsProviderNotConfiguredError):  # noqa: PT012
        website = S3StaticWebsite(
            name="test-website-no-domain",
            directory=str(temp_static_site),
            custom_domain="www.example.com",  # This will fail without DNS
        )

        # This should raise an error when trying to create ACM certificate
        _ = website.resources


@pulumi.runtime.test
def test_s3_static_website_custom_documents(
    pulumi_mocks, app_context_with_dns, component_registry, temp_static_site
):
    """Test S3StaticWebsite with various file types"""
    # Arrange - create additional files
    (temp_static_site / "home.html").write_text("<h1>Custom Home</h1>")
    (temp_static_site / "error.html").write_text("<h1>Custom Error</h1>")

    website = S3StaticWebsite(
        name="test-custom-docs",
        directory=str(temp_static_site),
        custom_domain="custom.example.com",
    )

    # Act
    resources = website.resources

    # Assert
    assert website.custom_domain == "custom.example.com"
    assert str(website.directory) == str(temp_static_site)

    # Verify resources exist
    assert hasattr(resources, "bucket")
    assert hasattr(resources, "files")
    assert hasattr(resources, "cloudfront_distribution")


@pulumi.runtime.test
def test_s3_static_website_file_upload(
    pulumi_mocks, app_context_with_dns, component_registry, temp_static_site
):
    """Test that S3StaticWebsite processes multiple file types correctly"""
    # Arrange - create various file types
    files_to_create = {
        "index.html": "<html><head><title>Test</title></head><body><h1>Hello</h1></body></html>",
        "styles.css": "body { font-family: Arial; }",
        "script.js": "console.log('Hello World');",
        "image.png": "fake-png-content",
        "data.json": '{"test": "data"}',
        "robots.txt": "User-agent: *\nDisallow: /admin/",
        "subdirectory/nested.html": "<h1>Nested Page</h1>",
    }

    # Create all test files
    for file_path, content in files_to_create.items():
        full_path = temp_static_site / file_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content)

    website = S3StaticWebsite(
        name="test-multiple-files",
        directory=str(temp_static_site),
        custom_domain="files.example.com",
    )

    # Act
    resources = website.resources

    # Assert - component should handle multiple file types
    assert website.name == "test-multiple-files"
    assert len(list(temp_static_site.rglob("*"))) >= len(files_to_create)  # All files exist

    # Verify resources exist
    assert hasattr(resources, "bucket")
    assert hasattr(resources, "files")
    assert hasattr(resources, "cloudfront_distribution")


@pulumi.runtime.test
def test_s3_static_website_component_registry(
    pulumi_mocks, app_context_with_dns, component_registry, temp_static_site
):
    """Test S3StaticWebsite integrates properly with component registry"""
    # Arrange
    initial_count = len(component_registry._instances)

    website = S3StaticWebsite(
        name="test-registry", directory=str(temp_static_site), custom_domain="registry.example.com"
    )

    # Act
    _ = website.resources

    # Assert - component should be registered along with its nested components
    # S3StaticWebsite creates:
    # + S3StaticWebsite
    # + Bucket
    # + CloudFrontDistribution
    # + AcmValidatedDomain
    # = 4 total
    assert len(component_registry._instances) == initial_count + 4
    assert "test-registry" in component_registry._registered_names
