"""Checkpoint a non-idempotent counter, suspend, and return the published revision."""

import os

import boto3
from aws_durable_execution_sdk_python import durable_execution, durable_step
from aws_durable_execution_sdk_python.config import Duration
from stlv_resources import Resources


@durable_step
def record(step_context, key):
    boto3.resource("dynamodb").Table(Resources.results.table_name).update_item(
        Key={"pk": key},
        UpdateExpression="ADD runs :one",
        ExpressionAttributeValues={":one": 1},
    )
    return os.environ["REVISION"]


@durable_execution
def main(event, context):
    revision = context.step(record(event["key"]), name="record")
    context.wait(Duration.from_seconds(event["wait"]), name="pause")
    return {"revision": revision, "current_revision": os.environ["REVISION"]}
