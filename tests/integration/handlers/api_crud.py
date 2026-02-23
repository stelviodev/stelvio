"""CRUD handler for API Gateway scenario test.

Routes: POST /items, GET /items/{id}, DELETE /items/{id}
Linked to a DynamoDB table via items link.
"""

import json

import boto3
from stlv_resources import Resources


def main(event, context):
    table = boto3.resource("dynamodb").Table(Resources.items.table_name)

    method = event["httpMethod"]
    path_params = event.get("pathParameters") or {}
    item_id = path_params.get("id")

    if method == "POST":
        body = json.loads(event["body"])
        table.put_item(Item=body)
        return {"statusCode": 201, "body": json.dumps(body)}

    if method == "GET" and item_id:
        resp = table.get_item(Key={"pk": item_id})
        item = resp.get("Item")
        if not item:
            return {"statusCode": 404, "body": "not found"}
        return {"statusCode": 200, "body": json.dumps(item)}

    if method == "DELETE" and item_id:
        table.delete_item(Key={"pk": item_id})
        return {"statusCode": 200, "body": "deleted"}

    return {"statusCode": 400, "body": "bad request"}
