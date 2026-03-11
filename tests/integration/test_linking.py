"""Integration tests for the Stelvio link system.

Verifies that linking a Function to other components (DynamoDB, SQS, SNS, S3,
Lambda) correctly injects STELVIO_ environment variables and grants the expected
IAM permissions on real AWS resources.
"""

import pytest

from stelvio.aws.dynamo_db import DynamoTable
from stelvio.aws.function import Function
from stelvio.aws.queue import Queue
from stelvio.aws.s3 import Bucket
from stelvio.aws.topic import Topic

from .assert_helpers import assert_lambda_function, assert_lambda_role_permissions

pytestmark = pytest.mark.integration


def test_link_function_to_dynamo(stelvio_env, project_dir):
    def infra():
        table = DynamoTable("orders", fields={"pk": "S"}, partition_key="pk")
        Function("processor", handler="handlers/echo.main", links=[table])

    outputs = stelvio_env.deploy(infra)

    assert_lambda_function(
        outputs["function_processor_arn"],
        environment={
            "STELVIO_ORDERS_TABLE_ARN": outputs["dynamotable_orders_arn"],
            "STELVIO_ORDERS_TABLE_NAME": outputs["dynamotable_orders_name"],
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
        Function("sender", handler="handlers/echo.main", links=[queue])

    outputs = stelvio_env.deploy(infra)

    assert_lambda_function(
        outputs["function_sender_arn"],
        environment={
            "STELVIO_TASKS_QUEUE_URL": outputs["queue_tasks_url"],
            "STELVIO_TASKS_QUEUE_ARN": outputs["queue_tasks_arn"],
            "STELVIO_TASKS_QUEUE_NAME": outputs["queue_tasks_name"],
        },
    )
    assert_lambda_role_permissions(
        outputs["function_sender_role_name"],
        expected_actions=["sqs:SendMessage", "sqs:GetQueueAttributes"],
    )


def test_link_function_to_topic(stelvio_env, project_dir):
    def infra():
        topic = Topic("notifications")
        Function("notifier", handler="handlers/echo.main", links=[topic])

    outputs = stelvio_env.deploy(infra)

    assert_lambda_function(
        outputs["function_notifier_arn"],
        environment={
            "STELVIO_NOTIFICATIONS_TOPIC_ARN": outputs["topic_notifications_arn"],
            "STELVIO_NOTIFICATIONS_TOPIC_NAME": outputs["topic_notifications_name"],
        },
    )
    assert_lambda_role_permissions(
        outputs["function_notifier_role_name"],
        expected_actions=["sns:Publish"],
    )


def test_link_function_to_bucket(stelvio_env, project_dir):
    def infra():
        bucket = Bucket("files")
        Function("uploader", handler="handlers/echo.main", links=[bucket])

    outputs = stelvio_env.deploy(infra)

    assert_lambda_function(
        outputs["function_uploader_arn"],
        environment={
            "STELVIO_FILES_BUCKET_ARN": outputs["s3bucket_files_arn"],
            "STELVIO_FILES_BUCKET_NAME": outputs["s3bucket_files_name"],
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
        Function("caller", handler="handlers/echo.main", links=[target])

    outputs = stelvio_env.deploy(infra)

    assert_lambda_function(
        outputs["function_caller_arn"],
        environment={
            "STELVIO_TARGET_FUNCTION_ARN": outputs["function_target_arn"],
            "STELVIO_TARGET_FUNCTION_NAME": outputs["function_target_name"],
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
        Function(
            "worker",
            handler="handlers/echo.main",
            links=[table, queue, topic],
        )

    outputs = stelvio_env.deploy(infra)

    # All linked components should have env vars injected
    assert_lambda_function(
        outputs["function_worker_arn"],
        environment={
            "STELVIO_DATA_TABLE_ARN": outputs["dynamotable_data_arn"],
            "STELVIO_DATA_TABLE_NAME": outputs["dynamotable_data_name"],
            "STELVIO_JOBS_QUEUE_URL": outputs["queue_jobs_url"],
            "STELVIO_JOBS_QUEUE_ARN": outputs["queue_jobs_arn"],
            "STELVIO_JOBS_QUEUE_NAME": outputs["queue_jobs_name"],
            "STELVIO_ALERTS_TOPIC_ARN": outputs["topic_alerts_arn"],
            "STELVIO_ALERTS_TOPIC_NAME": outputs["topic_alerts_name"],
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
