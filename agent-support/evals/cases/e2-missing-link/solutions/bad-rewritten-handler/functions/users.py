import boto3
import os

dynamodb = boto3.resource("dynamodb")


def get(event, context):
    user_id = event["pathParameters"]["id"]
    # Rewritten to hard-code env lookup instead of Resources
    table = dynamodb.Table(os.environ["STLV_USERS_TABLE_NAME"])
    result = table.get_item(Key={"id": user_id})
    return {"statusCode": 200, "body": str(result.get("Item", {}))}
