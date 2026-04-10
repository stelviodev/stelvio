import pytest

from stelvio.aws.dynamo_db import DynamoTable
from stelvio.aws.queue import Queue
from stelvio.aws.s3 import Bucket
from stelvio.aws.topic import Topic

from .assert_helpers import (
    assert_lambda_function,
    assert_lambda_tags,
    assert_s3_bucket,
    assert_s3_bucket_notifications,
    assert_s3_bucket_tags,
)
from .export_helpers import export_bucket, export_dynamo_table, export_function

pytestmark = pytest.mark.integration


# --- Properties ---


def test_bucket_basic(stelvio_env):
    def infra():
        b = Bucket("files")
        export_bucket(b)

    outputs = stelvio_env.deploy(infra)

    assert_s3_bucket(outputs["s3bucket_files_name"], public_access_blocked=True)


def test_bucket_versioning(stelvio_env):
    def infra():
        b = Bucket("data", versioning=True)
        export_bucket(b)

    outputs = stelvio_env.deploy(infra)

    assert_s3_bucket(
        outputs["s3bucket_data_name"],
        public_access_blocked=True,
        versioning=True,
    )


def test_bucket_public_access(stelvio_env):
    def infra():
        b = Bucket("public-assets", access="public")
        export_bucket(b)

    outputs = stelvio_env.deploy(infra)

    assert_s3_bucket(outputs["s3bucket_public-assets_name"], public_access_blocked=False)


def test_bucket_tags(stelvio_env):
    def infra():
        b = Bucket("tagged-bucket", tags={"Team": "platform"})
        export_bucket(b)

    outputs = stelvio_env.deploy(infra)
    assert_s3_bucket_tags(outputs["s3bucket_tagged-bucket_name"], {"Team": "platform"})


# --- Notifications ---


def test_bucket_notify_function(stelvio_env, project_dir):
    def infra():
        bucket = Bucket("uploads")
        sub = bucket.notify_function(
            "on-upload",
            events=["s3:ObjectCreated:*"],
            function="handlers/echo.main",
        )
        export_bucket(bucket)
        export_function(sub.resources.function)

    outputs = stelvio_env.deploy(infra)

    bucket_name = outputs["s3bucket_uploads_name"]
    assert_s3_bucket(bucket_name)
    assert_lambda_function(outputs["function_uploads-on-upload_arn"])
    assert_s3_bucket_notifications(bucket_name, lambda_count=1)


def test_bucket_notify_function_propagates_tags(stelvio_env, project_dir):
    def infra():
        bucket = Bucket("tagged-uploads", tags={"Team": "platform"})
        bucket.notify_function(
            "on-upload",
            events=["s3:ObjectCreated:*"],
            function="handlers/echo.main",
        )
        # Access function via bucket.resources to ensure proper creation order
        # (BucketNotifySubscription needs _bucket_arn set by Bucket._create_resources)
        sub = bucket.resources.subscriptions[0]
        export_function(sub.resources.function)

    outputs = stelvio_env.deploy(infra)
    assert_lambda_tags(outputs["function_tagged-uploads-on-upload_arn"], {"Team": "platform"})


def test_bucket_notify_queue(stelvio_env):
    def infra():
        bucket = Bucket("images")
        queue = Queue("processor")
        bucket.notify_queue(
            "on-upload",
            events=["s3:ObjectCreated:*"],
            queue=queue,
        )
        export_bucket(bucket)

    outputs = stelvio_env.deploy(infra)

    bucket_name = outputs["s3bucket_images_name"]
    assert_s3_bucket(bucket_name)
    assert_s3_bucket_notifications(bucket_name, queue_count=1)


def test_bucket_notify_topic(stelvio_env):
    def infra():
        bucket = Bucket("documents")
        topic = Topic("doc-events")
        bucket.notify_topic(
            "on-upload",
            events=["s3:ObjectCreated:*"],
            topic=topic,
        )
        export_bucket(bucket)

    outputs = stelvio_env.deploy(infra)

    bucket_name = outputs["s3bucket_documents_name"]
    assert_s3_bucket(bucket_name)
    assert_s3_bucket_notifications(bucket_name, topic_count=1)


def test_bucket_notify_function_with_filter(stelvio_env, project_dir):
    def infra():
        bucket = Bucket("assets")
        bucket.notify_function(
            "on-image",
            events=["s3:ObjectCreated:*"],
            function="handlers/echo.main",
            filter_prefix="images/",
            filter_suffix=".jpg",
        )
        export_bucket(bucket)

    outputs = stelvio_env.deploy(infra)

    bucket_name = outputs["s3bucket_assets_name"]
    assert_s3_bucket_notifications(bucket_name, lambda_count=1, has_filter=True)


def test_bucket_multiple_notifications(stelvio_env, project_dir):
    def infra():
        bucket = Bucket("media")
        queue = Queue("thumbnails")
        bucket.notify_function(
            "on-upload",
            events=["s3:ObjectCreated:*"],
            function="handlers/echo.main",
            filter_prefix="uploads/",
        )
        bucket.notify_queue(
            "to-queue",
            events=["s3:ObjectRemoved:*"],
            queue=queue,
        )
        export_bucket(bucket)

    outputs = stelvio_env.deploy(infra)

    bucket_name = outputs["s3bucket_media_name"]
    assert_s3_bucket_notifications(bucket_name, lambda_count=1, queue_count=1)


def test_bucket_notify_function_with_links(stelvio_env, project_dir):
    def infra():
        table = DynamoTable("results", fields={"pk": "S"}, partition_key="pk")
        bucket = Bucket("inbox")
        sub = bucket.notify_function(
            "process",
            events=["s3:ObjectCreated:*"],
            function="handlers/echo.main",
            links=[table],
        )
        export_bucket(bucket)
        export_dynamo_table(table)
        export_function(sub.resources.function)

    outputs = stelvio_env.deploy(infra)

    bucket_name = outputs["s3bucket_inbox_name"]
    assert_s3_bucket_notifications(bucket_name, lambda_count=1)
    assert_lambda_function(
        outputs["function_inbox-process_arn"],
        environment={"STLV_RESULTS_TABLE_NAME": outputs["dynamotable_results_name"]},
    )
