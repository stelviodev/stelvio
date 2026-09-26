import boto3

dynamodb = boto3.resource("dynamodb")


def get(event, context):
    user_id = event["pathParameters"]["id"]
    # Hard-coded logical name instead of Resources.users.table_name
    table = dynamodb.Table("users")
    result = table.get_item(Key={"id": user_id})
    item = result.get("Item", {})
    return {"statusCode": 200, "body": str(item)}
