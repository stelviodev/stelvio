import json

import boto3
from stlv_resources import Resources

sqs = boto3.client("sqs")


def handler(event, context):
    order_id = (json.loads(event.get("body") or "{}")).get("order_id", "unknown")
    sqs.send_message(
        QueueUrl=Resources.order_events.queue_url,
        MessageBody=json.dumps({"order_id": order_id}),
    )
    return {"statusCode": 200, "body": '{"status": "complete"}'}
