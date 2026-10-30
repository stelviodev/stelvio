"""Tests for S3 Bucket event notification functionality."""

import json
import re

import pulumi
from pytest import mark, param, raises

from stelvio.aws.dynamo_db import DynamoTable
from stelvio.aws.function import Function, FunctionConfig
from stelvio.aws.queue import Queue
from stelvio.aws.s3 import Bucket, BucketNotifySubscription
from stelvio.aws.s3.s3 import VALID_S3_EVENTS
from stelvio.aws.topic import Topic
from stelvio.link import Link

from ...conftest import TP
from ..pulumi_mocks import ACCOUNT_ID, DEFAULT_REGION, R, tid, tn

# Test handlers - use existing files in sample_test_project
SIMPLE_HANDLER = "functions/simple.handler"
UPLOAD_HANDLER = "functions/users.handler"
DELETE_HANDLER = "functions/orders.handler"
pytestmark = mark.usefixtures("project_cwd")


BUCKET_ID = tid(TP + "test-bucket")
BUCKET_ARN = f"arn:aws:s3:::{tn(TP + 'test-bucket')}"
QUEUE_ARN = f"arn:aws:sqs:{DEFAULT_REGION}:{ACCOUNT_ID}:{tn(TP + 'test-queue')}"
TOPIC_ARN = f"arn:aws:sns:{DEFAULT_REGION}:{ACCOUNT_ID}:{tn(TP + 'test-topic')}"


def lambda_arn(name: str) -> str:
    return f"arn:aws:lambda:{DEFAULT_REGION}:{ACCOUNT_ID}:function:{tn(TP + name)}"


def dynamo_link_statements(table: str) -> list[dict]:
    table_arn = f"arn:aws:dynamodb:{DEFAULT_REGION}:{ACCOUNT_ID}:table/{tn(TP + table)}"
    return [
        {
            "actions": [
                "dynamodb:Scan",
                "dynamodb:Query",
                "dynamodb:GetItem",
                "dynamodb:PutItem",
                "dynamodb:UpdateItem",
                "dynamodb:DeleteItem",
            ],
            "resources": [table_arn],
        },
        {"actions": ["dynamodb:Query", "dynamodb:Scan"], "resources": [f"{table_arn}/index/*"]},
    ]


# =============================================================================
# Validation Tests
# =============================================================================


def test_notify_function_requires_events():
    """notify_function() must raise ValueError when events is empty."""
    bucket = Bucket("test-bucket")

    with raises(ValueError, match="events list cannot be empty"):
        bucket.notify_function(
            "test-notify",
            events=[],
            function=SIMPLE_HANDLER,
        )


def test_notify_function_validates_event_types():
    """notify_function() must raise ValueError for invalid event types."""
    bucket = Bucket("test-bucket")

    with raises(ValueError, match="Invalid S3 event type"):
        bucket.notify_function(
            "test-notify",
            events=["s3:InvalidEvent:Type"],
            function=SIMPLE_HANDLER,
        )


def test_notify_function_validates_mixed_valid_invalid_events():
    """notify_function() must raise ValueError if any event type is invalid."""
    bucket = Bucket("test-bucket")

    with raises(ValueError, match="Invalid S3 event type"):
        bucket.notify_function(
            "test-notify",
            events=["s3:ObjectCreated:*", "s3:Invalid:Event"],
            function=SIMPLE_HANDLER,
        )


def test_notify_function_accepts_all_valid_event_types():
    """notify_function() must accept all valid S3 event types."""
    # Should not raise for any valid event type
    for event in VALID_S3_EVENTS:
        bucket_test = Bucket(f"bucket-{event.replace(':', '-').replace('*', 'star')}")
        bucket_test.notify_function(
            "notify",
            events=[event],
            function=SIMPLE_HANDLER,
        )
        # If we get here without exception, the event is valid


def test_notify_function_rejects_duplicate_names():
    """notify_function() must raise ValueError for duplicate notification names."""
    bucket = Bucket("test-bucket")

    bucket.notify_function(
        "on-upload",
        events=["s3:ObjectCreated:*"],
        function=SIMPLE_HANDLER,
    )

    with raises(ValueError, match="Notification 'on-upload' already exists"):
        bucket.notify_function(
            "on-upload",
            events=["s3:ObjectRemoved:*"],
            function=DELETE_HANDLER,
        )


def test_notify_function_returns_subscription():
    """notify_function() must return a BucketNotifySubscription instance."""
    bucket = Bucket("test-bucket")

    subscription = bucket.notify_function(
        "on-upload",
        events=["s3:ObjectCreated:*"],
        function=SIMPLE_HANDLER,
    )

    assert isinstance(subscription, BucketNotifySubscription)
    assert subscription.name == "test-bucket-on-upload-subscription"
    assert subscription._function_name == "test-bucket-on-upload"
    assert subscription._events == ["s3:ObjectCreated:*"]


def test_notify_function_rejects_after_resources_created(pulumi_mocks):
    """notify_function() must raise RuntimeError after bucket resources are created."""
    bucket = Bucket("test-bucket")

    # Trigger resource creation
    _ = bucket.resources

    with raises(RuntimeError, match="Cannot modify Bucket 'test-bucket' after resources"):
        bucket.notify_function(
            "test-notify",
            events=["s3:ObjectCreated:*"],
            function=SIMPLE_HANDLER,
        )


def test_notify_function_can_link_its_own_bucket(pulumi_mocks):
    # The link reads the bucket resource alone, so building the Lambda doesn't re-enter
    # the bucket's own creation (that re-entry built the subscription's Function twice).
    bucket = Bucket("uploads")
    bucket.notify_function(
        "on-upload", events=["s3:ObjectCreated:*"], function=SIMPLE_HANDLER, links=[bucket]
    )

    @pulumi.runtime.test
    def deploy():
        return bucket.resources

    deploy()

    bucket_name = tn(TP + "uploads")
    pulumi_mocks.assert_res("uploads", R.BUCKET)
    pulumi_mocks.assert_res(
        "uploads-on-upload",
        R.FUNCTION,
        {
            "environment": {
                "variables": {
                    "STLV_UPLOADS_BUCKET_ARN": f"arn:aws:s3:::{bucket_name}",
                    "STLV_UPLOADS_BUCKET_NAME": bucket_name,
                }
            }
        },
        partial=True,
    )
    pulumi_mocks.assert_res_counts(
        {
            R.BUCKET: 1,
            R.BUCKET_PUBLIC_ACCESS_BLOCK: 1,
            R.BUCKET_NOTIFICATION: 1,
            R.FUNCTION: 1,
            R.LAMBDA_PERMISSION: 1,
            R.ROLE: 1,
            R.POLICY: 1,
            R.ROLE_POLICY_ATTACHMENT: 2,
        }
    )


@mark.parametrize(
    ("read_bucket", "counts"),
    [
        (
            lambda bucket: bucket.arn,
            {
                R.BUCKET: 1,
                R.BUCKET_PUBLIC_ACCESS_BLOCK: 1,
                R.BUCKET_NOTIFICATION: 1,
                R.FUNCTION: 1,
                R.LAMBDA_PERMISSION: 1,
                R.ROLE: 1,
                R.ROLE_POLICY_ATTACHMENT: 1,
            },
        ),
        (
            lambda bucket: Function("reader", handler=SIMPLE_HANDLER, links=[bucket]).resources,
            {
                R.BUCKET: 1,
                R.BUCKET_PUBLIC_ACCESS_BLOCK: 1,
                R.BUCKET_NOTIFICATION: 1,
                R.FUNCTION: 2,
                R.LAMBDA_PERMISSION: 1,
                R.ROLE: 2,
                R.POLICY: 1,
                R.ROLE_POLICY_ATTACHMENT: 3,
            },
        ),
    ],
    ids=["arn", "linked-function"],
)
def test_notify_function_after_bucket_arn_or_link(pulumi_mocks, read_bucket, counts):
    bucket = Bucket("uploads")
    read_bucket(bucket)
    bucket.notify_function("on-upload", events=["s3:ObjectCreated:*"], function=SIMPLE_HANDLER)

    @pulumi.runtime.test
    def deploy():
        return bucket.resources

    deploy()

    pulumi_mocks.assert_res("uploads", R.BUCKET)
    pulumi_mocks.assert_res("uploads-notifications", R.BUCKET_NOTIFICATION)
    pulumi_mocks.assert_res("uploads-on-upload", R.FUNCTION)
    pulumi_mocks.assert_res_counts(counts)


def test_duplicate_notification_name_across_types():
    """Using the same notification name across different notify methods must raise ValueError."""
    bucket = Bucket("test-bucket")
    queue = Queue("test-queue")

    # First add a function notification
    bucket.notify_function(
        "on-upload",
        events=["s3:ObjectCreated:*"],
        function=SIMPLE_HANDLER,
    )

    # Then try to add a queue notification with the same name
    with raises(ValueError, match="Notification 'on-upload' already exists"):
        bucket.notify_queue(
            "on-upload",
            events=["s3:ObjectRemoved:*"],
            queue=queue,
        )


# =============================================================================
# Handler Configuration Tests
# =============================================================================


@mark.parametrize(
    ("handler", "opts", "expected_error"),
    [
        param(
            {"handler": SIMPLE_HANDLER},
            {"memory": 512},
            "cannot combine complete handler configuration with additional options",
            id="dict_with_opts",
        ),
        param(
            FunctionConfig(handler=SIMPLE_HANDLER),
            {"memory": 512},
            "cannot combine complete handler configuration with additional options",
            id="config_with_opts",
        ),
        param(
            {"handler": SIMPLE_HANDLER},
            {"links": [Link("extra", None, None)]},
            "cannot combine complete handler configuration with additional options",
            id="dict_with_links",
        ),
        param(
            FunctionConfig(handler=SIMPLE_HANDLER),
            {"links": [Link("extra", None, None)]},
            "cannot combine complete handler configuration with additional options",
            id="config_with_links",
        ),
        param(
            SIMPLE_HANDLER,
            {"handler": "other/handler.fn"},
            "Ambiguous handler configuration",
            id="handler_in_both_places",
        ),
    ],
)
def test_notify_function_handler_validation(handler, opts, expected_error):
    """notify_function() must validate handler configuration properly."""
    bucket = Bucket("test-bucket")

    with raises(ValueError, match=expected_error):
        bucket.notify_function(
            "test-notify",
            events=["s3:ObjectCreated:*"],
            function=handler,
            **opts,
        )


@mark.parametrize("key", ["subscriptions", "function", "queue", "topic"])
def test_bucket_customize_rejects_removed_notification_keys(key):
    with raises(ValueError, match=rf"Unknown customization key\(s\) \['{key}'\]"):
        Bucket("uploads", customize={key: {}})


@mark.parametrize("key", ["queue_policy", "topic_policy"])
def test_notify_function_customize_rejects_policy_keys(key):
    with raises(ValueError, match=rf"Unknown customization key\(s\) \['{key}'\]"):
        Bucket("uploads").notify_function(
            "on-upload",
            events=["s3:ObjectCreated:*"],
            function=SIMPLE_HANDLER,
            customize={key: {}},
        )


# =============================================================================
# Function Notification Resource Tests
# =============================================================================


def test_notify_function_creates_resources(pulumi_mocks):
    """notify_function() creates Lambda function, permission, and notification."""
    bucket = Bucket("test-bucket")

    bucket.notify_function(
        "on-upload",
        events=["s3:ObjectCreated:*"],
        function=UPLOAD_HANDLER,
    )

    @pulumi.runtime.test
    def deploy():
        return bucket.resources

    deploy()

    pulumi_mocks.assert_res("test-bucket-on-upload", R.FUNCTION)
    pulumi_mocks.assert_res(
        "test-bucket-on-upload-subscription-perm",
        R.LAMBDA_PERMISSION,
        {
            "action": "lambda:InvokeFunction",
            "function": tn(TP + "test-bucket-on-upload"),
            "principal": "s3.amazonaws.com",
            # Ensure permission is scoped to this bucket.
            "sourceArn": BUCKET_ARN,
        },
    )
    [notification] = pulumi_mocks.created(R.BUCKET_NOTIFICATION)
    assert notification.inputs == {
        "bucket": BUCKET_ID,
        "lambdaFunctions": [
            {
                "lambdaFunctionArn": lambda_arn("test-bucket-on-upload"),
                "events": ["s3:ObjectCreated:*"],
            }
        ],
    }
    pulumi_mocks.assert_res_counts(
        {
            R.BUCKET: 1,
            R.BUCKET_PUBLIC_ACCESS_BLOCK: 1,
            R.BUCKET_NOTIFICATION: 1,
            R.FUNCTION: 1,
            R.LAMBDA_PERMISSION: 1,
            R.ROLE: 1,
            R.ROLE_POLICY_ATTACHMENT: 1,
        }
    )


def test_notify_function_with_config(pulumi_mocks):
    """notify_function() with FunctionConfig creates function with correct configuration."""
    bucket = Bucket("test-bucket")

    config = FunctionConfig(handler=UPLOAD_HANDLER, memory=512, timeout=30)
    bucket.notify_function(
        "on-upload",
        events=["s3:ObjectCreated:*"],
        function=config,
    )

    @pulumi.runtime.test
    def deploy():
        return bucket.resources

    deploy()

    pulumi_mocks.assert_res(
        "test-bucket-on-upload", R.FUNCTION, {"memorySize": 512, "timeout": 30}, partial=True
    )


def test_notify_function_customize_reaches_only_its_own_lambda_and_permission(pulumi_mocks):
    bucket = Bucket("uploads")
    bucket.notify_function(
        "on-upload",
        events=["s3:ObjectCreated:*"],
        function=SIMPLE_HANDLER,
        customize={
            "function": {
                "function": {
                    "reserved_concurrent_executions": 5,
                    "tags": {"Nested": "customization"},
                }
            },
            "permission": {"statement_id": "s3-upload"},
        },
    )
    bucket.notify_function("on-delete", events=["s3:ObjectRemoved:*"], function=SIMPLE_HANDLER)

    @pulumi.runtime.test
    def deploy():
        return bucket.resources

    deploy()

    pulumi_mocks.assert_res(
        "uploads-on-upload",
        R.FUNCTION,
        {"reservedConcurrentExecutions": 5, "tags": {"Nested": "customization"}},
        partial=True,
    )
    pulumi_mocks.assert_res(
        "uploads-on-upload-subscription-perm",
        R.LAMBDA_PERMISSION,
        {"statementId": "s3-upload"},
        partial=True,
    )
    pulumi_mocks.assert_res(
        "uploads-on-delete", R.FUNCTION, {"reservedConcurrentExecutions": None}, partial=True
    )
    pulumi_mocks.assert_res(
        "uploads-on-delete-subscription-perm",
        R.LAMBDA_PERMISSION,
        {"statementId": None},
        partial=True,
    )
    pulumi_mocks.assert_res_counts(
        {
            R.BUCKET: 1,
            R.BUCKET_PUBLIC_ACCESS_BLOCK: 1,
            R.BUCKET_NOTIFICATION: 1,
            R.FUNCTION: 2,
            R.LAMBDA_PERMISSION: 2,
            R.ROLE: 2,
            R.ROLE_POLICY_ATTACHMENT: 2,
        }
    )


def test_notify_function_with_opts(pulumi_mocks):
    """notify_function() with string handler and opts creates function correctly."""
    bucket = Bucket("test-bucket")

    bucket.notify_function(
        "on-upload",
        events=["s3:ObjectCreated:*"],
        function=UPLOAD_HANDLER,
        memory=256,
        timeout=15,
    )

    @pulumi.runtime.test
    def deploy():
        return bucket.resources

    deploy()

    pulumi_mocks.assert_res(
        "test-bucket-on-upload", R.FUNCTION, {"memorySize": 256, "timeout": 15}, partial=True
    )


def test_notify_function_with_filters(pulumi_mocks):
    """notify_function() with filter_prefix and filter_suffix creates proper filter rules."""
    bucket = Bucket("test-bucket")

    bucket.notify_function(
        "on-upload",
        events=["s3:ObjectCreated:*"],
        filter_prefix="uploads/",
        filter_suffix=".jpg",
        function=UPLOAD_HANDLER,
    )

    @pulumi.runtime.test
    def deploy():
        return bucket.resources

    deploy()

    [notification] = pulumi_mocks.created(R.BUCKET_NOTIFICATION)
    assert notification.inputs == {
        "bucket": BUCKET_ID,
        "lambdaFunctions": [
            {
                "lambdaFunctionArn": lambda_arn("test-bucket-on-upload"),
                "events": ["s3:ObjectCreated:*"],
                "filterPrefix": "uploads/",
                "filterSuffix": ".jpg",
            }
        ],
    }


def test_notify_with_multiple_events(pulumi_mocks):
    """notify_function() with multiple events creates notification with all events."""
    bucket = Bucket("test-bucket")

    bucket.notify_function(
        "on-changes",
        events=["s3:ObjectCreated:*", "s3:ObjectRemoved:*"],
        function=UPLOAD_HANDLER,
    )

    @pulumi.runtime.test
    def deploy():
        return bucket.resources

    deploy()

    pulumi_mocks.assert_res("test-bucket-on-changes", R.FUNCTION)
    [notification] = pulumi_mocks.created(R.BUCKET_NOTIFICATION)
    assert notification.inputs == {
        "bucket": BUCKET_ID,
        "lambdaFunctions": [
            {
                "lambdaFunctionArn": lambda_arn("test-bucket-on-changes"),
                "events": ["s3:ObjectCreated:*", "s3:ObjectRemoved:*"],
            }
        ],
    }


def test_notify_function_with_empty_filter_strings(pulumi_mocks):
    """notify_function() normalizes empty filter strings to None."""
    bucket = Bucket("test-bucket")

    bucket.notify_function(
        "on-upload",
        events=["s3:ObjectCreated:*"],
        filter_prefix="",
        filter_suffix="",
        function=UPLOAD_HANDLER,
    )

    @pulumi.runtime.test
    def deploy():
        return bucket.resources

    deploy()

    [notification] = pulumi_mocks.created(R.BUCKET_NOTIFICATION)
    assert notification.inputs == {
        "bucket": BUCKET_ID,
        "lambdaFunctions": [
            {
                "lambdaFunctionArn": lambda_arn("test-bucket-on-upload"),
                "events": ["s3:ObjectCreated:*"],
            }
        ],
    }


# =============================================================================
# Queue Validation Tests
# =============================================================================


def test_notify_queue_requires_events():
    """notify_queue() must raise ValueError when events is empty."""
    bucket = Bucket("test-bucket")
    queue = Queue("test-queue")

    with raises(ValueError, match="events list cannot be empty"):
        bucket.notify_queue(
            "test-notify",
            events=[],
            queue=queue,
        )


def test_notify_queue_validates_event_types():
    """notify_queue() must raise ValueError for invalid event types."""
    bucket = Bucket("test-bucket")
    queue = Queue("test-queue")

    with raises(ValueError, match="Invalid S3 event type"):
        bucket.notify_queue(
            "test-notify",
            events=["s3:InvalidEvent:Type"],
            queue=queue,
        )


def test_notify_queue_rejects_duplicate_names():
    """notify_queue() must raise ValueError for duplicate notification names."""
    bucket = Bucket("test-bucket")
    queue = Queue("test-queue")

    bucket.notify_queue(
        "on-upload",
        events=["s3:ObjectCreated:*"],
        queue=queue,
    )

    with raises(ValueError, match="Notification 'on-upload' already exists"):
        bucket.notify_queue(
            "on-upload",
            events=["s3:ObjectRemoved:*"],
            queue=queue,
        )


# =============================================================================
# Queue Notification Resource Tests
# =============================================================================


def test_notify_queue_with_multiple_events(pulumi_mocks):
    """notify_queue() with multiple events creates notification with all events."""
    queue = Queue("test-queue")
    bucket = Bucket("test-bucket")

    bucket.notify_queue(
        "on-changes",
        events=["s3:ObjectCreated:*", "s3:ObjectRemoved:*"],
        queue=queue,
    )

    @pulumi.runtime.test
    def deploy():
        return [queue.resources, bucket.resources]

    deploy()

    [notification] = pulumi_mocks.created(R.BUCKET_NOTIFICATION)
    assert notification.inputs == {
        "bucket": BUCKET_ID,
        "queues": [
            {"queueArn": QUEUE_ARN, "events": ["s3:ObjectCreated:*", "s3:ObjectRemoved:*"]}
        ],
    }


def test_notify_queue_creates_resources(pulumi_mocks):
    """notify_queue() creates queue policy and notification."""
    queue = Queue("test-queue")
    bucket = Bucket("test-bucket")

    bucket.notify_queue(
        "on-upload",
        events=["s3:ObjectCreated:*"],
        queue=queue,
    )

    @pulumi.runtime.test
    def deploy():
        return [queue.resources, bucket.resources]

    deploy()

    assert len(pulumi_mocks.created(R.QUEUE_POLICY)) == 1
    [notification] = pulumi_mocks.created(R.BUCKET_NOTIFICATION)
    assert notification.inputs == {
        "bucket": BUCKET_ID,
        "queues": [{"queueArn": QUEUE_ARN, "events": ["s3:ObjectCreated:*"]}],
    }


def test_notify_queue_policy_lets_this_apps_buckets_and_topics_send(pulumi_mocks):
    queue = Queue("jobs")
    bucket = Bucket("uploads")
    bucket.notify_queue("on-upload", events=["s3:ObjectCreated:*"], queue=queue)

    @pulumi.runtime.test
    def deploy():
        return bucket.resources

    deploy()

    queue_name = tn(TP + "jobs")
    policy = pulumi_mocks.assert_res(
        "uploads-on-upload-subscription-qp",
        R.QUEUE_POLICY,
        {"queueUrl": f"https://sqs.{DEFAULT_REGION}.amazonaws.com/{ACCOUNT_ID}/{queue_name}"},
        partial=True,
    )
    assert json.loads(policy.inputs["policy"]) == {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": ["s3.amazonaws.com", "sns.amazonaws.com"]},
                "Action": "sqs:SendMessage",
                "Resource": f"arn:aws:sqs:{DEFAULT_REGION}:{ACCOUNT_ID}:{queue_name}",
                "Condition": {
                    "StringEquals": {"aws:SourceAccount": ACCOUNT_ID},
                    "StringLike": {
                        "aws:SourceArn": [
                            f"arn:aws:s3:::{TP}*",
                            f"arn:aws:sns:{DEFAULT_REGION}:{ACCOUNT_ID}:{TP}*",
                        ]
                    },
                },
            }
        ],
    }
    pulumi_mocks.assert_res_counts(
        {
            R.BUCKET: 1,
            R.BUCKET_PUBLIC_ACCESS_BLOCK: 1,
            R.BUCKET_NOTIFICATION: 1,
            R.QUEUE: 1,
            R.QUEUE_POLICY: 1,
        }
    )


def test_bucket_and_topic_write_the_same_queue_policy(pulumi_mocks):
    queue = Queue("jobs")
    bucket = Bucket("uploads")
    topic = Topic("events")
    bucket.notify_queue("on-upload", events=["s3:ObjectCreated:*"], queue=queue)
    sub = topic.subscribe_queue("forward", queue)

    @pulumi.runtime.test
    def deploy():
        return bucket.resources, sub.resources

    deploy()

    # Both write the queue's one policy field, so any difference means the last write wins
    from_bucket = pulumi_mocks.assert_res("uploads-on-upload-subscription-qp", R.QUEUE_POLICY)
    from_topic = pulumi_mocks.assert_res("jobs-events-sns-policy", R.QUEUE_POLICY)
    assert from_bucket.inputs["policy"] == from_topic.inputs["policy"]
    pulumi_mocks.assert_res_counts(
        {
            R.BUCKET: 1,
            R.BUCKET_PUBLIC_ACCESS_BLOCK: 1,
            R.BUCKET_NOTIFICATION: 1,
            R.QUEUE: 1,
            R.QUEUE_POLICY: 2,
            R.TOPIC: 1,
            R.TOPIC_SUBSCRIPTION: 1,
        }
    )


@mark.parametrize(
    ("notify", "target_arn"),
    [
        (
            lambda b: b.notify_queue(
                "on-upload", events=["s3:ObjectCreated:*"], queue=Queue("jobs")
            ),
            f"arn:aws:sqs:{DEFAULT_REGION}:{ACCOUNT_ID}:{tn(TP + 'jobs')}",
        ),
        (
            lambda b: b.notify_topic(
                "on-upload", events=["s3:ObjectCreated:*"], topic=Topic("jobs")
            ),
            f"arn:aws:sns:{DEFAULT_REGION}:{ACCOUNT_ID}:{tn(TP + 'jobs')}",
        ),
    ],
    ids=["queue", "topic"],
)
def test_notify_from_bucket_without_app_prefix_raises(pulumi_mocks, notify, target_arn):
    # The prefix only counts at the start of the name
    bucket = Bucket("uploads", customize={"bucket": {"bucket": f"old-{TP}uploads"}})
    notify(bucket)

    @pulumi.runtime.test
    def deploy():
        return bucket.resources

    with raises(
        ValueError,
        match=re.escape(
            f"arn:aws:s3:::old-{TP}uploads cannot send to {target_arn}: Stelvio's policy on it "
            f"only lets this app's buckets and topics send (names starting with '{TP}'). Keep "
            "that prefix in the custom name, or pass the target's ARN as a string and write its "
            "policy yourself."
        ),
    ):
        deploy()


def test_notify_queue_with_filters(pulumi_mocks):
    """notify_queue() with filters creates proper filter rules."""
    queue = Queue("test-queue")
    bucket = Bucket("test-bucket")

    bucket.notify_queue(
        "on-upload",
        events=["s3:ObjectCreated:Put"],
        filter_prefix="data/",
        filter_suffix=".json",
        queue=queue,
    )

    @pulumi.runtime.test
    def deploy():
        return [queue.resources, bucket.resources]

    deploy()

    [notification] = pulumi_mocks.created(R.BUCKET_NOTIFICATION)
    assert notification.inputs == {
        "bucket": BUCKET_ID,
        "queues": [
            {
                "queueArn": QUEUE_ARN,
                "events": ["s3:ObjectCreated:Put"],
                "filterPrefix": "data/",
                "filterSuffix": ".json",
            }
        ],
    }


def test_notify_queue_arn_string(pulumi_mocks):
    """notify_queue() with ARN string creates proper notification but expects manual policy."""
    bucket = Bucket("test-bucket")

    # Use a queue ARN string instead of Queue component
    queue_arn = "arn:aws:sqs:us-east-1:123456789012:my-external-queue"

    bucket.notify_queue(
        "on-upload",
        events=["s3:ObjectCreated:*"],
        queue=queue_arn,
    )

    @pulumi.runtime.test
    def deploy():
        return bucket.resources

    deploy()

    # SQS queue policy should NOT be created for external queues (prevents overwrite)
    pulumi_mocks.assert_no_res(R.QUEUE_POLICY)
    [notification] = pulumi_mocks.created(R.BUCKET_NOTIFICATION)
    assert notification.inputs == {
        "bucket": BUCKET_ID,
        "queues": [{"queueArn": queue_arn, "events": ["s3:ObjectCreated:*"]}],
    }


# =============================================================================
# Multiple Notifications Tests
# =============================================================================


def test_multiple_function_notifications(pulumi_mocks):
    """Multiple notify_function() calls aggregate into single BucketNotification."""
    bucket = Bucket("test-bucket")

    bucket.notify_function(
        "on-create",
        events=["s3:ObjectCreated:*"],
        function=UPLOAD_HANDLER,
    )

    bucket.notify_function(
        "on-delete",
        events=["s3:ObjectRemoved:*"],
        function=DELETE_HANDLER,
    )

    @pulumi.runtime.test
    def deploy():
        return bucket.resources

    deploy()

    pulumi_mocks.assert_res("test-bucket-on-create", R.FUNCTION)
    pulumi_mocks.assert_res("test-bucket-on-delete", R.FUNCTION)
    permissions = pulumi_mocks.created(R.LAMBDA_PERMISSION)
    assert {p.name: p.inputs["principal"] for p in permissions} == {
        TP + "test-bucket-on-create-subscription-perm": "s3.amazonaws.com",
        TP + "test-bucket-on-delete-subscription-perm": "s3.amazonaws.com",
    }
    # Should have only 1 BucketNotification with 2 lambda configs
    [notification] = pulumi_mocks.created(R.BUCKET_NOTIFICATION)
    assert notification.inputs == {
        "bucket": BUCKET_ID,
        "lambdaFunctions": [
            {
                "lambdaFunctionArn": lambda_arn("test-bucket-on-create"),
                "events": ["s3:ObjectCreated:*"],
            },
            {
                "lambdaFunctionArn": lambda_arn("test-bucket-on-delete"),
                "events": ["s3:ObjectRemoved:*"],
            },
        ],
    }


def test_multiple_notifications_different_filters(pulumi_mocks):
    """Multiple notifications with different filter configurations are aggregated correctly."""
    topic = Topic("test-topic")
    bucket = Bucket("test-bucket")

    bucket.notify_function(
        "process-images",
        events=["s3:ObjectCreated:*"],
        filter_suffix=".jpg",
        function=UPLOAD_HANDLER,
    )

    bucket.notify_topic(
        "process-videos",
        events=["s3:ObjectCreated:*"],
        filter_suffix=".mp4",
        topic=topic,
    )

    @pulumi.runtime.test
    def deploy():
        return [topic.resources, bucket.resources]

    deploy()

    pulumi_mocks.assert_res("test-bucket-process-images", R.FUNCTION)
    [notification] = pulumi_mocks.created(R.BUCKET_NOTIFICATION)
    assert notification.inputs == {
        "bucket": BUCKET_ID,
        "lambdaFunctions": [
            {
                "lambdaFunctionArn": lambda_arn("test-bucket-process-images"),
                "events": ["s3:ObjectCreated:*"],
                "filterSuffix": ".jpg",
            }
        ],
        "topics": [
            {"topicArn": TOPIC_ARN, "events": ["s3:ObjectCreated:*"], "filterSuffix": ".mp4"}
        ],
    }


def test_mixed_function_and_queue_notifications(pulumi_mocks):
    """notify_function() and notify_queue() create proper aggregated notification."""
    queue = Queue("test-queue")
    bucket = Bucket("test-bucket")

    bucket.notify_function(
        "on-create",
        events=["s3:ObjectCreated:*"],
        function=UPLOAD_HANDLER,
    )

    bucket.notify_queue(
        "on-delete",
        events=["s3:ObjectRemoved:*"],
        queue=queue,
    )

    @pulumi.runtime.test
    def deploy():
        return [queue.resources, bucket.resources]

    deploy()

    pulumi_mocks.assert_res("test-bucket-on-create", R.FUNCTION)
    permissions = pulumi_mocks.created(R.LAMBDA_PERMISSION)
    assert {p.name: p.inputs["principal"] for p in permissions} == {
        TP + "test-bucket-on-create-subscription-perm": "s3.amazonaws.com"
    }
    assert len(pulumi_mocks.created(R.QUEUE_POLICY)) == 1
    # Should have only 1 BucketNotification with both configs
    [notification] = pulumi_mocks.created(R.BUCKET_NOTIFICATION)
    assert notification.inputs == {
        "bucket": BUCKET_ID,
        "lambdaFunctions": [
            {
                "lambdaFunctionArn": lambda_arn("test-bucket-on-create"),
                "events": ["s3:ObjectCreated:*"],
            }
        ],
        "queues": [{"queueArn": QUEUE_ARN, "events": ["s3:ObjectRemoved:*"]}],
    }


# =============================================================================
# No Notification Tests
# =============================================================================


def test_bucket_without_notifications(pulumi_mocks):
    """Bucket without notification calls should not create notification resources."""
    bucket = Bucket("test-bucket")

    @pulumi.runtime.test
    def deploy():
        return bucket.resources

    deploy()

    pulumi_mocks.assert_no_res(R.BUCKET_NOTIFICATION)
    assert bucket.resources.bucket_notification is None
    assert bucket.resources.subscriptions == []


# =============================================================================
# BucketResources Tests
# =============================================================================


@mark.parametrize(
    ("kind", "make_target", "created"),
    [
        ("function", lambda: UPLOAD_HANDLER, {"function", "permission"}),
        ("queue", lambda: Queue("test-queue"), {"queue_policy"}),
        ("topic", lambda: Topic("test-topic"), {"topic_policy"}),
    ],
    ids=["function", "queue", "topic"],
)
def test_s3_bucket_resources_with_notification(pulumi_mocks, kind, make_target, created):
    """BucketResources holds the subscription, which keeps its target and only the resources
    that target needs."""
    bucket = Bucket("test-bucket")
    target = make_target()
    notify = getattr(bucket, f"notify_{kind}")
    notify("on-upload", events=["s3:ObjectCreated:*"], **{kind: target})

    @pulumi.runtime.test
    def deploy():
        return bucket.resources

    deploy()

    resources = bucket.resources
    assert resources.bucket_notification is not None
    [sub] = resources.subscriptions
    assert sub.name == "test-bucket-on-upload-subscription"
    assert sub.queue is (target if kind == "queue" else None)
    assert sub.topic is (target if kind == "topic" else None)
    fields = ("function", "permission", "queue_policy", "topic_policy")
    assert {f for f in fields if getattr(sub.resources, f) is not None} == created
    assert len(pulumi_mocks.created(R.BUCKET_NOTIFICATION)) == 1


# =============================================================================
# Links Tests
# =============================================================================


@mark.parametrize(
    "link_args",
    [
        param(lambda t: {"function": UPLOAD_HANDLER, "links": [t]}, id="handler_path"),
        param(lambda t: {"handler": UPLOAD_HANDLER, "links": [t]}, id="handler_in_opts"),
        param(
            lambda t: {"function": FunctionConfig(handler=UPLOAD_HANDLER, links=[t])},
            id="config",
        ),
        param(lambda t: {"function": {"handler": UPLOAD_HANDLER, "links": [t]}}, id="dict"),
    ],
)
def test_notify_function_with_links(pulumi_mocks, link_args):
    """Links reach the notification function's IAM policy in every handler form."""
    table = DynamoTable("test-table", fields={"pk": "string"}, partition_key="pk")
    bucket = Bucket("test-bucket")

    bucket.notify_function("on-upload", events=["s3:ObjectCreated:*"], **link_args(table))

    @pulumi.runtime.test
    def deploy():
        return [table.resources, bucket.resources]

    deploy()

    pulumi_mocks.assert_res("test-bucket-on-upload", R.FUNCTION)
    policy = pulumi_mocks.assert_res("test-bucket-on-upload-p", R.POLICY)
    assert json.loads(policy.inputs["policy"]) == dynamo_link_statements("test-table")
    pulumi_mocks.assert_res_counts(
        {
            R.DYNAMO_TABLE: 1,
            R.BUCKET: 1,
            R.BUCKET_PUBLIC_ACCESS_BLOCK: 1,
            R.BUCKET_NOTIFICATION: 1,
            R.FUNCTION: 1,
            R.LAMBDA_PERMISSION: 1,
            R.ROLE: 1,
            R.POLICY: 1,
            R.ROLE_POLICY_ATTACHMENT: 2,
        }
    )


def test_multiple_notifications_same_queue_creates_single_policy(pulumi_mocks):
    """Multiple notifications to the same queue should create only one QueuePolicy."""
    queue = Queue("test-queue")
    bucket = Bucket("test-bucket")

    bucket.notify_queue(
        "notify-1",
        events=["s3:ObjectCreated:Put"],
        queue=queue,
    )
    bucket.notify_queue(
        "notify-2",
        events=["s3:ObjectRemoved:*"],
        queue=queue,
    )

    @pulumi.runtime.test
    def deploy():
        return [queue.resources, bucket.resources]

    deploy()

    [notification] = pulumi_mocks.created(R.BUCKET_NOTIFICATION)
    assert notification.inputs == {
        "bucket": BUCKET_ID,
        "queues": [
            {"queueArn": QUEUE_ARN, "events": ["s3:ObjectCreated:Put"]},
            {"queueArn": QUEUE_ARN, "events": ["s3:ObjectRemoved:*"]},
        ],
    }
    # BUT should have only 1 QueuePolicy
    assert len(pulumi_mocks.created(R.QUEUE_POLICY)) == 1


# =============================================================================
# Topic Validation Tests
# =============================================================================


def test_notify_topic_requires_events():
    """notify_topic() must raise ValueError when events is empty."""
    bucket = Bucket("test-bucket")
    topic = Topic("test-topic")

    with raises(ValueError, match="events list cannot be empty"):
        bucket.notify_topic(
            "test-notify",
            events=[],
            topic=topic,
        )


def test_notify_topic_validates_event_types():
    """notify_topic() must raise ValueError for invalid event types."""
    bucket = Bucket("test-bucket")
    topic = Topic("test-topic")

    with raises(ValueError, match="Invalid S3 event type"):
        bucket.notify_topic(
            "test-notify",
            events=["s3:InvalidEvent:Type"],
            topic=topic,
        )


def test_notify_topic_rejects_duplicate_names():
    """notify_topic() must raise ValueError for duplicate notification names."""
    bucket = Bucket("test-bucket")
    topic = Topic("test-topic")

    bucket.notify_topic(
        "on-upload",
        events=["s3:ObjectCreated:*"],
        topic=topic,
    )

    with raises(ValueError, match="Notification 'on-upload' already exists"):
        bucket.notify_topic(
            "on-upload",
            events=["s3:ObjectRemoved:*"],
            topic=topic,
        )


# =============================================================================
# Topic Notification Tests
# =============================================================================


def test_notify_topic_creates_resources(pulumi_mocks):
    """notify_topic() creates topic policy and notification."""
    topic = Topic("test-topic")
    bucket = Bucket("test-bucket")

    bucket.notify_topic(
        "on-upload",
        events=["s3:ObjectCreated:*"],
        topic=topic,
    )

    @pulumi.runtime.test
    def deploy():
        return [topic.resources, bucket.resources]

    deploy()

    assert len(pulumi_mocks.created(R.TOPIC_POLICY)) == 1
    [notification] = pulumi_mocks.created(R.BUCKET_NOTIFICATION)
    assert notification.inputs == {
        "bucket": BUCKET_ID,
        "topics": [{"topicArn": TOPIC_ARN, "events": ["s3:ObjectCreated:*"]}],
    }


def test_notify_topic_policy_keeps_default_statement_and_lets_this_apps_buckets_send(
    pulumi_mocks,
):
    topic = Topic("events")
    bucket = Bucket("uploads")
    bucket.notify_topic("on-upload", events=["s3:ObjectCreated:*"], topic=topic)

    @pulumi.runtime.test
    def deploy():
        return bucket.resources

    deploy()

    topic_arn = f"arn:aws:sns:{DEFAULT_REGION}:{ACCOUNT_ID}:{tn(TP + 'events')}"
    policy = pulumi_mocks.assert_res(
        "uploads-on-upload-subscription-tp", R.TOPIC_POLICY, {"arn": topic_arn}, partial=True
    )
    assert json.loads(policy.inputs["policy"]) == {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "__default_statement_ID",
                "Effect": "Allow",
                "Principal": {"AWS": "*"},
                "Action": [
                    "SNS:GetTopicAttributes",
                    "SNS:SetTopicAttributes",
                    "SNS:AddPermission",
                    "SNS:RemovePermission",
                    "SNS:DeleteTopic",
                    "SNS:Subscribe",
                    "SNS:ListSubscriptionsByTopic",
                    "SNS:Publish",
                    "SNS:Receive",
                ],
                "Resource": topic_arn,
                "Condition": {"StringEquals": {"AWS:SourceOwner": ACCOUNT_ID}},
            },
            {
                "Effect": "Allow",
                "Principal": {"Service": "s3.amazonaws.com"},
                "Action": "sns:Publish",
                "Resource": topic_arn,
                "Condition": {
                    "StringEquals": {"aws:SourceAccount": ACCOUNT_ID},
                    "StringLike": {"aws:SourceArn": [f"arn:aws:s3:::{TP}*"]},
                },
            },
        ],
    }
    pulumi_mocks.assert_res_counts(
        {
            R.BUCKET: 1,
            R.BUCKET_PUBLIC_ACCESS_BLOCK: 1,
            R.BUCKET_NOTIFICATION: 1,
            R.TOPIC: 1,
            R.TOPIC_POLICY: 1,
        }
    )


def test_notify_topic_with_filters(pulumi_mocks):
    """notify_topic() with filters creates proper filter rules."""
    topic = Topic("test-topic")
    bucket = Bucket("test-bucket")

    bucket.notify_topic(
        "on-upload",
        events=["s3:ObjectCreated:Put"],
        filter_prefix="logs/",
        filter_suffix=".log",
        topic=topic,
    )

    @pulumi.runtime.test
    def deploy():
        return [topic.resources, bucket.resources]

    deploy()

    [notification] = pulumi_mocks.created(R.BUCKET_NOTIFICATION)
    assert notification.inputs == {
        "bucket": BUCKET_ID,
        "topics": [
            {
                "topicArn": TOPIC_ARN,
                "events": ["s3:ObjectCreated:Put"],
                "filterPrefix": "logs/",
                "filterSuffix": ".log",
            }
        ],
    }


def test_notify_topic_arn_string(pulumi_mocks):
    """notify_topic() with ARN string creates proper notification but no policy."""
    bucket = Bucket("test-bucket")

    # Use a topic ARN string instead of Topic component
    topic_arn = "arn:aws:sns:us-east-1:123456789012:my-external-topic"

    bucket.notify_topic(
        "on-upload",
        events=["s3:ObjectCreated:*"],
        topic=topic_arn,
    )

    @pulumi.runtime.test
    def deploy():
        return bucket.resources

    deploy()

    # SNS topic policy should NOT be created for external topics (prevents overwrite)
    pulumi_mocks.assert_no_res(R.TOPIC_POLICY)
    [notification] = pulumi_mocks.created(R.BUCKET_NOTIFICATION)
    assert notification.inputs == {
        "bucket": BUCKET_ID,
        "topics": [{"topicArn": topic_arn, "events": ["s3:ObjectCreated:*"]}],
    }


def test_multiple_notifications_same_topic_creates_single_policy(pulumi_mocks):
    """Multiple notifications to the same topic should create only one TopicPolicy."""
    topic = Topic("test-topic")
    bucket = Bucket("test-bucket")

    bucket.notify_topic(
        "notify-1",
        events=["s3:ObjectCreated:Put"],
        topic=topic,
    )
    bucket.notify_topic(
        "notify-2",
        events=["s3:ObjectRemoved:*"],
        topic=topic,
    )

    @pulumi.runtime.test
    def deploy():
        return [topic.resources, bucket.resources]

    deploy()

    [notification] = pulumi_mocks.created(R.BUCKET_NOTIFICATION)
    assert notification.inputs == {
        "bucket": BUCKET_ID,
        "topics": [
            {"topicArn": TOPIC_ARN, "events": ["s3:ObjectCreated:Put"]},
            {"topicArn": TOPIC_ARN, "events": ["s3:ObjectRemoved:*"]},
        ],
    }
    # BUT should have only 1 TopicPolicy
    assert len(pulumi_mocks.created(R.TOPIC_POLICY)) == 1


def test_mixed_function_queue_and_topic_notifications(pulumi_mocks):
    """All three notify methods create proper aggregated notification."""
    queue = Queue("test-queue")
    topic = Topic("test-topic")
    bucket = Bucket("test-bucket")

    bucket.notify_function(
        "on-create",
        events=["s3:ObjectCreated:*"],
        function=UPLOAD_HANDLER,
    )

    bucket.notify_queue(
        "on-delete",
        events=["s3:ObjectRemoved:*"],
        queue=queue,
    )

    bucket.notify_topic(
        "on-restore",
        events=["s3:ObjectRestore:*"],
        topic=topic,
    )

    @pulumi.runtime.test
    def deploy():
        return [queue.resources, topic.resources, bucket.resources]

    deploy()

    pulumi_mocks.assert_res("test-bucket-on-create", R.FUNCTION)
    permissions = pulumi_mocks.created(R.LAMBDA_PERMISSION)
    assert {p.name: p.inputs["principal"] for p in permissions} == {
        TP + "test-bucket-on-create-subscription-perm": "s3.amazonaws.com"
    }
    assert len(pulumi_mocks.created(R.QUEUE_POLICY)) == 1
    assert len(pulumi_mocks.created(R.TOPIC_POLICY)) == 1
    # Should have only 1 BucketNotification with all configs
    [notification] = pulumi_mocks.created(R.BUCKET_NOTIFICATION)
    assert notification.inputs == {
        "bucket": BUCKET_ID,
        "lambdaFunctions": [
            {
                "lambdaFunctionArn": lambda_arn("test-bucket-on-create"),
                "events": ["s3:ObjectCreated:*"],
            }
        ],
        "queues": [{"queueArn": QUEUE_ARN, "events": ["s3:ObjectRemoved:*"]}],
        "topics": [{"topicArn": TOPIC_ARN, "events": ["s3:ObjectRestore:*"]}],
    }


@mark.parametrize(
    "notify",
    [
        lambda b: b.notify_function(
            "on-upload", events=["s3:ObjectCreated:*"], function=UPLOAD_HANDLER
        ),
        lambda b: b.notify_queue(
            "on-upload", events=["s3:ObjectCreated:*"], queue=Queue("notify-queue")
        ),
        lambda b: b.notify_topic(
            "on-upload", events=["s3:ObjectCreated:*"], topic=Topic("notify-topic")
        ),
    ],
    ids=["function", "queue", "topic"],
)
@pulumi.runtime.test
def test_bucket_notify_subscription_parented_to_bucket(pulumi_mocks, notify):
    bucket = Bucket("parented-bucket")
    sub = notify(bucket)
    _ = bucket.resources

    def check(urn):
        assert urn == (
            "urn:pulumi:stack::project::stelvio:aws:Bucket$stelvio:aws:BucketNotifySubscription"
            "::parented-bucket-on-upload-subscription"
        )

    return sub.urn.apply(check)
