import json

import boto3
from stlv_resources import Resources

dynamodb = boto3.resource("dynamodb")
sns = boto3.client("sns")


def create(event, context):
    body = json.loads(event.get("body") or "{}")
    user_id = body.get("id", "unknown")
    table = dynamodb.Table(Resources.users.table_name)
    table.put_item(Item={"id": user_id, **body})
    sns.publish(
        TopicArn=Resources.user_created.topic_arn,
        Message=json.dumps({"id": user_id, "event": "user_created"}),
    )
    return {"statusCode": 201, "body": json.dumps({"id": user_id})}
