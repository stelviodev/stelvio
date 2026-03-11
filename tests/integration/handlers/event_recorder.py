"""Writes received event to a linked DynamoDB results table.

Used by all async scenario tests (queue, topic, s3, stream, cron triggers).
The test deploys this handler with links=[results_table], then polls the
results table to verify the event was processed.
"""

import json
import time

import boto3
from stelvio_resources import Resources


def main(event, context):
    table = boto3.resource("dynamodb").Table(Resources.results.table_name)

    table.put_item(
        Item={
            "pk": context.aws_request_id,
            "event": json.dumps(event, default=str),
            "source": context.function_name,
            "timestamp": str(time.time()),
        }
    )

    return {"statusCode": 200, "body": "recorded"}
