import pytest

from stelvio.aws.queue import Queue

from .assert_helpers import (
    assert_event_source_mapping,
    assert_lambda_function,
    assert_sqs_queue,
)

pytestmark = pytest.mark.integration


def test_queue_basic(stelvio_env):
    def infra():
        Queue("tasks")

    outputs = stelvio_env.deploy(infra)

    assert_sqs_queue(outputs["queue_tasks_url"])


def test_queue_fifo(stelvio_env):
    def infra():
        Queue("orders.fifo", fifo=True)

    outputs = stelvio_env.deploy(infra)

    assert_sqs_queue(outputs["queue_orders.fifo_url"], fifo=True)


def test_queue_config(stelvio_env):
    def infra():
        Queue("jobs", visibility_timeout=120, delay=10, retention=86400)

    outputs = stelvio_env.deploy(infra)

    assert_sqs_queue(
        outputs["queue_jobs_url"],
        visibility_timeout=120,
        delay=10,
        retention=86400,
    )


def test_queue_dlq(stelvio_env):
    def infra():
        dlq = Queue("failures")
        Queue("work", dlq=dlq)

    outputs = stelvio_env.deploy(infra)

    assert_sqs_queue(outputs["queue_failures_url"])
    assert_sqs_queue(
        outputs["queue_work_url"],
        dlq_arn=outputs["queue_failures_arn"],
    )


def test_queue_subscribe(stelvio_env, project_dir):
    def infra():
        queue = Queue("tasks")
        queue.subscribe("processor", "handlers/echo.main", batch_size=5)

    outputs = stelvio_env.deploy(infra)

    assert_sqs_queue(outputs["queue_tasks_url"])

    function_arn = outputs["function_tasks-processor_arn"]
    assert_lambda_function(function_arn)

    assert_event_source_mapping(
        function_arn,
        event_source_arn=outputs["queue_tasks_arn"],
        batch_size=5,
    )
