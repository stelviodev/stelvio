import pytest

from stelvio.aws.queue import DlqConfig, Queue

from .assert_helpers import (
    assert_event_source_mapping,
    assert_lambda_function,
    assert_lambda_tags,
    assert_sqs_queue,
    assert_sqs_tags,
)
from .export_helpers import export_function, export_queue

pytestmark = pytest.mark.integration


# --- Properties ---


def test_queue_basic(stelvio_env):
    def infra():
        q = Queue("tasks")
        export_queue(q)

    outputs = stelvio_env.deploy(infra)

    assert_sqs_queue(outputs["queue_tasks_url"], visibility_timeout=60)


def test_queue_fifo(stelvio_env):
    def infra():
        q = Queue("orders.fifo", fifo=True)
        export_queue(q)

    outputs = stelvio_env.deploy(infra)

    assert_sqs_queue(outputs["queue_orders.fifo_url"], fifo=True)


def test_queue_tags(stelvio_env):
    def infra():
        q = Queue("tagged-queue", tags={"Team": "platform"})
        export_queue(q)

    outputs = stelvio_env.deploy(infra)
    assert_sqs_tags(outputs["queue_tagged-queue_url"], {"Team": "platform"})


def test_queue_config(stelvio_env):
    def infra():
        q = Queue("jobs", visibility_timeout=120, delay=10, retention=86400)
        export_queue(q)

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
        q = Queue("work", dlq=dlq)
        export_queue(dlq)
        export_queue(q)

    outputs = stelvio_env.deploy(infra)

    assert_sqs_queue(outputs["queue_failures_url"])
    assert_sqs_queue(
        outputs["queue_work_url"],
        dlq_arn=outputs["queue_failures_arn"],
        dlq_retry=3,
    )


def test_queue_dlq_custom_retry(stelvio_env):
    def infra():
        dlq = Queue("failures")
        q = Queue("work", dlq=DlqConfig(queue=dlq, retry=5))
        export_queue(dlq)
        export_queue(q)

    outputs = stelvio_env.deploy(infra)

    assert_sqs_queue(outputs["queue_failures_url"])
    assert_sqs_queue(
        outputs["queue_work_url"],
        dlq_arn=outputs["queue_failures_arn"],
        dlq_retry=5,
    )


# --- Subscribe ---


def test_queue_subscribe(stelvio_env, project_dir):
    def infra():
        queue = Queue("tasks")
        sub = queue.subscribe("processor", "handlers/echo.main", batch_size=5)
        export_queue(queue)
        export_function(sub.resources.function)

    outputs = stelvio_env.deploy(infra)

    assert_sqs_queue(outputs["queue_tasks_url"])

    function_arn = outputs["function_tasks-processor_arn"]
    assert_lambda_function(function_arn)

    assert_event_source_mapping(
        function_arn,
        event_source_arn=outputs["queue_tasks_arn"],
        batch_size=5,
    )


def test_queue_subscribe_propagates_tags_to_generated_function(stelvio_env, project_dir):
    def infra():
        queue = Queue("tagged-jobs", tags={"Team": "platform"})
        sub = queue.subscribe("worker", "handlers/echo.main")
        export_function(sub.resources.function)

    outputs = stelvio_env.deploy(infra)
    assert_lambda_tags(outputs["function_tagged-jobs-worker_arn"], {"Team": "platform"})


def test_queue_fifo_subscribe(stelvio_env, project_dir):
    def infra():
        queue = Queue("jobs.fifo", fifo=True)
        sub = queue.subscribe("worker", "handlers/echo.main", batch_size=5)
        export_queue(queue)
        export_function(sub.resources.function)

    outputs = stelvio_env.deploy(infra)

    assert_sqs_queue(outputs["queue_jobs.fifo_url"], fifo=True)

    function_arn = outputs["function_jobs-fifo-worker_arn"]
    assert_lambda_function(function_arn)

    assert_event_source_mapping(
        function_arn,
        event_source_arn=outputs["queue_jobs.fifo_arn"],
        batch_size=5,
    )


def test_queue_subscribe_with_filter(stelvio_env, project_dir):
    def infra():
        queue = Queue("events")
        sub = queue.subscribe(
            "high-priority",
            "handlers/echo.main",
            filters=[{"body": {"priority": ["high"]}}],
        )
        export_queue(queue)
        export_function(sub.resources.function)

    outputs = stelvio_env.deploy(infra)

    assert_sqs_queue(outputs["queue_events_url"])

    assert_event_source_mapping(
        outputs["function_events-high-priority_arn"],
        event_source_arn=outputs["queue_events_arn"],
        has_filter_criteria=True,
    )


def test_queue_multiple_subscriptions(stelvio_env, project_dir):
    def infra():
        queue = Queue("orders")
        sub1 = queue.subscribe("processor", "handlers/echo.main", batch_size=10)
        sub2 = queue.subscribe("auditor", "handlers/echo.main", batch_size=1)
        export_queue(queue)
        export_function(sub1.resources.function)
        export_function(sub2.resources.function)

    outputs = stelvio_env.deploy(infra)

    queue_arn = outputs["queue_orders_arn"]

    processor_arn = outputs["function_orders-processor_arn"]
    assert_lambda_function(processor_arn)
    assert_event_source_mapping(processor_arn, event_source_arn=queue_arn, batch_size=10)

    auditor_arn = outputs["function_orders-auditor_arn"]
    assert_lambda_function(auditor_arn)
    assert_event_source_mapping(auditor_arn, event_source_arn=queue_arn, batch_size=1)
