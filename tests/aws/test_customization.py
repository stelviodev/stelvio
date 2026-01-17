"""Tests for the customize parameter across all components.

These tests verify that the customize parameter is properly passed through to the
underlying Pulumi resources for each component.
"""

import shutil
from pathlib import Path

import pulumi
import pytest
from pulumi.runtime import set_mocks

from stelvio.aws.cron import Cron
from stelvio.aws.dynamo_db import DynamoTable
from stelvio.aws.function import Function
from stelvio.aws.queue import Queue
from stelvio.aws.s3 import Bucket
from stelvio.aws.topic import Topic

from .pulumi_mocks import PulumiTestMocks

# Test prefix
TP = "test-test-"


@pytest.fixture
def pulumi_mocks():
    mocks = PulumiTestMocks()
    set_mocks(mocks)
    return mocks


def delete_files(directory: Path, filename: str):
    """Helper to clean up generated files."""
    for file_path in directory.rglob(filename):
        file_path.unlink(missing_ok=True)


@pytest.fixture
def project_cwd(monkeypatch, pytestconfig, tmp_path):
    from stelvio.project import get_project_root

    get_project_root.cache_clear()
    rootpath = pytestconfig.rootpath
    source_project_dir = rootpath / "tests" / "aws" / "sample_test_project"
    temp_project_dir = tmp_path / "sample_project_copy"

    shutil.copytree(source_project_dir, temp_project_dir, dirs_exist_ok=True)
    monkeypatch.chdir(temp_project_dir)
    yield temp_project_dir
    delete_files(temp_project_dir, "stlv_resources.py")


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
