import pytest

from stelvio.aws.queue import Queue
from stelvio.aws.s3 import Bucket

from .assert_helpers import (
    assert_lambda_function,
    assert_s3_bucket,
    assert_s3_bucket_notifications,
)


@pytest.mark.integration
def test_bucket_basic(stelvio_env):
    def infra():
        Bucket("files")

    outputs = stelvio_env.deploy(infra)

    assert_s3_bucket(outputs["s3bucket_files_name"], public_access_blocked=True)


@pytest.mark.integration
def test_bucket_versioning(stelvio_env):
    def infra():
        Bucket("data", versioning=True)

    outputs = stelvio_env.deploy(infra)

    assert_s3_bucket(
        outputs["s3bucket_data_name"],
        public_access_blocked=True,
        versioning=True,
    )


@pytest.mark.integration
def test_bucket_public_access(stelvio_env):
    def infra():
        Bucket("public-assets", access="public")

    outputs = stelvio_env.deploy(infra)

    assert_s3_bucket(outputs["s3bucket_public-assets_name"], public_access_blocked=False)


@pytest.mark.integration
def test_bucket_notify_function(stelvio_env, project_dir):
    def infra():
        bucket = Bucket("uploads")
        bucket.notify_function(
            "on-upload",
            events=["s3:ObjectCreated:*"],
            function="handlers/echo.main",
        )

    outputs = stelvio_env.deploy(infra)

    bucket_name = outputs["s3bucket_uploads_name"]
    assert_s3_bucket(bucket_name)
    assert_lambda_function(outputs["function_uploads-on-upload_arn"])
    assert_s3_bucket_notifications(bucket_name, lambda_count=1)


@pytest.mark.integration
def test_bucket_notify_queue(stelvio_env):
    def infra():
        bucket = Bucket("images")
        queue = Queue("processor")
        bucket.notify_queue(
            "on-upload",
            events=["s3:ObjectCreated:*"],
            queue=queue,
        )

    outputs = stelvio_env.deploy(infra)

    bucket_name = outputs["s3bucket_images_name"]
    assert_s3_bucket(bucket_name)
    assert_s3_bucket_notifications(bucket_name, queue_count=1)


# Future test ideas:
# - notify_topic (SNS notification)
# - Notification with filter_prefix/filter_suffix
# - Multiple notifications on same bucket (Lambda + Queue)
# - notify_function with links
