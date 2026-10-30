"""Tests for the customize parameter across all components.

These tests verify that the customize parameter is properly passed through to the
underlying Pulumi resources for each component.
"""

import tempfile
from pathlib import Path
from unittest.mock import Mock

import pulumi
import pytest

from stelvio.aws.api_gateway import HttpApi, RestApi, WebsocketApi
from stelvio.aws.cloudfront import CloudFrontDistribution
from stelvio.aws.cloudfront.router import Router
from stelvio.aws.cron import Cron
from stelvio.aws.dynamo_db import DynamoTable
from stelvio.aws.email import Email
from stelvio.aws.function import Function
from stelvio.aws.layer import Layer
from stelvio.aws.queue import Queue
from stelvio.aws.s3 import Bucket, S3StaticWebsite
from stelvio.aws.topic import Topic
from stelvio.config import AwsConfig
from stelvio.context import AppContext, _ContextStore
from stelvio.dns import Dns

from ..conftest import TP
from .conftest import create_app_context_with_global_customize
from .pulumi_mocks import MockDns, R, tid, tn


@pytest.fixture
def mock_dns():
    dns = Mock(spec=Dns)
    dns.create_record.return_value = Mock()
    return dns


@pytest.fixture
def app_context_with_dns():
    """Fixture that provides an app context with DNS configured."""
    _ContextStore.clear()
    mock_dns = MockDns()
    _ContextStore.set(
        AppContext(
            name="test",
            env="test",
            aws=AwsConfig(profile="default", region="us-east-1"),
            home="aws",
            dns=mock_dns,
        )
    )
    yield mock_dns
    _ContextStore.clear()
    _ContextStore.set(
        AppContext(
            name="test",
            env="test",
            aws=AwsConfig(profile="default", region="us-east-1"),
            home="aws",
        )
    )


# =============================================================================
# S3 Bucket Customization Tests
# =============================================================================


def test_bucket_customize_bucket_resource(pulumi_mocks, project_cwd):
    """Test that customize parameter is applied to S3 bucket resource."""
    # Arrange
    bucket = Bucket(
        "my-bucket",
        customize={
            "bucket": {
                "force_destroy": True,
                "tags": {"Environment": "test"},
            }
        },
    )

    # Act
    @pulumi.runtime.test
    def deploy():
        return bucket.resources

    deploy()

    # Assert
    pulumi_mocks.assert_res(
        "my-bucket",
        R.BUCKET,
        {"forceDestroy": True, "tags": {"Environment": "test"}},
        partial=True,
    )


def test_bucket_customize_public_access_block(pulumi_mocks, project_cwd):
    """Test that customize parameter is applied to public access block resource."""
    # Arrange - private bucket (access=None) with customize
    bucket = Bucket(
        "my-bucket",
        customize={
            "public_access_block": {
                "block_public_acls": False,  # Override default True
            }
        },
    )

    # Act
    @pulumi.runtime.test
    def deploy():
        return bucket.resources

    deploy()

    # Assert - customization overrides the default, other defaults remain
    pulumi_mocks.assert_res(
        "my-bucket-pab",
        R.BUCKET_PUBLIC_ACCESS_BLOCK,
        {"blockPublicAcls": False, "blockPublicPolicy": True},
        partial=True,
    )


# =============================================================================
# Function Customization Tests
# =============================================================================


def test_function_customize_function_resource(pulumi_mocks, project_cwd):
    """Test that customize parameter is applied to Lambda function resource."""
    # Arrange
    fn = Function(
        "my-function",
        handler="functions/simple.handler",
        customize={
            "function": {
                "reserved_concurrent_executions": 10,
            }
        },
    )

    # Act
    @pulumi.runtime.test
    def deploy():
        return fn.resources

    deploy()

    # Assert
    pulumi_mocks.assert_res(
        "my-function", R.FUNCTION, {"reservedConcurrentExecutions": 10}, partial=True
    )


def test_function_customize_function_url_resource(pulumi_mocks, project_cwd):
    fn = Function(
        "my-function",
        handler="functions/simple.handler",
        url="public",
        customize={"function_url": {"invoke_mode": "RESPONSE_STREAM"}},
    )

    @pulumi.runtime.test
    def deploy():
        return fn.resources

    deploy()

    pulumi_mocks.assert_res(
        "my-function-url",
        R.FUNCTION_URL,
        {
            "functionName": tn(TP + "my-function"),
            "authorizationType": "NONE",
            "cors": {
                "allowOrigins": ["*"],
                "allowMethods": ["*"],
                "allowHeaders": ["*"],
                "allowCredentials": False,
            },
            "invokeMode": "RESPONSE_STREAM",
        },
    )
    pulumi_mocks.assert_res_counts(
        {R.ROLE: 1, R.ROLE_POLICY_ATTACHMENT: 1, R.FUNCTION: 1, R.FUNCTION_URL: 1}
    )


# =============================================================================
# Queue Customization Tests
# =============================================================================


def test_queue_customize_queue_resource(pulumi_mocks, project_cwd):
    """Test that customize parameter is applied to SQS queue resource."""
    # Arrange
    queue = Queue(
        "my-queue",
        customize={
            "queue": {
                "tags": {"Team": "backend"},
            }
        },
    )

    # Act
    @pulumi.runtime.test
    def deploy():
        return queue.resources

    deploy()

    # Assert
    pulumi_mocks.assert_res("my-queue", R.QUEUE, {"tags": {"Team": "backend"}}, partial=True)


# =============================================================================
# Topic Customization Tests
# =============================================================================


def test_topic_customize_topic_resource(pulumi_mocks, project_cwd):
    """Test that customize parameter is applied to SNS topic resource."""
    # Arrange
    topic = Topic(
        "my-topic",
        customize={
            "topic": {
                "tags": {"Project": "stelvio"},
            }
        },
    )

    # Act
    @pulumi.runtime.test
    def deploy():
        return topic.resources

    deploy()

    # Assert
    assert len(pulumi_mocks.created(R.TOPIC)) == 1
    pulumi_mocks.assert_res("my-topic", R.TOPIC, {"tags": {"Project": "stelvio"}}, partial=True)


# =============================================================================
# DynamoDB Customization Tests
# =============================================================================


def test_dynamo_table_customize_table_resource(pulumi_mocks, project_cwd):
    """Test that customize parameter is applied to DynamoDB table resource."""
    # Arrange
    table = DynamoTable(
        "my-table",
        fields={"id": "string"},
        partition_key="id",
        customize={
            "table": {
                "tags": {"Service": "orders"},
            }
        },
    )

    # Act
    @pulumi.runtime.test
    def deploy():
        return table.resources

    deploy()

    # Assert
    assert len(pulumi_mocks.created(R.DYNAMO_TABLE)) == 1
    pulumi_mocks.assert_res(
        "my-table", R.DYNAMO_TABLE, {"tags": {"Service": "orders"}}, partial=True
    )


# =============================================================================
# Cron Customization Tests
# =============================================================================


def test_cron_customize_rule_resource(pulumi_mocks, project_cwd):
    """Test that customize parameter is applied to EventBridge rule resource."""
    # Arrange
    cron = Cron(
        "my-cron",
        "rate(1 hour)",
        "functions/simple.handler",
        customize={
            "rule": {
                "tags": {"Schedule": "hourly"},
            }
        },
    )

    # Act
    @pulumi.runtime.test
    def deploy():
        return cron.resources

    deploy()

    # Assert
    assert len(pulumi_mocks.created(R.EVENT_RULE)) == 1
    pulumi_mocks.assert_res(
        "my-cron-rule", R.EVENT_RULE, {"tags": {"Schedule": "hourly"}}, partial=True
    )


def test_cron_customize_target_resource(pulumi_mocks, project_cwd):
    """Test that customize parameter is applied to EventBridge target resource."""
    # Arrange
    cron = Cron(
        "my-cron",
        "rate(1 hour)",
        "functions/simple.handler",
        customize={
            "target": {
                "retry_policy": {"maximum_event_age_in_seconds": 60},
            }
        },
    )

    # Act
    @pulumi.runtime.test
    def deploy():
        return cron.resources

    deploy()

    # Assert
    assert len(pulumi_mocks.created(R.EVENT_TARGET)) == 1
    pulumi_mocks.assert_res(
        "my-cron-target",
        R.EVENT_TARGET,
        {"retryPolicy": {"maximumEventAgeInSeconds": 60}},
        partial=True,
    )


# =============================================================================
# Test customization merging behavior
# =============================================================================


def test_customize_merges_with_defaults(pulumi_mocks, project_cwd):
    """Test that customize merges with defaults instead of replacing them."""
    # Arrange
    fn = Function(
        "my-function",
        handler="functions/simple.handler",
        memory=256,  # Default param
        customize={
            "function": {
                "timeout": 60,  # Customization
            }
        },
    )

    # Act
    @pulumi.runtime.test
    def deploy():
        return fn.resources

    deploy()

    # Assert - both default and customization are present
    pulumi_mocks.assert_res(
        "my-function", R.FUNCTION, {"memorySize": 256, "timeout": 60}, partial=True
    )


def test_customize_can_override_defaults(pulumi_mocks, project_cwd):
    """Test that customize can override default values."""
    # Arrange - Override the default memory size
    fn = Function(
        "my-function",
        handler="functions/simple.handler",
        memory=256,  # Default param
        customize={
            "function": {
                "memory_size": 512,  # Override via customize
            }
        },
    )

    # Act
    @pulumi.runtime.test
    def deploy():
        return fn.resources

    deploy()

    # Assert - customization overrides the config value
    pulumi_mocks.assert_res("my-function", R.FUNCTION, {"memorySize": 512}, partial=True)


def test_customize_empty_dict_uses_defaults(pulumi_mocks, project_cwd):
    """Test that empty customize dict still uses defaults."""
    # Arrange
    fn = Function(
        "my-function",
        handler="functions/simple.handler",
        memory=256,
        customize={},  # Empty customize
    )

    # Act
    @pulumi.runtime.test
    def deploy():
        return fn.resources

    deploy()

    # Assert - defaults are still applied
    pulumi_mocks.assert_res("my-function", R.FUNCTION, {"memorySize": 256}, partial=True)


def test_customize_none_uses_defaults(pulumi_mocks, project_cwd):
    """Test that None customize uses defaults."""
    # Arrange
    fn = Function(
        "my-function",
        handler="functions/simple.handler",
        memory=256,
        customize=None,
    )

    # Act
    @pulumi.runtime.test
    def deploy():
        return fn.resources

    deploy()

    # Assert - defaults are still applied
    pulumi_mocks.assert_res("my-function", R.FUNCTION, {"memorySize": 256}, partial=True)


# =============================================================================
# Email Customization Tests
# =============================================================================


def test_email_customize_identity_resource(pulumi_mocks, project_cwd, mock_dns):
    """Test that customize parameter is applied to SES email identity resource."""
    # Arrange
    email = Email(
        "my-email",
        "test@example.com",
        dmarc=None,
        customize={
            "identity": {
                "tags": {"Service": "notifications"},
            }
        },
    )

    # Act
    @pulumi.runtime.test
    def deploy():
        return email.resources

    deploy()

    # Assert
    assert len(pulumi_mocks.created(R.EMAIL_IDENTITY)) == 1
    pulumi_mocks.assert_res(
        "my-email-identity", R.EMAIL_IDENTITY, {"tags": {"Service": "notifications"}}, partial=True
    )


def test_email_customize_configuration_set(pulumi_mocks, project_cwd, mock_dns):
    """Test that customize parameter is applied to SES configuration set."""
    # Arrange - Domain email which creates configuration set
    email = Email(
        "my-domain-email",
        "example.com",
        dmarc=None,
        dns=mock_dns,
        customize={
            "configuration_set": {
                "tags": {"Environment": "production"},
            }
        },
    )

    # Act
    @pulumi.runtime.test
    def deploy():
        return email.resources

    deploy()

    # Assert
    assert len(pulumi_mocks.created(R.CONFIGURATION_SET)) == 1
    pulumi_mocks.assert_res(
        "my-domain-email-config-set",
        R.CONFIGURATION_SET,
        {"tags": {"Environment": "production"}},
        partial=True,
    )


# =============================================================================
# Api Gateway Customization Tests
# =============================================================================


def test_api_customize_rest_api_resource(pulumi_mocks, project_cwd):
    """Test that customize parameter is applied to API Gateway REST API resource."""
    # Arrange
    api = RestApi(
        "my-api",
        customize={
            "rest_api": {
                "description": "Custom API description",
            }
        },
    )
    api.route("GET", "/", "functions/simple.handler")

    # Act
    @pulumi.runtime.test
    def deploy():
        return api.resources

    deploy()

    # Assert
    assert len(pulumi_mocks.created(R.REST_API)) == 1
    pulumi_mocks.assert_res(
        "my-api", R.REST_API, {"description": "Custom API description"}, partial=True
    )


def test_api_customize_stage_resource(pulumi_mocks, project_cwd):
    """Test that customize parameter is applied to API Gateway stage resource."""
    # Arrange
    api = RestApi(
        "my-api",
        customize={
            "stage": {
                "description": "Custom stage description",
            }
        },
    )
    api.route("GET", "/", "functions/simple.handler")

    # Act
    @pulumi.runtime.test
    def deploy():
        return api.resources

    deploy()

    # Assert
    assert len(pulumi_mocks.created(R.API_STAGE)) == 1
    pulumi_mocks.assert_res(
        "my-api-stage-v1", R.API_STAGE, {"description": "Custom stage description"}, partial=True
    )


def _short_access_log(props: dict) -> dict:
    return {**props, "access_log_settings": {**props["access_log_settings"], "format": "$x"}}


@pytest.mark.parametrize(
    ("api_type", "route", "stage"),
    [
        pytest.param(RestApi, ("GET", "/"), "api-stage-v1", id="rest"),
        pytest.param(HttpApi, ("GET", "/"), "api-stage", id="http"),
        pytest.param(WebsocketApi, ("$connect",), "api-stage", id="websocket"),
    ],
)
def test_stage_callable_can_read_every_stage_prop(
    pulumi_mocks, project_cwd, api_type, route, stage
):
    # `url` reads the stage name from config, so the callable only runs with every prop.
    api = api_type("api", customize={"stage": _short_access_log})
    api.route(*route, "functions/simple.handler")

    @pulumi.runtime.test
    def deploy():
        _ = api.resources
        return api.url

    deploy()

    typ = R.API_STAGE if api_type is RestApi else R.HTTP_API_STAGE
    created = pulumi_mocks.assert_res(stage, typ)
    assert created.inputs["accessLogSettings"]["format"] == "$x"


# =============================================================================
# CloudFront Distribution Customization Tests
# =============================================================================


def test_cloudfront_customize_distribution_resource(pulumi_mocks, project_cwd):
    """Test that customize parameter is applied to CloudFront distribution resource."""
    # Arrange
    bucket = Bucket("my-bucket")

    cf = CloudFrontDistribution(
        "my-cf",
        bucket=bucket,
        customize={
            "distribution": {
                "comment": "Custom CloudFront comment",
            }
        },
    )

    # Act
    @pulumi.runtime.test
    def deploy():
        return [bucket.resources, cf.resources]

    deploy()

    # Assert
    assert len(pulumi_mocks.created(R.DISTRIBUTION)) == 1
    pulumi_mocks.assert_res(
        "my-cf", R.DISTRIBUTION, {"comment": "Custom CloudFront comment"}, partial=True
    )


def test_cloudfront_customize_origin_access_control(pulumi_mocks, project_cwd):
    """Test that customize parameter is applied to origin access control resource."""
    # Arrange
    bucket = Bucket("my-bucket")

    cf = CloudFrontDistribution(
        "my-cf",
        bucket=bucket,
        customize={
            "origin_access_control": {
                "description": "Custom OAC description",
            }
        },
    )

    # Act
    @pulumi.runtime.test
    def deploy():
        return [bucket.resources, cf.resources]

    deploy()

    # Assert
    assert len(pulumi_mocks.created(R.ORIGIN_ACCESS_CONTROL)) == 1
    pulumi_mocks.assert_res(
        "my-cf-oac",
        R.ORIGIN_ACCESS_CONTROL,
        {"description": "Custom OAC description"},
        partial=True,
    )


# =============================================================================
# Router Customization Tests
# =============================================================================


def test_router_customize_distribution_resource(pulumi_mocks, project_cwd):
    """Test that customize parameter is applied to Router CloudFront distribution."""
    # Arrange
    bucket = Bucket("static-bucket")

    router = Router(
        "my-router",
        customize={
            "distribution": {
                "comment": "Custom Router comment",
            }
        },
    )
    router.route("/static", bucket)

    # Act
    @pulumi.runtime.test
    def deploy():
        return [bucket.resources, router.resources]

    deploy()

    # Assert
    assert len(pulumi_mocks.created(R.DISTRIBUTION)) == 1
    pulumi_mocks.assert_res(
        "my-router", R.DISTRIBUTION, {"comment": "Custom Router comment"}, partial=True
    )


@pytest.mark.parametrize("scope", ["instance", "global"])
def test_router_customize_reaches_origin_resources(pulumi_mocks, project_cwd, scope):
    """The per-route OAC, bucket policy and CloudFront functions come from the origin
    adapters, not from Router itself; its customize keys must still reach them, app-wide
    ones too, since every value here is one Stelvio picks."""
    policy = '{"Version": "2012-10-17", "Statement": []}'
    customize = {
        "origin_access_controls": {"description": "custom oac"},
        "access_policies": {"policy": policy},
        "cloudfront_functions": {"comment": "custom function"},
    }
    if scope == "global":
        create_app_context_with_global_customize({Router: customize})

    @pulumi.runtime.test
    def deploy():
        router = Router("my-router", customize=customize if scope == "instance" else None)
        router.route("/static", Bucket("static-bucket"))
        return router.resources

    deploy()

    pulumi_mocks.assert_res(
        "my-router-static-bucket-oac",
        R.ORIGIN_ACCESS_CONTROL,
        {
            "description": "custom oac",
            "originAccessControlOriginType": "s3",
            "signingBehavior": "always",
            "signingProtocol": "sigv4",
        },
    )
    pulumi_mocks.assert_res(
        "static-bucket-bucket-policy",
        R.BUCKET_POLICY,
        {"bucket": tid(f"{TP}static-bucket"), "policy": policy},
    )
    for function in ("my-router-static-bucket-uri-rewrite", "my-router-default-404"):
        pulumi_mocks.assert_res(
            function,
            R.CLOUDFRONT_FUNCTION,
            {"runtime": "cloudfront-js-2.0", "comment": "custom function"},
            partial=True,
        )
    pulumi_mocks.assert_res_counts(
        {
            R.BUCKET: 1,
            R.BUCKET_PUBLIC_ACCESS_BLOCK: 1,
            R.BUCKET_POLICY: 1,
            R.ORIGIN_ACCESS_CONTROL: 1,
            R.CLOUDFRONT_FUNCTION: 2,
            R.DISTRIBUTION: 1,
        }
    )


# =============================================================================
# S3StaticWebsite Customization Tests
# =============================================================================


def test_s3_static_website_customize_bucket_resource(pulumi_mocks, project_cwd):
    """Test that customize parameter is applied to S3StaticWebsite bucket resource."""
    # Arrange
    with tempfile.TemporaryDirectory() as tmpdir:
        static_dir = Path(tmpdir) / "static"
        static_dir.mkdir()
        (static_dir / "index.html").write_text("<html>Hello</html>")

        website = S3StaticWebsite(
            "my-website",
            directory=str(static_dir),
            customize={
                "bucket": {  # S3StaticWebsiteCustomizationDict key
                    "bucket": {  # BucketCustomizationDict key targeting the bucket resource
                        "force_destroy": True,
                    }
                }
            },
        )

        # Act
        @pulumi.runtime.test
        def deploy():
            return website.resources

        deploy()

    # Assert
    assert len(pulumi_mocks.created(R.BUCKET)) == 1
    pulumi_mocks.assert_res("my-website-bucket", R.BUCKET, {"forceDestroy": True}, partial=True)


def test_s3_static_website_customize_cloudfront_distribution(
    pulumi_mocks, project_cwd, app_context_with_dns
):
    """Test that customize parameter is applied to S3StaticWebsite CloudFront."""
    # Arrange
    with tempfile.TemporaryDirectory() as tmpdir:
        static_dir = Path(tmpdir) / "static"
        static_dir.mkdir()
        (static_dir / "index.html").write_text("<html>Hello</html>")

        website = S3StaticWebsite(
            "my-website",
            directory=str(static_dir),
            custom_domain="example.com",
            customize={
                "cloudfront_distribution": {
                    "distribution": {
                        "comment": "Custom Website CDN",
                    }
                }
            },
        )

        # Act
        @pulumi.runtime.test
        def deploy():
            return website.resources

        deploy()

    # Assert
    assert len(pulumi_mocks.created(R.DISTRIBUTION)) == 1
    pulumi_mocks.assert_res(
        "my-website-cloudfront", R.DISTRIBUTION, {"comment": "Custom Website CDN"}, partial=True
    )


# =============================================================================
# Shallow Merge Behavior Tests
# =============================================================================


def test_customize_shallow_merge_replaces_nested_tags(pulumi_mocks, project_cwd):
    """Test that shallow merge completely replaces nested objects like tags.

    This documents the intentional shallow merge behavior: when customizing
    a nested object like tags, the ENTIRE nested object is replaced, not
    deep-merged. For example:
        defaults: {"tags": {"a": 1, "b": 2}}
        customize: {"tags": {"c": 3}}
        result: {"tags": {"c": 3}}  (NOT {"a": 1, "b": 2, "c": 3})
    """
    # Arrange - we'll customize with new tags that should replace any defaults
    bucket = Bucket(
        "shallow-merge-bucket",
        customize={
            "bucket": {
                "tags": {"NewTag": "only-this-should-exist"},
            }
        },
    )

    # Act
    @pulumi.runtime.test
    def deploy():
        return bucket.resources

    deploy()

    # Assert - shallow merge: only our custom tags are present
    pulumi_mocks.assert_res(
        "shallow-merge-bucket",
        R.BUCKET,
        {"tags": {"NewTag": "only-this-should-exist"}},
        partial=True,
    )


# =============================================================================
# Nested Component Customization Flow Tests
# =============================================================================


def test_router_customize_flows_to_nested_acm_validated_domain(
    pulumi_mocks, project_cwd, app_context_with_dns
):
    """Test that Router passes customize to nested AcmValidatedDomain.

    When customizing a router's ACM certificate, the customize dict should
    flow through:
    Router.customize["acm_validated_domain"]["certificate"] -> AcmValidatedDomain
    -> certificate customization
    """
    # Arrange
    bucket = Bucket("static-bucket")

    router = Router(
        "my-router",
        custom_domain="test.example.com",
        customize={
            "acm_validated_domain": {
                "certificate": {
                    "tags": {"ACM": "customized"},
                }
            }
        },
    )
    router.route("/static", bucket)

    # Act
    @pulumi.runtime.test
    def deploy():
        return [bucket.resources, router.resources]

    deploy()

    # Assert
    assert len(pulumi_mocks.created(R.CERTIFICATE)) == 1
    pulumi_mocks.assert_res(
        "my-router-acm-validated-domain-certificate",
        R.CERTIFICATE,
        {"tags": {"ACM": "customized"}},
        partial=True,
    )


def test_cloudfront_customize_flows_to_nested_acm_validated_domain(
    pulumi_mocks, project_cwd, app_context_with_dns
):
    """Test that CloudFrontDistribution passes customize to nested AcmValidatedDomain."""
    # Arrange
    bucket = Bucket("static-bucket")

    cf = CloudFrontDistribution(
        "my-cf",
        bucket=bucket,
        custom_domain="test.example.com",
        customize={
            "acm_validated_domain": {
                "certificate": {
                    "tags": {"CloudFront": "customized"},
                }
            }
        },
    )

    # Act
    @pulumi.runtime.test
    def deploy():
        return cf.resources

    deploy()

    # Assert
    assert len(pulumi_mocks.created(R.CERTIFICATE)) == 1
    pulumi_mocks.assert_res(
        "my-cf-acm-validated-domain-certificate",
        R.CERTIFICATE,
        {"tags": {"CloudFront": "customized"}},
        partial=True,
    )


# =============================================================================
# Unknown Customization Key Warning Tests
# =============================================================================


def test_customize_unknown_key_raises_error(pulumi_mocks, project_cwd):
    """Test that unknown customization keys raise ValueError.

    This validates early detection of typos/invalid resource keys.
    """
    # Act & Assert - creating a bucket with an unknown key should raise ValueError
    with pytest.raises(ValueError, match=r"Unknown customization key\(s\)") as exc_info:
        Bucket(
            "error-bucket",
            customize={
                "buckt": {"force_destroy": True},  # Typo: should be "bucket"
                "bucket": {"tags": {"Valid": "true"}},
            },
        )

    # Assert - error message should contain helpful information
    error_message = str(exc_info.value)
    assert "buckt" in error_message  # Unknown key is mentioned
    assert "Bucket" in error_message  # Component type is mentioned
    assert "error-bucket" in error_message  # Component name is mentioned
    assert "bucket" in error_message  # Valid keys are listed


# =============================================================================
# Layer Customization Tests
# =============================================================================


def test_layer_customize_layer_version_resource(
    pulumi_mocks, project_cwd, mock_get_or_install_dependencies_layer
):
    """Test that customize parameter is applied to Lambda layer version resource."""
    # Arrange
    layer = Layer(
        "my-layer",
        requirements=["requests"],
        customize={
            "layer_version": {
                "description": "Custom layer description",
            }
        },
    )

    # Act
    @pulumi.runtime.test
    def deploy():
        return layer.resources

    deploy()

    # Assert
    assert len(pulumi_mocks.created(R.LAYER_VERSION)) == 1
    pulumi_mocks.assert_res(
        "my-layer", R.LAYER_VERSION, {"description": "Custom layer description"}, partial=True
    )


# =============================================================================
# S3StaticWebsite Files Customization Tests
# =============================================================================


def test_s3_static_website_customize_files_resource(pulumi_mocks, project_cwd):
    """Test that customize parameter is applied to S3StaticWebsite file objects."""
    # Arrange
    with tempfile.TemporaryDirectory() as tmpdir:
        static_dir = Path(tmpdir) / "static"
        static_dir.mkdir()
        (static_dir / "index.html").write_text("<html>Hello</html>")

        website = S3StaticWebsite(
            "my-website",
            directory=str(static_dir),
            customize={
                "files": {
                    "cache_control": "max-age=31536000",  # Override default cache control
                }
            },
        )

        # Act
        @pulumi.runtime.test
        def deploy():
            return website.resources

        deploy()

    # Assert
    assert len(pulumi_mocks.created(R.BUCKET_OBJECT)) == 1
    pulumi_mocks.assert_res(
        "my-website-index-html-p",
        R.BUCKET_OBJECT,
        {"cacheControl": "max-age=31536000"},
        partial=True,
    )


# =============================================================================
# Cron Nested Function Customization Tests
# =============================================================================


def test_cron_customize_nested_function(pulumi_mocks, project_cwd):
    """Test that Cron passes customize to nested Function via function key.

    When customizing a Cron job's Lambda function, the customize dict should
    flow through: Cron.customize["function"] -> Function.customize
    """
    # Arrange
    cron = Cron(
        "my-cron",
        "rate(1 hour)",
        "functions/simple.handler",
        customize={
            "function": {
                "function": {
                    "reserved_concurrent_executions": 5,
                    "tags": {"CronNested": "customized"},
                }
            }
        },
    )

    # Act
    @pulumi.runtime.test
    def deploy():
        return cron.resources

    deploy()

    # Assert
    assert len(pulumi_mocks.created(R.FUNCTION)) == 1
    pulumi_mocks.assert_res(
        "my-cron-fn",
        R.FUNCTION,
        {"reservedConcurrentExecutions": 5, "tags": {"CronNested": "customized"}},
        partial=True,
    )


# =============================================================================
# Topic Subscription Nested Function Customization Tests
# =============================================================================


def test_topic_subscription_customize_nested_function(pulumi_mocks, project_cwd):
    """Test that TopicSubscription passes customize to nested Function via function key.

    When customizing a topic subscription's Lambda function, the customize dict should
    flow through: Topic.subscribe(..., customize={"function": {...}}) -> Function.customize
    """
    # Arrange
    topic = Topic("my-topic")

    subscription = topic.subscribe(
        "my-handler",
        "functions/simple.handler",
        customize={
            "function": {
                "function": {
                    "reserved_concurrent_executions": 3,
                    "tags": {"TopicNested": "customized"},
                }
            }
        },
    )

    # Act
    @pulumi.runtime.test
    def deploy():
        return subscription.resources

    deploy()

    # Assert
    assert len(pulumi_mocks.created(R.FUNCTION)) == 1
    pulumi_mocks.assert_res(
        "my-topic-my-handler",
        R.FUNCTION,
        {"reservedConcurrentExecutions": 3, "tags": {"TopicNested": "customized"}},
        partial=True,
    )


# =============================================================================
# Api Deployment Customization Tests
# =============================================================================


def test_api_customize_deployment_resource(pulumi_mocks, project_cwd):
    """Test that customize parameter is applied to API Gateway deployment resource."""
    # Arrange
    api = RestApi(
        "my-api",
        customize={
            "deployment": {
                "description": "Custom deployment description",
            }
        },
    )
    api.route("GET", "/", "functions/simple.handler")

    # Act
    @pulumi.runtime.test
    def deploy():
        return api.resources

    deploy()

    # Assert
    assert len(pulumi_mocks.created(R.API_DEPLOYMENT)) == 1
    pulumi_mocks.assert_res(
        "my-api-deployment",
        R.API_DEPLOYMENT,
        {"description": "Custom deployment description"},
        partial=True,
    )
