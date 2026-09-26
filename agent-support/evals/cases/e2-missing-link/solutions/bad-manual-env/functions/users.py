import boto3

from stlv_resources import Resources

dynamodb = boto3.resource("dynamodb")


def get(event, context):
    user_id = event["pathParameters"]["id"]
    table = dynamodb.Table(Resources.users.table_name)
    result = table.get_item(Key={"id": user_id})
    item = result.get("Item", {})
    return {"statusCode": 200, "body": str(item)}
