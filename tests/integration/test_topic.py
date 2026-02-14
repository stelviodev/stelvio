import pytest

from stelvio.aws.queue import Queue
from stelvio.aws.topic import Topic

from .assert_helpers import (
    assert_lambda_function,
    assert_sns_subscription,
    assert_sns_topic,
)


@pytest.mark.integration
def test_topic_basic(stelvio_env):
    def infra():
        Topic("notifications")

    outputs = stelvio_env.deploy(infra)

    assert_sns_topic(outputs["topic_notifications_arn"])


@pytest.mark.integration
def test_topic_fifo(stelvio_env):
    def infra():
        Topic("orders", fifo=True)

    outputs = stelvio_env.deploy(infra)

    assert_sns_topic(outputs["topic_orders_arn"], fifo=True)


@pytest.mark.integration
def test_topic_subscribe(stelvio_env, project_dir):
    def infra():
        topic = Topic("alerts")
        topic.subscribe("handler", "handlers/echo.main")

    outputs = stelvio_env.deploy(infra)

    topic_arn = outputs["topic_alerts_arn"]
    assert_sns_topic(topic_arn)

    function_arn = outputs["function_alerts-handler_arn"]
    assert_lambda_function(function_arn)

    assert_sns_subscription(topic_arn, protocol="lambda", endpoint=function_arn)


@pytest.mark.integration
def test_topic_subscribe_queue(stelvio_env):
    def infra():
        topic = Topic("events")
        queue = Queue("processor")
        topic.subscribe_queue("forward", queue)

    outputs = stelvio_env.deploy(infra)

    topic_arn = outputs["topic_events_arn"]
    queue_arn = outputs["queue_processor_arn"]

    assert_sns_topic(topic_arn)
    assert_sns_subscription(topic_arn, protocol="sqs", endpoint=queue_arn)


# Future test ideas:
# - Subscribe with filter_ policy
# - subscribe_queue with raw_message_delivery=True
# - FIFO topic with subscribe_queue (FIFO queue)
# - Multiple subscriptions on same topic
