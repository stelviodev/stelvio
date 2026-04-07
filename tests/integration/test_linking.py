"""Integration tests for the Stelvio link system.

Verifies that linking a Function to other components (DynamoDB, SQS, SNS, S3,
Lambda) correctly injects STLV_ environment variables and grants the expected
IAM permissions on real AWS resources.
"""

import pytest

from stelvio.aws.dynamo_db import DynamoTable
from stelvio.aws.function import Function
from stelvio.aws.queue import Queue
from stelvio.aws.s3 import Bucket
from stelvio.aws.topic import Topic

from .assert_helpers import assert_lambda_function, assert_lambda_role_permissions
from .export_helpers import (
    export_bucket,
    export_dynamo_table,
    export_function,
    export_queue,
    export_topic,
)

pytestmark = pytest.mark.integration


def test_link_function_to_dynamo(stelvio_env, project_dir):
    def infra():
        table = DynamoTable("orders", fields={"pk": "S"}, partition_key="pk")
        fn = Function("processor", handler="handlers/echo.main", links=[table])
        export_function(fn)
        export_dynamo_table(table)

    outputs = stelvio_env.deploy(infra)

    assert_lambda_function(
        outputs["function_processor_arn"],
        environment={
            "STLV_ORDERS_TABLE_ARN": outputs["dynamotable_orders_arn"],
            "STLV_ORDERS_TABLE_NAME": outputs["dynamotable_orders_name"],
        },
    )
    assert_lambda_role_permissions(
        outputs["function_processor_role_name"],
        expected_actions=[
            "dynamodb:Scan",
            "dynamodb:Query",
            "dynamodb:GetItem",
            "dynamodb:PutItem",
            "dynamodb:UpdateItem",
            "dynamodb:DeleteItem",
        ],
    )


def test_link_function_to_queue(stelvio_env, project_dir):
    def infra():
        queue = Queue("tasks")
        fn = Function("sender", handler="handlers/echo.main", links=[queue])
        export_function(fn)
        export_queue(queue)

    outputs = stelvio_env.deploy(infra)

    assert_lambda_function(
        outputs["function_sender_arn"],
        environment={
            "STLV_TASKS_QUEUE_URL": outputs["queue_tasks_url"],
            "STLV_TASKS_QUEUE_ARN": outputs["queue_tasks_arn"],
            "STLV_TASKS_QUEUE_NAME": outputs["queue_tasks_name"],
        },
    )
    assert_lambda_role_permissions(
        outputs["function_sender_role_name"],
        expected_actions=["sqs:SendMessage", "sqs:GetQueueAttributes"],
    )


def test_link_function_to_topic(stelvio_env, project_dir):
    def infra():
        topic = Topic("notifications")
        fn = Function("notifier", handler="handlers/echo.main", links=[topic])
        export_function(fn)
        export_topic(topic)

    outputs = stelvio_env.deploy(infra)

    assert_lambda_function(
        outputs["function_notifier_arn"],
        environment={
            "STLV_NOTIFICATIONS_TOPIC_ARN": outputs["topic_notifications_arn"],
            "STLV_NOTIFICATIONS_TOPIC_NAME": outputs["topic_notifications_name"],
        },
    )
    assert_lambda_role_permissions(
        outputs["function_notifier_role_name"],
        expected_actions=["sns:Publish"],
    )


def test_link_function_to_bucket(stelvio_env, project_dir):
    def infra():
        bucket = Bucket("files")
        fn = Function("uploader", handler="handlers/echo.main", links=[bucket])
        export_function(fn)
        export_bucket(bucket)

    outputs = stelvio_env.deploy(infra)

    assert_lambda_function(
        outputs["function_uploader_arn"],
        environment={
            "STLV_FILES_BUCKET_ARN": outputs["s3bucket_files_arn"],
            "STLV_FILES_BUCKET_NAME": outputs["s3bucket_files_name"],
        },
    )
    assert_lambda_role_permissions(
        outputs["function_uploader_role_name"],
        expected_actions=[
            "s3:ListBucket",
            "s3:GetObject",
            "s3:PutObject",
            "s3:DeleteObject",
        ],
    )


def test_link_function_to_function(stelvio_env, project_dir):
    def infra():
        target = Function("target", handler="handlers/echo.main")
        caller = Function("caller", handler="handlers/echo.main", links=[target])
        export_function(target)
        export_function(caller)

    outputs = stelvio_env.deploy(infra)

    assert_lambda_function(
        outputs["function_caller_arn"],
        environment={
            "STLV_TARGET_FUNCTION_ARN": outputs["function_target_arn"],
            "STLV_TARGET_FUNCTION_NAME": outputs["function_target_name"],
        },
    )
    assert_lambda_role_permissions(
        outputs["function_caller_role_name"],
        expected_actions=["lambda:InvokeFunction"],
    )


def test_link_function_multiple_links(stelvio_env, project_dir):
    def infra():
        table = DynamoTable("data", fields={"pk": "S"}, partition_key="pk")
        queue = Queue("jobs")
        topic = Topic("alerts")
        fn = Function(
            "worker",
            handler="handlers/echo.main",
            links=[table, queue, topic],
        )
        export_function(fn)
        export_dynamo_table(table)
        export_queue(queue)
        export_topic(topic)

    outputs = stelvio_env.deploy(infra)

    # All linked components should have env vars injected
    assert_lambda_function(
        outputs["function_worker_arn"],
        environment={
            "STLV_DATA_TABLE_ARN": outputs["dynamotable_data_arn"],
            "STLV_DATA_TABLE_NAME": outputs["dynamotable_data_name"],
            "STLV_JOBS_QUEUE_URL": outputs["queue_jobs_url"],
            "STLV_JOBS_QUEUE_ARN": outputs["queue_jobs_arn"],
            "STLV_JOBS_QUEUE_NAME": outputs["queue_jobs_name"],
            "STLV_ALERTS_TOPIC_ARN": outputs["topic_alerts_arn"],
            "STLV_ALERTS_TOPIC_NAME": outputs["topic_alerts_name"],
        },
    )
    # IAM policy should contain actions from all linked components
    assert_lambda_role_permissions(
        outputs["function_worker_role_name"],
        expected_actions=[
            "dynamodb:GetItem",
            "dynamodb:PutItem",
            "sqs:SendMessage",
            "sns:Publish",
        ],
    )
