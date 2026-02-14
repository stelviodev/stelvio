import pytest

from stelvio.aws.topic import Topic

from .assert_helpers import assert_sns_topic


@pytest.mark.integration
def test_topic(stelvio_env):
    def infra():
        Topic("notifications")

    outputs = stelvio_env.deploy(infra)

    assert_sns_topic(outputs["topic_notifications_arn"])
