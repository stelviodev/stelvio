"""Sends API request body to a linked SQS queue.

Used by the API->Queue->Worker composite scenario test.
The queue is accessed via the 'jobs' link.
"""

import json

import boto3
from stelvio_resources import Resources


def main(event, context):
    client = boto3.client("sqs")

    body = event.get("body", "{}")
    resp = client.send_message(QueueUrl=Resources.jobs.queue_url, MessageBody=body)

    return {
        "statusCode": 200,
        "body": json.dumps({"messageId": resp["MessageId"]}),
    }
