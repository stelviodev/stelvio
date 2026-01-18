"""Tests for S3 Bucket event notification functionality."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pulumi
import pytest
from pulumi.runtime import set_mocks

from stelvio.aws.dynamo_db import DynamoTable
from stelvio.aws.function import FunctionConfig
from stelvio.aws.queue import Queue
from stelvio.aws.s3 import Bucket, BucketNotifySubscription, S3BucketResources
from stelvio.aws.s3.s3 import VALID_S3_EVENTS
from stelvio.aws.topic import Topic

from ..pulumi_mocks import PulumiTestMocks

# Test prefix
TP = "test-test-"

# Test handlers - use existing files in sample_test_project
SIMPLE_HANDLER = "functions/simple.handler"
UPLOAD_HANDLER = "functions/users.handler"
DELETE_HANDLER = "functions/orders.handler"


def delete_files(directory: Path, filename: str) -> None:
    for file_path in directory.rglob(filename):
        file_path.unlink()


def wait_for_notification_resources(
    resources: S3BucketResources,
    check_callback: Callable[[Any], None],
) -> None:
    """Wait for notification resources to be created before running checks.

    This helper ensures we wait on the actual notification resources
    (permissions, queue policies, bucket notification) before checking
    created resources in mocks.
    """
    outputs_to_wait = [resources.bucket.arn]

    if resources.bucket_notification:
        outputs_to_wait.append(resources.bucket_notification.id)

    # Collect outputs from subscriptions
    for subscription in resources.subscriptions:
        sub_resources = subscription.resources
        if sub_resources.permission:
            outputs_to_wait.append(sub_resources.permission.id)
        if sub_resources.queue_policy:
            outputs_to_wait.append(sub_resources.queue_policy.id)
        if sub_resources.topic_policy:
            outputs_to_wait.append(sub_resources.topic_policy.id)

    pulumi.Output.all(*outputs_to_wait).apply(check_callback)


@pytest.fixture(autouse=True)
def project_cwd(monkeypatch, pytestconfig):
    rootpath = pytestconfig.rootpath
    test_project_dir = rootpath / "tests" / "aws" / "sample_test_project"
    monkeypatch.chdir(test_project_dir)
    yield test_project_dir
    delete_files(test_project_dir, "stlv_resources.py")


@pytest.fixture
def pulumi_mocks():
    mocks = PulumiTestMocks()
    set_mocks(mocks)
    return mocks


# =============================================================================
# Validation Tests
# =============================================================================


def test_notify_requires_events():
    """notify() must raise ValueError when events is empty."""
    bucket = Bucket("test-bucket")

    with pytest.raises(ValueError, match="events list cannot be empty"):
        bucket.notify(
            "test-notify",
            events=[],
            function=SIMPLE_HANDLER,
        )


def test_notify_validates_event_types():
    """notify() must raise ValueError for invalid event types."""
    bucket = Bucket("test-bucket")

    with pytest.raises(ValueError, match="Invalid S3 event type"):
        bucket.notify(
            "test-notify",
            events=["s3:InvalidEvent:Type"],  # type: ignore[list-item]
            function=SIMPLE_HANDLER,
        )


def test_notify_validates_mixed_valid_invalid_events():
    """notify() must raise ValueError if any event type is invalid."""
    bucket = Bucket("test-bucket")

    with pytest.raises(ValueError, match="Invalid S3 event type"):
        bucket.notify(
            "test-notify",
            events=["s3:ObjectCreated:*", "s3:Invalid:Event"],  # type: ignore[list-item]
            function=SIMPLE_HANDLER,
        )


def test_notify_accepts_all_valid_event_types():
    """notify() must accept all valid S3 event types."""
    # Should not raise for any valid event type
    for event in VALID_S3_EVENTS:
        bucket_test = Bucket(f"bucket-{event.replace(':', '-').replace('*', 'star')}")
        bucket_test.notify(
            "notify",
            events=[event],  # type: ignore[list-item]
            function=SIMPLE_HANDLER,
        )
        # If we get here without exception, the event is valid


def test_notify_requires_function_or_queue():
    """notify() must raise ValueError when neither function nor queue is specified."""
    bucket = Bucket("test-bucket")

    with pytest.raises(ValueError, match="Missing notification target"):
        bucket.notify(
            "test-notify",
            events=["s3:ObjectCreated:*"],
        )


def test_notify_rejects_both_function_and_queue():
    """notify() must raise ValueError when both function and queue are specified."""
    bucket = Bucket("test-bucket")
    queue = Queue("test-queue")

    with pytest.raises(ValueError, match="cannot specify multiple notification targets"):
        bucket.notify(
            "test-notify",
            events=["s3:ObjectCreated:*"],
            function=SIMPLE_HANDLER,
            queue=queue,
        )


def test_notify_rejects_duplicate_names():
    """notify() must raise ValueError for duplicate notification names."""
    bucket = Bucket("test-bucket")

    bucket.notify(
        "on-upload",
        events=["s3:ObjectCreated:*"],
        function=SIMPLE_HANDLER,
    )

    with pytest.raises(ValueError, match="Notification 'on-upload' already exists"):
        bucket.notify(
            "on-upload",
            events=["s3:ObjectRemoved:*"],
            function=DELETE_HANDLER,
        )


def test_notify_rejects_opts_with_queue():
    """notify() must raise ValueError when function opts are provided with queue target."""
    bucket = Bucket("test-bucket")
    queue = Queue("test-queue")

    with pytest.raises(
        ValueError, match="Cannot use function options.*with 'queue' notifications"
    ):
        bucket.notify(
            "test-notify",
            events=["s3:ObjectCreated:*"],
            queue=queue,
            memory=512,
        )


def test_notify_rejects_opts_with_topic():
    """notify() must raise ValueError when function opts are provided with topic target."""
    bucket = Bucket("test-bucket")
    topic = Topic("test-topic")

    with pytest.raises(
        ValueError, match="Cannot use function options.*with 'topic' notifications"
    ):
        bucket.notify(
            "test-notify",
            events=["s3:ObjectCreated:*"],
            topic=topic,
            timeout=30,
        )


def test_notify_returns_subscription():
    """notify() must return a BucketNotifySubscription instance."""
    bucket = Bucket("test-bucket")

    subscription = bucket.notify(
        "on-upload",
        events=["s3:ObjectCreated:*"],
        function=SIMPLE_HANDLER,
    )

    assert isinstance(subscription, BucketNotifySubscription)
    assert subscription.name == "test-bucket-on-upload-subscription"
    assert subscription.function_name == "test-bucket-on-upload"
    assert subscription.events == ["s3:ObjectCreated:*"]


def test_notify_function_with_empty_links():
    """notify() with empty links list should work normally."""
    bucket = Bucket("test-bucket")

    subscription = bucket.notify(
        "on-upload",
        events=["s3:ObjectCreated:*"],
        function=SIMPLE_HANDLER,
        links=[],
    )

    assert isinstance(subscription, BucketNotifySubscription)
    assert subscription.links == []


def test_notify_rejects_after_resources_created(pulumi_mocks):
    """notify() must raise RuntimeError after bucket resources are created."""
    bucket = Bucket("test-bucket")

    # Trigger resource creation
    _ = bucket.resources

    with pytest.raises(RuntimeError, match="Cannot add notifications after Bucket resources"):
        bucket.notify(
            "test-notify",
            events=["s3:ObjectCreated:*"],
            function=SIMPLE_HANDLER,
        )


# =============================================================================
# Handler Configuration Tests
# =============================================================================


@pytest.mark.parametrize(
    ("handler", "opts", "expected_error"),
    [
        pytest.param(
            {"handler": SIMPLE_HANDLER},
            {"memory": 512},
            "cannot combine complete handler configuration with additional options",
            id="dict_with_opts",
        ),
        pytest.param(
            FunctionConfig(handler=SIMPLE_HANDLER),
            {"memory": 512},
            "cannot combine complete handler configuration with additional options",
            id="config_with_opts",
        ),
        pytest.param(
            SIMPLE_HANDLER,
            {"handler": "other/handler.fn"},
            "Ambiguous handler configuration",
            id="handler_in_both_places",
        ),
    ],
)
def test_notify_handler_validation(handler, opts, expected_error):
    """notify() must validate handler configuration properly."""
    bucket = Bucket("test-bucket")

    with pytest.raises(ValueError, match=expected_error):
        bucket.notify(
            "test-notify",
            events=["s3:ObjectCreated:*"],
            function=handler,
            **opts,
        )


# =============================================================================
# Function Notification Resource Tests
# =============================================================================


@pulumi.runtime.test
def test_notify_function_creates_resources(pulumi_mocks):
    """notify() with function creates Lambda function, permission, and notification."""
    bucket = Bucket("test-bucket")

    bucket.notify(
        "on-upload",
        events=["s3:ObjectCreated:*"],
        function=UPLOAD_HANDLER,
    )

    # Trigger resource creation
    resources = bucket.resources

    def check_resources(_):
        # Check Lambda function was created
        functions = pulumi_mocks.created_functions()
        function_names = [f.name for f in functions]
        assert TP + "test-bucket-on-upload" in function_names

        # Check Lambda permission was created for S3
        permissions = pulumi_mocks.created_permissions()
        assert len(permissions) >= 1

        # Find the S3 permission
        s3_permissions = [
            p for p in permissions if p.inputs.get("principal") == "s3.amazonaws.com"
        ]
        assert len(s3_permissions) == 1
        s3_perm = s3_permissions[0]
        assert s3_perm.inputs["action"] == "lambda:InvokeFunction"

        # Check BucketNotification was created
        notifications = pulumi_mocks.created_bucket_notifications()
        assert len(notifications) == 1
        notification = notifications[0]

        # Verify lambda functions config
        lambda_functions = notification.inputs.get("lambdaFunctions")
        assert lambda_functions is not None
        assert len(lambda_functions) == 1
        assert lambda_functions[0]["events"] == ["s3:ObjectCreated:*"]

    wait_for_notification_resources(resources, check_resources)


@pulumi.runtime.test
def test_notify_function_with_config(pulumi_mocks):
    """notify() with FunctionConfig creates function with correct configuration."""
    bucket = Bucket("test-bucket")

    config = FunctionConfig(handler=UPLOAD_HANDLER, memory=512, timeout=30)
    bucket.notify(
        "on-upload",
        events=["s3:ObjectCreated:*"],
        function=config,
    )

    resources = bucket.resources

    def check_resources(_):
        functions = pulumi_mocks.created_functions()
        upload_fn = next((f for f in functions if "on-upload" in f.name), None)
        assert upload_fn is not None
        assert upload_fn.inputs.get("memorySize") == 512
        assert upload_fn.inputs.get("timeout") == 30

    wait_for_notification_resources(resources, check_resources)


@pulumi.runtime.test
def test_notify_function_with_opts(pulumi_mocks):
    """notify() with string handler and opts creates function with correct configuration."""
    bucket = Bucket("test-bucket")

    bucket.notify(
        "on-upload",
        events=["s3:ObjectCreated:*"],
        function=UPLOAD_HANDLER,
        memory=256,
        timeout=15,
    )

    resources = bucket.resources

    def check_resources(_):
        functions = pulumi_mocks.created_functions()
        upload_fn = next((f for f in functions if "on-upload" in f.name), None)
        assert upload_fn is not None
        assert upload_fn.inputs.get("memorySize") == 256
        assert upload_fn.inputs.get("timeout") == 15

    wait_for_notification_resources(resources, check_resources)


@pulumi.runtime.test
def test_notify_with_filters(pulumi_mocks):
    """notify() with filter_prefix and filter_suffix creates proper filter rules."""
    bucket = Bucket("test-bucket")

    bucket.notify(
        "on-upload",
        events=["s3:ObjectCreated:*"],
        filter_prefix="uploads/",
        filter_suffix=".jpg",
        function=UPLOAD_HANDLER,
    )

    resources = bucket.resources

    def check_resources(_):
        notifications = pulumi_mocks.created_bucket_notifications()
        assert len(notifications) == 1
        notification = notifications[0]

        lambda_functions = notification.inputs.get("lambdaFunctions")
        assert len(lambda_functions) == 1
        lambda_config = lambda_functions[0]
        assert lambda_config.get("filterPrefix") == "uploads/"
        assert lambda_config.get("filterSuffix") == ".jpg"

    wait_for_notification_resources(resources, check_resources)


# =============================================================================
# Queue Notification Resource Tests
# =============================================================================


@pulumi.runtime.test
def test_notify_queue_creates_resources(pulumi_mocks):
    """notify() with queue creates queue policy and notification."""
    queue = Queue("test-queue")
    bucket = Bucket("test-bucket")

    bucket.notify(
        "on-upload",
        events=["s3:ObjectCreated:*"],
        queue=queue,
    )

    # Trigger resource creation
    _ = queue.resources
    resources = bucket.resources

    def check_resources(_):
        # Check SQS queue policy was created
        queue_policies = pulumi_mocks.created_queue_policies()
        assert len(queue_policies) == 1

        # Check BucketNotification was created
        notifications = pulumi_mocks.created_bucket_notifications()
        assert len(notifications) == 1
        notification = notifications[0]

        # Verify queues config (not lambdaFunctions)
        queues = notification.inputs.get("queues")
        assert queues is not None
        assert len(queues) == 1
        assert queues[0]["events"] == ["s3:ObjectCreated:*"]

        # Lambda functions should be empty/None
        lambda_functions = notification.inputs.get("lambdaFunctions")
        assert lambda_functions is None

    # Wait for the notification to be created before checking resources
    # The bucket_notification is only created when there are notifications
    wait_for_notification_resources(resources, check_resources)


@pulumi.runtime.test
def test_notify_queue_with_filters(pulumi_mocks):
    """notify() with queue and filters creates proper filter rules."""
    queue = Queue("test-queue")
    bucket = Bucket("test-bucket")

    bucket.notify(
        "on-upload",
        events=["s3:ObjectCreated:Put"],
        filter_prefix="data/",
        filter_suffix=".json",
        queue=queue,
    )

    _ = queue.resources
    resources = bucket.resources

    def check_resources(_):
        notifications = pulumi_mocks.created_bucket_notifications()
        assert len(notifications) == 1
        notification = notifications[0]

        queues = notification.inputs.get("queues")
        assert len(queues) == 1
        queue_config = queues[0]
        assert queue_config.get("filterPrefix") == "data/"
        assert queue_config.get("filterSuffix") == ".json"
        assert queue_config["events"] == ["s3:ObjectCreated:Put"]

    wait_for_notification_resources(resources, check_resources)


@pulumi.runtime.test
def test_notify_queue_arn_string(pulumi_mocks):
    """notify() with queue ARN string creates proper notification but expects manual policy."""
    bucket = Bucket("test-bucket")

    # Use a queue ARN string instead of Queue component
    queue_arn = "arn:aws:sqs:us-east-1:123456789012:my-external-queue"

    bucket.notify(
        "on-upload",
        events=["s3:ObjectCreated:*"],
        queue=queue_arn,
    )

    resources = bucket.resources

    def check_resources(_):
        # SQS queue policy should NOT be created for external queues (prevents overwrite)
        queue_policies = pulumi_mocks.created_queue_policies()
        assert len(queue_policies) == 0

        # Check BucketNotification was created
        notifications = pulumi_mocks.created_bucket_notifications()
        assert len(notifications) == 1
        notification = notifications[0]

        # Verify queues config has the ARN
        queues = notification.inputs.get("queues")
        assert queues is not None
        assert len(queues) == 1
        assert queues[0]["queueArn"] == queue_arn

    wait_for_notification_resources(resources, check_resources)


# =============================================================================
# Multiple Notifications Tests
# =============================================================================


@pulumi.runtime.test
def test_multiple_function_notifications(pulumi_mocks):
    """Multiple notify() calls aggregate into single BucketNotification."""
    bucket = Bucket("test-bucket")

    bucket.notify(
        "on-create",
        events=["s3:ObjectCreated:*"],
        function=UPLOAD_HANDLER,
    )

    bucket.notify(
        "on-delete",
        events=["s3:ObjectRemoved:*"],
        function=DELETE_HANDLER,
    )

    resources = bucket.resources

    def check_resources(_):
        # Should have 2 Lambda functions
        functions = pulumi_mocks.created_functions()
        function_names = [f.name for f in functions]
        assert TP + "test-bucket-on-create" in function_names
        assert TP + "test-bucket-on-delete" in function_names

        # Should have 2 Lambda permissions
        permissions = pulumi_mocks.created_permissions()
        s3_permissions = [
            p for p in permissions if p.inputs.get("principal") == "s3.amazonaws.com"
        ]
        assert len(s3_permissions) == 2

        # Should have only 1 BucketNotification with 2 lambda configs
        notifications = pulumi_mocks.created_bucket_notifications()
        assert len(notifications) == 1
        notification = notifications[0]

        lambda_functions = notification.inputs.get("lambdaFunctions")
        assert len(lambda_functions) == 2

    wait_for_notification_resources(resources, check_resources)


@pulumi.runtime.test
def test_mixed_function_and_queue_notifications(pulumi_mocks):
    """notify() with both function and queue targets creates proper aggregated notification."""
    queue = Queue("test-queue")
    bucket = Bucket("test-bucket")

    bucket.notify(
        "on-create",
        events=["s3:ObjectCreated:*"],
        function=UPLOAD_HANDLER,
    )

    bucket.notify(
        "on-delete",
        events=["s3:ObjectRemoved:*"],
        queue=queue,
    )

    _ = queue.resources
    resources = bucket.resources

    def check_resources(_):
        # Should have 1 Lambda function
        functions = pulumi_mocks.created_functions()
        function_names = [f.name for f in functions]
        assert TP + "test-bucket-on-create" in function_names

        # Should have 1 Lambda permission
        s3_permissions = [
            p
            for p in pulumi_mocks.created_permissions()
            if p.inputs.get("principal") == "s3.amazonaws.com"
        ]
        assert len(s3_permissions) == 1

        # Should have 1 queue policy
        queue_policies = pulumi_mocks.created_queue_policies()
        assert len(queue_policies) == 1

        # Should have only 1 BucketNotification with both configs
        notifications = pulumi_mocks.created_bucket_notifications()
        assert len(notifications) == 1
        notification = notifications[0]

        lambda_functions = notification.inputs.get("lambdaFunctions")
        assert len(lambda_functions) == 1

        queues = notification.inputs.get("queues")
        assert len(queues) == 1

    wait_for_notification_resources(resources, check_resources)


# =============================================================================
# No Notification Tests
# =============================================================================


@pulumi.runtime.test
def test_bucket_without_notifications(pulumi_mocks):
    """Bucket without notify() calls should not create notification resources."""
    bucket = Bucket("test-bucket")

    resources = bucket.resources

    def check_resources(_):
        # Should have no BucketNotification
        notifications = pulumi_mocks.created_bucket_notifications()
        assert len(notifications) == 0

        # Resources should have None for notification
        assert resources.bucket_notification is None
        assert resources.subscriptions == []

    # No notifications, so we can just wait on the bucket itself
    resources.bucket.arn.apply(check_resources)


# =============================================================================
# S3BucketResources Tests
# =============================================================================


@pulumi.runtime.test
def test_s3_bucket_resources_with_notifications(pulumi_mocks):
    """S3BucketResources includes notification-related resources."""
    bucket = Bucket("test-bucket")

    bucket.notify(
        "on-upload",
        events=["s3:ObjectCreated:*"],
        function=UPLOAD_HANDLER,
    )

    resources = bucket.resources

    def check_resources(_):
        assert resources.bucket_notification is not None
        assert len(resources.subscriptions) == 1

        sub = resources.subscriptions[0]
        assert sub.name == "test-bucket-on-upload-subscription"
        assert sub.function_name == "test-bucket-on-upload"
        assert sub.events == ["s3:ObjectCreated:*"]

        # Verify function resources
        assert sub.resources.function is not None
        assert sub.resources.function.name == "test-bucket-on-upload"
        assert sub.resources.permission is not None
        assert sub.resources.queue_policy is None
        assert sub.resources.topic_policy is None

        # Verify actual Pulumi resources
        notifications = pulumi_mocks.created_bucket_notifications()
        assert len(notifications) == 1

    wait_for_notification_resources(resources, check_resources)


@pulumi.runtime.test
def test_s3_bucket_resources_with_queue_notification(pulumi_mocks):
    """S3BucketResources includes queue policy when using queue notification."""
    queue = Queue("test-queue")
    bucket = Bucket("test-bucket")

    bucket.notify(
        "on-upload",
        events=["s3:ObjectCreated:*"],
        queue=queue,
    )

    _ = queue.resources
    resources = bucket.resources

    def check_resources(_):
        assert resources.bucket_notification is not None
        assert len(resources.subscriptions) == 1

        sub = resources.subscriptions[0]
        assert sub.name == "test-bucket-on-upload-subscription"
        assert sub.queue_ref is queue

        # Verify queue subscription resources
        assert sub.resources.function is None
        assert sub.resources.permission is None
        assert sub.resources.queue_policy is not None
        assert sub.resources.topic_policy is None

        # Verify actual Pulumi resources
        queue_policies = pulumi_mocks.created_queue_policies()
        assert len(queue_policies) == 1

    wait_for_notification_resources(resources, check_resources)


@pulumi.runtime.test
def test_s3_bucket_resources_with_topic_notification(pulumi_mocks):
    """S3BucketResources includes topic policy when using topic notification."""
    topic = Topic("test-topic")
    bucket = Bucket("test-bucket")

    bucket.notify(
        "on-upload",
        events=["s3:ObjectCreated:*"],
        topic=topic,
    )

    _ = topic.resources
    resources = bucket.resources

    def check_resources(_):
        assert resources.bucket_notification is not None
        assert len(resources.subscriptions) == 1

        sub = resources.subscriptions[0]
        assert sub.name == "test-bucket-on-upload-subscription"
        assert sub.topic_ref is topic

        # Verify topic subscription resources
        assert sub.resources.function is None
        assert sub.resources.permission is None
        assert sub.resources.queue_policy is None
        assert sub.resources.topic_policy is not None

        # Verify actual Pulumi resources
        topic_policies = pulumi_mocks.created_topic_policies()
        assert len(topic_policies) == 1

    wait_for_notification_resources(resources, check_resources)


# =============================================================================
# Links Tests
# =============================================================================


@pulumi.runtime.test
def test_notify_function_with_links(pulumi_mocks):
    """notify() with links passes links to the created function."""
    table = DynamoTable("test-table", fields={"pk": "string"}, partition_key="pk")
    bucket = Bucket("test-bucket")

    bucket.notify(
        "on-upload",
        events=["s3:ObjectCreated:*"],
        function=UPLOAD_HANDLER,
        links=[table],
    )

    # Trigger resource creation
    _ = table.resources
    resources = bucket.resources

    def check_resources(_):
        # Check Lambda function was created
        functions = pulumi_mocks.created_functions()
        function_names = [f.name for f in functions]
        assert TP + "test-bucket-on-upload" in function_names

        # Verify DynamoDB permissions are included in the function's IAM policy
        policies = pulumi_mocks.created_policies()
        fn_policy = next((p for p in policies if "test-bucket-on-upload" in p.name), None)
        assert fn_policy is not None, "IAM policy not found for function"

        policy_doc = json.loads(fn_policy.inputs["policy"])
        dynamo_statements = [
            s for s in policy_doc if any("dynamodb:" in a for a in s.get("actions", []))
        ]
        assert len(dynamo_statements) > 0, "DynamoDB permissions not found in function policy"

    wait_for_notification_resources(resources, check_resources)


@pulumi.runtime.test
def test_notify_function_merges_links_with_config_links(pulumi_mocks):
    """notify() merges links parameter with links from FunctionConfig."""
    table1 = DynamoTable("table1", fields={"pk": "string"}, partition_key="pk")
    table2 = DynamoTable("table2", fields={"pk": "string"}, partition_key="pk")
    bucket = Bucket("test-bucket")

    # Config has links to table1, notify() adds links to table2
    config = FunctionConfig(handler=UPLOAD_HANDLER, links=[table1])
    bucket.notify(
        "on-upload",
        events=["s3:ObjectCreated:*"],
        function=config,
        links=[table2],
    )

    # Trigger resource creation
    _ = table1.resources
    _ = table2.resources
    resources = bucket.resources

    def check_resources(_):
        # Check Lambda function was created
        functions = pulumi_mocks.created_functions()
        function_names = [f.name for f in functions]
        assert TP + "test-bucket-on-upload" in function_names

        # Verify both tables' permissions are included in the function's IAM policy
        policies = pulumi_mocks.created_policies()
        fn_policy = next((p for p in policies if "test-bucket-on-upload" in p.name), None)
        assert fn_policy is not None, "IAM policy not found for function"

        policy_doc = json.loads(fn_policy.inputs["policy"])
        dynamo_statements = [
            s for s in policy_doc if any("dynamodb:" in a for a in s.get("actions", []))
        ]
        # Should have permissions for both tables
        assert len(dynamo_statements) >= 2, (
            f"Expected permissions for both tables, found {len(dynamo_statements)} statements"
        )

    wait_for_notification_resources(resources, check_resources)


@pulumi.runtime.test
def test_multiple_notifications_same_queue_creates_single_policy(pulumi_mocks):
    """Multiple notifications to the same queue should create only one QueuePolicy."""
    queue = Queue("test-queue")
    bucket = Bucket("test-bucket")

    bucket.notify(
        "notify-1",
        events=["s3:ObjectCreated:Put"],
        queue=queue,
    )
    bucket.notify(
        "notify-2",
        events=["s3:ObjectRemoved:*"],
        queue=queue,
    )

    _ = queue.resources
    resources = bucket.resources

    def check_resources(_):
        # Should have 2 queue configs in BucketNotification
        notifications = pulumi_mocks.created_bucket_notifications()
        assert len(notifications) == 1
        queues = notifications[0].inputs.get("queues")
        assert len(queues) == 2

        # BUT should have only 1 QueuePolicy
        queue_policies = pulumi_mocks.created_queue_policies()
        assert len(queue_policies) == 1

    wait_for_notification_resources(resources, check_resources)


# =============================================================================
# Topic Notification Tests
# =============================================================================


def test_notify_rejects_function_and_topic():
    """notify() must raise ValueError when both function and topic are specified."""
    bucket = Bucket("test-bucket")
    topic = Topic("test-topic")

    with pytest.raises(ValueError, match="cannot specify multiple notification targets"):
        bucket.notify(
            "test-notify",
            events=["s3:ObjectCreated:*"],
            function=SIMPLE_HANDLER,
            topic=topic,
        )


def test_notify_rejects_queue_and_topic():
    """notify() must raise ValueError when both queue and topic are specified."""
    bucket = Bucket("test-bucket")
    queue = Queue("test-queue")
    topic = Topic("test-topic")

    with pytest.raises(ValueError, match="cannot specify multiple notification targets"):
        bucket.notify(
            "test-notify",
            events=["s3:ObjectCreated:*"],
            queue=queue,
            topic=topic,
        )


def test_notify_rejects_all_three_targets():
    """notify() must raise ValueError when function, queue, and topic are all specified."""
    bucket = Bucket("test-bucket")
    queue = Queue("test-queue")
    topic = Topic("test-topic")

    with pytest.raises(ValueError, match="cannot specify multiple notification targets"):
        bucket.notify(
            "test-notify",
            events=["s3:ObjectCreated:*"],
            function=SIMPLE_HANDLER,
            queue=queue,
            topic=topic,
        )


def test_notify_rejects_links_with_queue():
    """notify() must raise ValueError when links is specified with queue."""
    bucket = Bucket("test-bucket")
    queue = Queue("test-queue")
    table = DynamoTable("test-table", fields={"pk": "string"}, partition_key="pk")

    with pytest.raises(ValueError, match="'links' parameter cannot be used with 'queue'"):
        bucket.notify(
            "test-notify",
            events=["s3:ObjectCreated:*"],
            queue=queue,
            links=[table],
        )


def test_notify_rejects_links_with_topic():
    """notify() must raise ValueError when links is specified with topic."""
    bucket = Bucket("test-bucket")
    topic = Topic("test-topic")
    table = DynamoTable("test-table", fields={"pk": "string"}, partition_key="pk")

    with pytest.raises(ValueError, match="'links' parameter cannot be used with 'topic'"):
        bucket.notify(
            "test-notify",
            events=["s3:ObjectCreated:*"],
            topic=topic,
            links=[table],
        )


@pulumi.runtime.test
def test_notify_topic_creates_resources(pulumi_mocks):
    """notify() with topic creates topic policy and notification."""
    topic = Topic("test-topic")
    bucket = Bucket("test-bucket")

    bucket.notify(
        "on-upload",
        events=["s3:ObjectCreated:*"],
        topic=topic,
    )

    # Trigger resource creation
    _ = topic.resources
    resources = bucket.resources

    def check_resources(_):
        # Check SNS topic policy was created
        topic_policies = pulumi_mocks.created_topic_policies()
        assert len(topic_policies) == 1

        # Check BucketNotification was created
        notifications = pulumi_mocks.created_bucket_notifications()
        assert len(notifications) == 1
        notification = notifications[0]

        # Verify topics config (not lambdaFunctions or queues)
        topics = notification.inputs.get("topics")
        assert topics is not None
        assert len(topics) == 1
        assert topics[0]["events"] == ["s3:ObjectCreated:*"]

        # Lambda functions and queues should be empty/None
        lambda_functions = notification.inputs.get("lambdaFunctions")
        assert lambda_functions is None
        queues = notification.inputs.get("queues")
        assert queues is None

    wait_for_notification_resources(resources, check_resources)


@pulumi.runtime.test
def test_notify_topic_with_filters(pulumi_mocks):
    """notify() with topic and filters creates proper filter rules."""
    topic = Topic("test-topic")
    bucket = Bucket("test-bucket")

    bucket.notify(
        "on-upload",
        events=["s3:ObjectCreated:Put"],
        filter_prefix="logs/",
        filter_suffix=".log",
        topic=topic,
    )

    _ = topic.resources
    resources = bucket.resources

    def check_resources(_):
        notifications = pulumi_mocks.created_bucket_notifications()
        assert len(notifications) == 1
        notification = notifications[0]

        topics = notification.inputs.get("topics")
        assert len(topics) == 1
        topic_config = topics[0]
        assert topic_config.get("filterPrefix") == "logs/"
        assert topic_config.get("filterSuffix") == ".log"
        assert topic_config["events"] == ["s3:ObjectCreated:Put"]

    wait_for_notification_resources(resources, check_resources)


@pulumi.runtime.test
def test_notify_topic_arn_string(pulumi_mocks):
    """notify() with topic ARN string creates proper notification but no policy."""
    bucket = Bucket("test-bucket")

    # Use a topic ARN string instead of Topic component
    topic_arn = "arn:aws:sns:us-east-1:123456789012:my-external-topic"

    bucket.notify(
        "on-upload",
        events=["s3:ObjectCreated:*"],
        topic=topic_arn,
    )

    resources = bucket.resources

    def check_resources(_):
        # SNS topic policy should NOT be created for external topics (prevents overwrite)
        topic_policies = pulumi_mocks.created_topic_policies()
        assert len(topic_policies) == 0

        # Check BucketNotification was created
        notifications = pulumi_mocks.created_bucket_notifications()
        assert len(notifications) == 1
        notification = notifications[0]

        # Verify topics config has the ARN
        topics = notification.inputs.get("topics")
        assert topics is not None
        assert len(topics) == 1
        assert topics[0]["topicArn"] == topic_arn

    wait_for_notification_resources(resources, check_resources)


@pulumi.runtime.test
def test_multiple_notifications_same_topic_creates_single_policy(pulumi_mocks):
    """Multiple notifications to the same topic should create only one TopicPolicy."""
    topic = Topic("test-topic")
    bucket = Bucket("test-bucket")

    bucket.notify(
        "notify-1",
        events=["s3:ObjectCreated:Put"],
        topic=topic,
    )
    bucket.notify(
        "notify-2",
        events=["s3:ObjectRemoved:*"],
        topic=topic,
    )

    _ = topic.resources
    resources = bucket.resources

    def check_resources(_):
        # Should have 2 topic configs in BucketNotification
        notifications = pulumi_mocks.created_bucket_notifications()
        assert len(notifications) == 1
        topics = notifications[0].inputs.get("topics")
        assert len(topics) == 2

        # BUT should have only 1 TopicPolicy
        topic_policies = pulumi_mocks.created_topic_policies()
        assert len(topic_policies) == 1

    wait_for_notification_resources(resources, check_resources)


@pulumi.runtime.test
def test_mixed_function_queue_and_topic_notifications(pulumi_mocks):
    """notify() with function, queue, and topic targets creates proper aggregated notification."""
    queue = Queue("test-queue")
    topic = Topic("test-topic")
    bucket = Bucket("test-bucket")

    bucket.notify(
        "on-create",
        events=["s3:ObjectCreated:*"],
        function=UPLOAD_HANDLER,
    )

    bucket.notify(
        "on-delete",
        events=["s3:ObjectRemoved:*"],
        queue=queue,
    )

    bucket.notify(
        "on-restore",
        events=["s3:ObjectRestore:*"],
        topic=topic,
    )

    _ = queue.resources
    _ = topic.resources
    resources = bucket.resources

    def check_resources(_):
        # Should have 1 Lambda function
        functions = pulumi_mocks.created_functions()
        function_names = [f.name for f in functions]
        assert TP + "test-bucket-on-create" in function_names

        # Should have 1 Lambda permission
        s3_permissions = [
            p
            for p in pulumi_mocks.created_permissions()
            if p.inputs.get("principal") == "s3.amazonaws.com"
        ]
        assert len(s3_permissions) == 1

        # Should have 1 queue policy
        queue_policies = pulumi_mocks.created_queue_policies()
        assert len(queue_policies) == 1

        # Should have 1 topic policy
        topic_policies = pulumi_mocks.created_topic_policies()
        assert len(topic_policies) == 1

        # Should have only 1 BucketNotification with all configs
        notifications = pulumi_mocks.created_bucket_notifications()
        assert len(notifications) == 1
        notification = notifications[0]

        lambda_functions = notification.inputs.get("lambdaFunctions")
        assert len(lambda_functions) == 1

        queues = notification.inputs.get("queues")
        assert len(queues) == 1

        topics = notification.inputs.get("topics")
        assert len(topics) == 1

    wait_for_notification_resources(resources, check_resources)
