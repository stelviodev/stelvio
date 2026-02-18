"""Scenario tests: async event triggers.

Verifies that event sources (SQS, SNS, S3, DynamoDB Streams, Cron) actually
trigger Lambda functions. Each test deploys a results DynamoDB table + the
event_recorder handler, triggers an event, then polls the results table.
"""

import json
import time

import pytest

from stelvio.aws.cron import Cron
from stelvio.aws.dynamo_db import DynamoTable
from stelvio.aws.queue import Queue
from stelvio.aws.s3 import Bucket
from stelvio.aws.topic import Topic

from .assert_helpers import (
    drain_sqs,
    poll_dynamo_items,
    poll_sqs_messages,
    publish_sns_message,
    put_dynamo_item,
    send_sqs_message,
    upload_s3_object,
    wait_for_event_source_mapping,
)

pytestmark = pytest.mark.integration


def _results_table():
    """Create a DynamoDB table for recording events. Reused by all async tests."""
    return DynamoTable("results", fields={"pk": "S"}, partition_key="pk")


# --- Queue ---


def test_scenario_queue_triggers_lambda(stelvio_env, project_dir):
    """SQS message triggers Lambda which writes to results table."""

    def infra():
        results = _results_table()
        queue = Queue("jobs")
        queue.subscribe("worker", "handlers/event_recorder.main", links=[results])

    outputs = stelvio_env.deploy(infra)

    # Trigger: send message to queue
    send_sqs_message(outputs["queue_jobs_url"], {"task": "process-123"})

    # Poll: wait for event_recorder to write to results table
    items = poll_dynamo_items(outputs["dynamotable_results_name"])
    assert len(items) >= 1
    event = json.loads(items[0]["event"])
    # SQS event wraps message in Records[].body
    assert "process-123" in json.dumps(event)


# --- Topic ---


def test_scenario_topic_triggers_lambda(stelvio_env, project_dir):
    """SNS message triggers Lambda subscriber which writes to results table."""

    def infra():
        results = _results_table()
        topic = Topic("alerts")
        topic.subscribe("handler", "handlers/event_recorder.main", links=[results])

    outputs = stelvio_env.deploy(infra)

    # Trigger: publish to topic
    publish_sns_message(outputs["topic_alerts_arn"], {"alert": "server-down"})

    # Poll: wait for event_recorder
    items = poll_dynamo_items(outputs["dynamotable_results_name"])
    assert len(items) >= 1
    event = json.loads(items[0]["event"])
    # SNS event wraps message in Records[].Sns.Message
    assert "server-down" in json.dumps(event)


def test_scenario_topic_fanout_to_queue(stelvio_env, project_dir):
    """SNS publishes to SQS queue via subscribe_queue — no Lambda involved."""

    def infra():
        topic = Topic("events")
        inbox = Queue("inbox")
        topic.subscribe_queue("forward", inbox)

    outputs = stelvio_env.deploy(infra)

    # Trigger: publish to topic
    publish_sns_message(outputs["topic_events_arn"], {"event": "user-signup"})

    # Poll: message should appear in the queue
    messages = poll_sqs_messages(outputs["queue_inbox_url"])
    assert len(messages) >= 1
    # SNS wraps the message — the body is the SNS notification JSON
    assert "user-signup" in json.dumps(messages)


# --- S3 ---


def test_scenario_s3_triggers_lambda(stelvio_env, project_dir):
    """S3 upload triggers notify_function Lambda which writes to results table."""

    def infra():
        results = _results_table()
        bucket = Bucket("uploads")
        bucket.notify_function(
            "processor",
            events=["s3:ObjectCreated:*"],
            function="handlers/event_recorder.main",
            links=[results],
        )

    outputs = stelvio_env.deploy(infra)

    # Trigger: upload a file
    upload_s3_object(outputs["s3bucket_uploads_name"], "test/file.txt", "hello")

    # Poll: wait for event_recorder
    items = poll_dynamo_items(outputs["dynamotable_results_name"])
    assert len(items) >= 1
    event = json.loads(items[0]["event"])
    # S3 event contains the bucket name and key in Records[].s3
    assert "file.txt" in json.dumps(event)


def test_scenario_s3_triggers_queue(stelvio_env, project_dir):
    """S3 upload triggers notify_queue — message appears in SQS."""

    def infra():
        bucket = Bucket("inbox")
        queue = Queue("notifications")
        bucket.notify_queue(
            "on-upload",
            events=["s3:ObjectCreated:*"],
            queue=queue,
        )

    outputs = stelvio_env.deploy(infra)
    queue_url = outputs["queue_notifications_url"]

    # Wait for and drain the S3 test event that AWS sends on notification setup
    time.sleep(10)
    drain_sqs(queue_url)

    # Trigger: upload a file
    upload_s3_object(outputs["s3bucket_inbox_name"], "doc.pdf", "content")

    # Poll: S3 notification should appear in queue
    messages = poll_sqs_messages(queue_url)
    assert len(messages) >= 1
    # S3 event notifications wrap records — check the key is present
    assert "doc.pdf" in json.dumps(messages)


def test_scenario_s3_triggers_topic(stelvio_env, project_dir):
    """S3 upload triggers notify_topic — message flows through to subscribed queue."""

    def infra():
        bucket = Bucket("files")
        topic = Topic("file-events")
        queue = Queue("listener")
        bucket.notify_topic(
            "on-upload",
            events=["s3:ObjectCreated:*"],
            topic=topic,
        )
        # Subscribe a queue to the topic so we can poll for the message
        topic.subscribe_queue("forward", queue)

    outputs = stelvio_env.deploy(infra)

    # Trigger: upload a file
    upload_s3_object(outputs["s3bucket_files_name"], "image.png", "pixels")

    # Poll: message should flow S3 → Topic → Queue
    messages = poll_sqs_messages(outputs["queue_listener_url"])
    assert len(messages) >= 1
    assert "image.png" in json.dumps(messages)


# --- DynamoDB Streams ---


def test_scenario_dynamo_stream_triggers_lambda(stelvio_env, project_dir):
    """DynamoDB Stream triggers Lambda which writes to results table."""

    def infra():
        results = _results_table()
        source = DynamoTable(
            "source",
            fields={"pk": "S"},
            partition_key="pk",
            stream="new-image",
        )
        source.subscribe("processor", "handlers/event_recorder.main", links=[results])

    outputs = stelvio_env.deploy(infra)
    source_table = outputs["dynamotable_source_name"]
    results_table = outputs["dynamotable_results_name"]

    # Wait for event source mapping to reach "Enabled" state.
    wait_for_event_source_mapping(outputs["function_source-processor_arn"])

    # DynamoDB Streams ESM with starting_position=LATEST needs time to discover
    # shards and start polling even after reaching "Enabled" state. Write items
    # periodically so the ESM picks one up once it starts reading.
    deadline = time.monotonic() + 180
    write_counter = 0

    while time.monotonic() < deadline:
        write_counter += 1
        put_dynamo_item(source_table, {"pk": f"change-{write_counter}", "data": "test"})
        time.sleep(15)

        try:
            items = poll_dynamo_items(results_table, timeout=5)
        except AssertionError:
            continue

        event = json.loads(items[0]["event"])
        # DynamoDB Stream event contains Records[].dynamodb
        assert "dynamodb" in json.dumps(event)
        return

    raise AssertionError(
        f"DynamoDB Stream never triggered Lambda after {write_counter} writes over 180s"
    )


# --- Cron ---


def test_scenario_cron_triggers_lambda(stelvio_env, project_dir):
    """Cron fires every minute — verify Lambda is invoked at least once.

    This test waits up to 120s for the first cron fire. The cron schedule
    is rate(1 minute), so the first invocation happens within 0-60s after
    deploy completes, plus some EventBridge propagation time.
    """

    def infra():
        results = _results_table()
        Cron(
            "ticker",
            "rate(1 minute)",
            "handlers/event_recorder.main",
            links=[results],
        )

    outputs = stelvio_env.deploy(infra)

    # Poll: wait for cron to fire (up to 120s)
    items = poll_dynamo_items(outputs["dynamotable_results_name"], timeout=120)
    assert len(items) >= 1
