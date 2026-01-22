"""Tests for the customize parameter across all components.

These tests verify that the customize parameter is properly passed through to the
underlying Pulumi resources for each component.
"""

import tempfile
from pathlib import Path
from unittest.mock import Mock

import pulumi
import pytest
from pulumi.runtime import set_mocks

from stelvio.aws.api_gateway import Api
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
from .pulumi_mocks import MockDns, PulumiTestMocks


class EmailTestMocks(PulumiTestMocks):
    """Extended mocks for Email tests that add DKIM tokens."""

    def new_resource(self, args):
        id_, props = super().new_resource(args)
        if args.typ == "aws:sesv2/emailIdentity:EmailIdentity":
            props["dkim_signing_attributes"] = {"tokens": ["token1", "token2", "token3"]}
            props["arn"] = (
                f"arn:aws:ses:us-east-1:123456789012:identity/{args.inputs['emailIdentity']}"
            )
        if args.typ == "aws:sesv2/configurationSet:ConfigurationSet":
            props["arn"] = (
                f"arn:aws:ses:us-east-1:123456789012:configuration-set/"
                f"{args.inputs['configurationSetName']}"
            )
        return id_, props


@pytest.fixture
def email_mocks():
    mocks = EmailTestMocks()
    set_mocks(mocks)
    return mocks


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


@pulumi.runtime.test
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
    _ = bucket.resources

    # Assert
    def check_resources(_):
        buckets = pulumi_mocks.created_s3_buckets(TP + "my-bucket")
        assert len(buckets) == 1
        created_bucket = buckets[0]

        # Check customization was applied
        assert created_bucket.inputs.get("forceDestroy") is True
        assert created_bucket.inputs.get("tags") == {"Environment": "test"}

    bucket.resources.bucket.id.apply(check_resources)


@pulumi.runtime.test
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
    _ = bucket.resources

    # Assert
    def check_resources(_):
        pabs = pulumi_mocks.created_s3_public_access_blocks(TP + "my-bucket-pab")
        assert len(pabs) == 1
        pab = pabs[0]

        # Customization should override the default
        assert pab.inputs.get("blockPublicAcls") is False
        # Other defaults should remain
        assert pab.inputs.get("blockPublicPolicy") is True

    bucket.resources.public_access_block.id.apply(check_resources)


# =============================================================================
# Function Customization Tests
# =============================================================================


@pulumi.runtime.test
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
    _ = fn.resources

    # Assert
    def check_resources(_):
        functions = pulumi_mocks.created_functions(TP + "my-function")
        assert len(functions) == 1
        created_fn = functions[0]

        # Check customization was applied
        assert created_fn.inputs.get("reservedConcurrentExecutions") == 10

    fn.resources.function.id.apply(check_resources)


# =============================================================================
# Queue Customization Tests
# =============================================================================


@pulumi.runtime.test
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
    _ = queue.resources

    # Assert
    def check_resources(_):
        queues = pulumi_mocks.created_sqs_queues(TP + "my-queue")
        assert len(queues) == 1
        created_queue = queues[0]

        # Check customization was applied
        assert created_queue.inputs.get("tags") == {"Team": "backend"}

    queue.resources.queue.id.apply(check_resources)


# =============================================================================
# Topic Customization Tests
# =============================================================================


@pulumi.runtime.test
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
    _ = topic.resources

    # Assert
    def check_resources(_):
        topics = pulumi_mocks.created_sns_topics()
        assert len(topics) >= 1

        # Find our topic
        matching_topics = [t for t in topics if "my-topic" in t.name]
        assert len(matching_topics) == 1
        created_topic = matching_topics[0]

        # Check customization was applied
        assert created_topic.inputs.get("tags") == {"Project": "stelvio"}

    topic.resources.topic.id.apply(check_resources)


# =============================================================================
# DynamoDB Customization Tests
# =============================================================================


@pulumi.runtime.test
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
    _ = table.resources

    # Assert
    def check_resources(_):
        tables = pulumi_mocks.created_dynamodb_tables()
        assert len(tables) >= 1

        # Find our table
        matching_tables = [t for t in tables if "my-table" in t.name]
        assert len(matching_tables) == 1
        created_table = matching_tables[0]

        # Check customization was applied
        assert created_table.inputs.get("tags") == {"Service": "orders"}

    table.resources.table.id.apply(check_resources)


# =============================================================================
# Cron Customization Tests
# =============================================================================


@pulumi.runtime.test
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
    _ = cron.resources

    # Assert
    def check_resources(_):
        rules = pulumi_mocks.created_event_rules()
        assert len(rules) >= 1

        # Find our rule
        matching_rules = [r for r in rules if "my-cron" in r.name]
        assert len(matching_rules) == 1
        created_rule = matching_rules[0]

        # Check customization was applied
        assert created_rule.inputs.get("tags") == {"Schedule": "hourly"}

    cron.resources.rule.id.apply(check_resources)


@pulumi.runtime.test
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
    _ = cron.resources

    # Assert
    def check_resources(_):
        targets = pulumi_mocks.created_event_targets()
        assert len(targets) >= 1

        # Find our target
        matching_targets = [t for t in targets if "my-cron" in t.name]
        assert len(matching_targets) == 1
        created_target = matching_targets[0]

        # Check customization was applied
        retry_policy = created_target.inputs.get("retryPolicy")
        assert retry_policy is not None
        assert retry_policy.get("maximumEventAgeInSeconds") == 60

    cron.resources.target.id.apply(check_resources)


# =============================================================================
# Test customization merging behavior
# =============================================================================


@pulumi.runtime.test
def test_customize_merges_with_defaults(pulumi_mocks, project_cwd):
    """Test that customize merges with defaults instead of replacing them."""
    # Arrange
    bucket = Bucket(
        "my-bucket",
        versioning=True,  # Default param
        customize={
            "bucket": {
                "force_destroy": True,  # Customization
            }
        },
    )

    # Act
    _ = bucket.resources

    # Assert
    def check_resources(_):
        buckets = pulumi_mocks.created_s3_buckets(TP + "my-bucket")
        assert len(buckets) == 1
        created_bucket = buckets[0]

        # Both default and customization should be present
        assert created_bucket.inputs.get("versioning", {}).get("enabled") is True
        assert created_bucket.inputs.get("forceDestroy") is True

    bucket.resources.bucket.id.apply(check_resources)


@pulumi.runtime.test
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
    _ = fn.resources

    # Assert
    def check_resources(_):
        functions = pulumi_mocks.created_functions(TP + "my-function")
        assert len(functions) == 1
        created_fn = functions[0]

        # Customization should override the config value
        assert created_fn.inputs.get("memorySize") == 512

    fn.resources.function.id.apply(check_resources)


@pulumi.runtime.test
def test_customize_empty_dict_uses_defaults(pulumi_mocks, project_cwd):
    """Test that empty customize dict still uses defaults."""
    # Arrange
    bucket = Bucket(
        "my-bucket",
        versioning=True,
        customize={},  # Empty customize
    )

    # Act
    _ = bucket.resources

    # Assert
    def check_resources(_):
        buckets = pulumi_mocks.created_s3_buckets(TP + "my-bucket")
        assert len(buckets) == 1
        created_bucket = buckets[0]

        # Defaults should still be applied
        assert created_bucket.inputs.get("versioning", {}).get("enabled") is True

    bucket.resources.bucket.id.apply(check_resources)


@pulumi.runtime.test
def test_customize_none_uses_defaults(pulumi_mocks, project_cwd):
    """Test that None customize uses defaults."""
    # Arrange
    bucket = Bucket(
        "my-bucket",
        versioning=True,
        customize=None,
    )

    # Act
    _ = bucket.resources

    # Assert
    def check_resources(_):
        buckets = pulumi_mocks.created_s3_buckets(TP + "my-bucket")
        assert len(buckets) == 1
        created_bucket = buckets[0]

        # Defaults should still be applied
        assert created_bucket.inputs.get("versioning", {}).get("enabled") is True

    bucket.resources.bucket.id.apply(check_resources)


# =============================================================================
# Email Customization Tests
# =============================================================================


@pulumi.runtime.test
def test_email_customize_identity_resource(email_mocks, project_cwd, mock_dns):
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
    _ = email.resources

    # Assert
    def check_resources(_):
        identities = email_mocks.created_email_identities()
        assert len(identities) >= 1

        # Find our identity
        matching_identities = [i for i in identities if "my-email" in i.name]
        assert len(matching_identities) == 1
        created_identity = matching_identities[0]

        # Check customization was applied
        assert created_identity.inputs.get("tags") == {"Service": "notifications"}

    email.resources.identity.id.apply(check_resources)


@pulumi.runtime.test
def test_email_customize_configuration_set(email_mocks, project_cwd, mock_dns):
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
    _ = email.resources

    # Assert
    def check_resources(_):
        config_sets = email_mocks.created_configuration_sets()
        assert len(config_sets) >= 1

        # Find our configuration set
        matching_sets = [cs for cs in config_sets if "my-domain-email" in cs.name]
        assert len(matching_sets) == 1
        created_config_set = matching_sets[0]

        # Check customization was applied
        assert created_config_set.inputs.get("tags") == {"Environment": "production"}

    email.resources.configuration_set.id.apply(check_resources)


# =============================================================================
# Api Gateway Customization Tests
# =============================================================================


@pulumi.runtime.test
def test_api_customize_rest_api_resource(pulumi_mocks, project_cwd):
    """Test that customize parameter is applied to API Gateway REST API resource."""
    # Arrange
    api = Api(
        "my-api",
        customize={
            "rest_api": {
                "description": "Custom API description",
            }
        },
    )
    api.route("GET", "/", "functions/simple.handler")

    # Act
    _ = api.resources

    # Assert
    def check_resources(_):
        rest_apis = pulumi_mocks.created_rest_apis()
        assert len(rest_apis) >= 1

        # Find our REST API
        matching_apis = [a for a in rest_apis if "my-api" in a.name]
        assert len(matching_apis) == 1
        created_api = matching_apis[0]

        # Check customization was applied
        assert created_api.inputs.get("description") == "Custom API description"

    api.resources.rest_api.id.apply(check_resources)


@pulumi.runtime.test
def test_api_customize_stage_resource(pulumi_mocks, project_cwd):
    """Test that customize parameter is applied to API Gateway stage resource."""
    # Arrange
    api = Api(
        "my-api",
        customize={
            "stage": {
                "description": "Custom stage description",
            }
        },
    )
    api.route("GET", "/", "functions/simple.handler")

    # Act
    _ = api.resources

    # Assert
    def check_resources(_):
        stages = pulumi_mocks.created_stages()
        assert len(stages) >= 1

        # Find our stage
        matching_stages = [s for s in stages if "my-api" in s.name]
        assert len(matching_stages) == 1
        created_stage = matching_stages[0]

        # Check customization was applied
        assert created_stage.inputs.get("description") == "Custom stage description"

    api.resources.stage.id.apply(check_resources)


# =============================================================================
# CloudFront Distribution Customization Tests
# =============================================================================


@pulumi.runtime.test
def test_cloudfront_customize_distribution_resource(pulumi_mocks, project_cwd):
    """Test that customize parameter is applied to CloudFront distribution resource."""
    # Arrange
    bucket = Bucket("my-bucket")
    _ = bucket.resources

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
    _ = cf.resources

    # Assert
    def check_resources(_):
        distributions = pulumi_mocks.created_cloudfront_distributions()
        assert len(distributions) >= 1

        # Find our distribution
        matching_dists = [d for d in distributions if "my-cf" in d.name]
        assert len(matching_dists) == 1
        created_dist = matching_dists[0]

        # Check customization was applied
        assert created_dist.inputs.get("comment") == "Custom CloudFront comment"

    cf.resources.distribution.id.apply(check_resources)


@pulumi.runtime.test
def test_cloudfront_customize_origin_access_control(pulumi_mocks, project_cwd):
    """Test that customize parameter is applied to origin access control resource."""
    # Arrange
    bucket = Bucket("my-bucket")
    _ = bucket.resources

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
    _ = cf.resources

    # Assert
    def check_resources(_):
        oacs = pulumi_mocks.created_origin_access_controls()
        assert len(oacs) >= 1

        # Find our OAC
        matching_oacs = [o for o in oacs if "my-cf" in o.name]
        assert len(matching_oacs) == 1
        created_oac = matching_oacs[0]

        # Check customization was applied
        assert created_oac.inputs.get("description") == "Custom OAC description"

    cf.resources.origin_access_control.id.apply(check_resources)


# =============================================================================
# Router Customization Tests
# =============================================================================


@pulumi.runtime.test
def test_router_customize_distribution_resource(pulumi_mocks, project_cwd):
    """Test that customize parameter is applied to Router CloudFront distribution."""
    # Arrange
    bucket = Bucket("static-bucket")
    _ = bucket.resources

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
    _ = router.resources

    # Assert
    def check_resources(_):
        distributions = pulumi_mocks.created_cloudfront_distributions()
        assert len(distributions) >= 1

        # Find our distribution
        matching_dists = [d for d in distributions if "my-router" in d.name]
        assert len(matching_dists) == 1
        created_dist = matching_dists[0]

        # Check customization was applied
        assert created_dist.inputs.get("comment") == "Custom Router comment"

    router.resources.distribution.id.apply(check_resources)


# =============================================================================
# S3StaticWebsite Customization Tests
# =============================================================================


@pulumi.runtime.test
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
        _ = website.resources

        # Assert
        def check_resources(_):
            buckets = pulumi_mocks.created_s3_buckets()
            assert len(buckets) >= 1

            # Find our bucket
            matching_buckets = [b for b in buckets if "my-website" in b.name]
            assert len(matching_buckets) == 1
            created_bucket = matching_buckets[0]

            # Check customization was applied
            assert created_bucket.inputs.get("forceDestroy") is True

        website.resources.bucket.resources.bucket.id.apply(check_resources)


@pulumi.runtime.test
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
        _ = website.resources

        # Assert
        def check_resources(_):
            distributions = pulumi_mocks.created_cloudfront_distributions()
            assert len(distributions) >= 1

            # Find our distribution
            matching_dists = [d for d in distributions if "my-website" in d.name]
            assert len(matching_dists) == 1
            created_dist = matching_dists[0]

            # Check customization was applied
            assert created_dist.inputs.get("comment") == "Custom Website CDN"

        website.resources.cloudfront_distribution.resources.distribution.id.apply(check_resources)


# =============================================================================
# Shallow Merge Behavior Tests
# =============================================================================


@pulumi.runtime.test
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
    _ = bucket.resources

    # Assert
    def check_resources(_):
        buckets = pulumi_mocks.created_s3_buckets(TP + "shallow-merge-bucket")
        assert len(buckets) == 1
        created_bucket = buckets[0]

        # Shallow merge: only our custom tags are present
        tags = created_bucket.inputs.get("tags", {})
        assert tags == {"NewTag": "only-this-should-exist"}

    bucket.resources.bucket.id.apply(check_resources)


# =============================================================================
# Nested Component Customization Flow Tests
# =============================================================================


@pulumi.runtime.test
def test_bucket_notify_customize_flows_to_nested_function(pulumi_mocks, project_cwd):
    """Test that BucketNotifySubscription passes customize to nested Function.

    When customizing a bucket's subscription function, the customize dict should
    flow through:
    Bucket.customize["subscriptions"]["function"] -> BucketNotifySubscription
    -> Function.customize
    """
    from tests.aws.s3.test_bucket_notify import wait_for_notification_resources

    # Arrange
    bucket = Bucket(
        "notify-bucket",
        customize={
            "subscriptions": {
                "function": {
                    "function": {
                        "reserved_concurrent_executions": 5,
                        "tags": {"Nested": "customization"},
                    }
                }
            }
        },
    )

    bucket.notify_function(
        "on-upload",
        events=["s3:ObjectCreated:*"],
        function="functions/simple.handler",
    )

    # Act
    resources = bucket.resources

    # Assert
    def check_resources(_):
        # Find the function created by the subscription by name substring
        functions = pulumi_mocks.created_functions()
        created_fn = next((f for f in functions if "on-upload" in f.name), None)
        assert created_fn is not None, (
            f"No function with 'on-upload' found. Created: {[f.name for f in functions]}"
        )

        # Check that customization was applied to the nested function
        assert created_fn.inputs.get("reservedConcurrentExecutions") == 5
        assert created_fn.inputs.get("tags") == {"Nested": "customization"}

    wait_for_notification_resources(resources, check_resources)


@pulumi.runtime.test
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
    _ = bucket.resources

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
    _ = router.resources

    # Assert
    def check_resources(_):
        # Find the ACM certificate created by the router
        certs = pulumi_mocks.created_certificates()
        assert len(certs) >= 1

        # Find our certificate
        matching_certs = [c for c in certs if "my-router" in c.name]
        assert len(matching_certs) == 1
        created_cert = matching_certs[0]

        # Check that customization was applied to the nested ACM certificate
        assert created_cert.inputs.get("tags") == {"ACM": "customized"}

    router.resources.distribution.id.apply(check_resources)


@pulumi.runtime.test
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
    _ = cf.resources

    # Assert
    def check_resources(_):
        # Find the ACM certificate created by CloudFront
        certs = pulumi_mocks.created_certificates()
        assert len(certs) >= 1

        # Find our certificate
        matching_certs = [c for c in certs if "my-cf" in c.name]
        assert len(matching_certs) == 1
        created_cert = matching_certs[0]

        # Check that customization was applied
        assert created_cert.inputs.get("tags") == {"CloudFront": "customized"}

    cf.resources.distribution.id.apply(check_resources)


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


@pulumi.runtime.test
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
    _ = layer.resources

    # Assert
    def check_resources(_):
        layer_versions = pulumi_mocks.created_layer_versions()
        assert len(layer_versions) >= 1

        # Find our layer version
        matching_layers = [lv for lv in layer_versions if "my-layer" in lv.name]
        assert len(matching_layers) == 1
        created_layer = matching_layers[0]

        # Check customization was applied
        assert created_layer.inputs.get("description") == "Custom layer description"

    layer.resources.layer_version.id.apply(check_resources)


# =============================================================================
# S3StaticWebsite Files Customization Tests
# =============================================================================


@pulumi.runtime.test
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
        _ = website.resources

        # Assert
        def check_resources(_):
            bucket_objects = pulumi_mocks.created_s3_bucket_objects()
            assert len(bucket_objects) >= 1

            # Find our file object
            matching_objects = [obj for obj in bucket_objects if "my-website" in obj.name]
            assert len(matching_objects) >= 1
            created_object = matching_objects[0]

            # Check customization was applied
            assert created_object.inputs.get("cacheControl") == "max-age=31536000"

        # Wait for files to be created before checking
        website.resources.files[0].id.apply(check_resources)


# =============================================================================
# Cron Nested Function Customization Tests
# =============================================================================


@pulumi.runtime.test
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
    _ = cron.resources

    # Assert
    def check_resources(_):
        # Find the function created by Cron
        functions = pulumi_mocks.created_functions()
        matching_functions = [f for f in functions if "my-cron" in f.name]
        assert len(matching_functions) == 1
        created_fn = matching_functions[0]

        # Check that customization was applied to the nested function
        assert created_fn.inputs.get("reservedConcurrentExecutions") == 5
        assert created_fn.inputs.get("tags") == {"CronNested": "customized"}

    cron.resources.function.resources.function.id.apply(check_resources)


# =============================================================================
# Topic Subscription Nested Function Customization Tests
# =============================================================================


@pulumi.runtime.test
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

    # Act - trigger resource creation for both topic and subscription
    _ = topic.resources
    _ = subscription.resources

    # Assert
    def check_resources(_):
        # Find the function created by the subscription
        functions = pulumi_mocks.created_functions()
        matching_functions = [f for f in functions if "my-handler" in f.name]
        assert len(matching_functions) == 1
        created_fn = matching_functions[0]

        # Check that customization was applied to the nested function
        assert created_fn.inputs.get("reservedConcurrentExecutions") == 3
        assert created_fn.inputs.get("tags") == {"TopicNested": "customized"}

    subscription.resources.function.resources.function.id.apply(check_resources)


# =============================================================================
# Api Deployment Customization Tests
# =============================================================================


@pulumi.runtime.test
def test_api_customize_deployment_resource(pulumi_mocks, project_cwd):
    """Test that customize parameter is applied to API Gateway deployment resource."""
    # Arrange
    api = Api(
        "my-api",
        customize={
            "deployment": {
                "description": "Custom deployment description",
            }
        },
    )
    api.route("GET", "/", "functions/simple.handler")

    # Act
    _ = api.resources

    # Assert
    def check_resources(_):
        deployments = pulumi_mocks.created_deployments()
        assert len(deployments) >= 1

        # Find our deployment
        matching_deployments = [d for d in deployments if "my-api" in d.name]
        assert len(matching_deployments) == 1
        created_deployment = matching_deployments[0]

        # Check customization was applied
        assert created_deployment.inputs.get("description") == "Custom deployment description"

    api.resources.deployment.id.apply(check_resources)
