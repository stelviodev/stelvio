import json

import boto3
from stlv_resources import Resources

sns = boto3.client("sns")


def handler(event, context):
    order_id = (json.loads(event.get("body") or "{}")).get("order_id", "unknown")
    sns.publish(
        TopicArn=Resources.order_completed.topic_arn,
        Message=json.dumps({"order_id": order_id}),
    )
    return {"statusCode": 200, "body": '{"status": "complete"}'}
