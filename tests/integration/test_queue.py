import pytest

from stelvio.aws.queue import Queue

from .assert_helpers import assert_sqs_queue


@pytest.mark.integration
def test_queue(stelvio_env):
    def infra():
        Queue("tasks")

    outputs = stelvio_env.deploy(infra)

    assert_sqs_queue(outputs["queue_tasks_url"])
