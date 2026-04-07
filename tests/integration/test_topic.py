import pytest

from stelvio.aws.queue import Queue
from stelvio.aws.topic import Topic

from .assert_helpers import (
    assert_lambda_function,
    assert_lambda_tags,
    assert_sns_subscription,
    assert_sns_tags,
    assert_sns_topic,
    assert_sqs_queue,
)
from .export_helpers import export_function, export_queue, export_topic

pytestmark = pytest.mark.integration


# --- Properties ---


def test_topic_basic(stelvio_env):
    def infra():
        t = Topic("notifications")
        export_topic(t)

    outputs = stelvio_env.deploy(infra)

    assert_sns_topic(outputs["topic_notifications_arn"], fifo=False)


def test_topic_fifo(stelvio_env):
    def infra():
        t = Topic("orders", fifo=True)
        export_topic(t)

    outputs = stelvio_env.deploy(infra)

    assert_sns_topic(outputs["topic_orders_arn"], fifo=True)


def test_topic_tags(stelvio_env):
    def infra():
        t = Topic("tagged-topic", tags={"Team": "platform"})
        export_topic(t)

    outputs = stelvio_env.deploy(infra)
    assert_sns_tags(outputs["topic_tagged-topic_arn"], {"Team": "platform"})


# --- Subscribe (Lambda) ---


def test_topic_subscribe(stelvio_env, project_dir):
    def infra():
        topic = Topic("alerts")
        sub = topic.subscribe("handler", "handlers/echo.main")
        export_topic(topic)
        export_function(sub.resources.function)

    outputs = stelvio_env.deploy(infra)

    topic_arn = outputs["topic_alerts_arn"]
    assert_sns_topic(topic_arn)

    function_arn = outputs["function_alerts-handler_arn"]
    assert_lambda_function(function_arn)

    assert_sns_subscription(topic_arn, protocol="lambda", endpoint=function_arn)


def test_topic_subscribe_propagates_tags_to_generated_function(stelvio_env, project_dir):
    def infra():
        topic = Topic("tagged-sub-topic", tags={"Team": "platform"})
        sub = topic.subscribe("worker", "handlers/echo.main")
        export_function(sub.resources.function)

    outputs = stelvio_env.deploy(infra)
    assert_lambda_tags(outputs["function_tagged-sub-topic-worker_arn"], {"Team": "platform"})


def test_topic_subscribe_with_filter(stelvio_env, project_dir):
    def infra():
        topic = Topic("alerts")
        sub = topic.subscribe(
            "urgent-only",
            "handlers/echo.main",
            filter_={"status": ["urgent"]},
        )
        export_topic(topic)
        export_function(sub.resources.function)

    outputs = stelvio_env.deploy(infra)

    topic_arn = outputs["topic_alerts_arn"]
    function_arn = outputs["function_alerts-urgent-only_arn"]

    assert_sns_subscription(
        topic_arn,
        protocol="lambda",
        endpoint=function_arn,
        has_filter_policy=True,
    )


# --- Subscribe queue ---


def test_topic_subscribe_queue(stelvio_env):
    def infra():
        topic = Topic("events")
        queue = Queue("processor")
        topic.subscribe_queue("forward", queue)
        export_topic(topic)
        export_queue(queue)

    outputs = stelvio_env.deploy(infra)

    topic_arn = outputs["topic_events_arn"]
    queue_arn = outputs["queue_processor_arn"]

    assert_sns_topic(topic_arn)
    assert_sns_subscription(topic_arn, protocol="sqs", endpoint=queue_arn)


def test_topic_subscribe_queue_raw_message(stelvio_env):
    def infra():
        topic = Topic("events")
        queue = Queue("raw-consumer")
        topic.subscribe_queue("raw", queue, raw_message_delivery=True)
        export_topic(topic)
        export_queue(queue)

    outputs = stelvio_env.deploy(infra)

    topic_arn = outputs["topic_events_arn"]
    queue_arn = outputs["queue_raw-consumer_arn"]

    assert_sns_subscription(
        topic_arn,
        protocol="sqs",
        endpoint=queue_arn,
        raw_message_delivery=True,
    )


def test_topic_fifo_subscribe_queue(stelvio_env):
    def infra():
        topic = Topic("orders", fifo=True)
        queue = Queue("order-processor.fifo", fifo=True)
        topic.subscribe_queue("process", queue)
        export_topic(topic)
        export_queue(queue)

    outputs = stelvio_env.deploy(infra)

    topic_arn = outputs["topic_orders_arn"]
    assert_sns_topic(topic_arn, fifo=True)

    queue_arn = outputs["queue_order-processor.fifo_arn"]
    assert_sqs_queue(outputs["queue_order-processor.fifo_url"], fifo=True)

    assert_sns_subscription(topic_arn, protocol="sqs", endpoint=queue_arn)


def test_topic_subscribe_queue_with_filter(stelvio_env):
    def infra():
        topic = Topic("events")
        queue = Queue("filtered")
        topic.subscribe_queue("filtered", queue, filter_={"status": ["active"]})
        export_topic(topic)
        export_queue(queue)

    outputs = stelvio_env.deploy(infra)

    topic_arn = outputs["topic_events_arn"]
    queue_arn = outputs["queue_filtered_arn"]

    assert_sns_subscription(
        topic_arn,
        protocol="sqs",
        endpoint=queue_arn,
        has_filter_policy=True,
    )


# --- Multiple subscriptions ---


def test_topic_multiple_subscriptions(stelvio_env, project_dir):
    def infra():
        topic = Topic("events")
        queue = Queue("analytics")
        sub = topic.subscribe("handler", "handlers/echo.main")
        topic.subscribe_queue("analytics", queue)
        export_topic(topic)
        export_queue(queue)
        export_function(sub.resources.function)

    outputs = stelvio_env.deploy(infra)

    topic_arn = outputs["topic_events_arn"]
    function_arn = outputs["function_events-handler_arn"]
    queue_arn = outputs["queue_analytics_arn"]

    assert_lambda_function(function_arn)
    assert_sns_subscription(topic_arn, protocol="lambda", endpoint=function_arn)
    assert_sns_subscription(topic_arn, protocol="sqs", endpoint=queue_arn)
