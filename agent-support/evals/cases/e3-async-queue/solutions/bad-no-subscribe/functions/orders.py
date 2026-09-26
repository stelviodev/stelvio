import json

import boto3
from stlv_resources import Resources

sqs = boto3.client("sqs")


def process_order(order: dict) -> None:
    _ = order


def accept(event, context):
    order = json.loads(event.get("body") or "{}")
    sqs.send_message(QueueUrl=Resources.orders.queue_url, MessageBody=json.dumps(order))
    return {"statusCode": 202, "body": '{"status": "accepted"}'}
