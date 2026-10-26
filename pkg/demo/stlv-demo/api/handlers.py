import datetime
import json
import uuid
from typing import Any

import boto3
from stlv_resources import Resources


def hello_world(event: dict[str, Any], context: object) -> dict[str, str | int]:
    del event, context
    return {"statusCode": 200, "body": "Hello World!"}


def post_todo(event: dict[str, Any], context: object) -> dict[str, str | int]:
    del context

    body = json.loads(event.get("body", "{}"))

    todo = {
        "user": body.get("name", "Stranger"),
        "text": body.get("text", ""),
        "finished": body.get("finished", False),
        "date": datetime.datetime.now().strftime("%Y-%m-%d"),
        "id": uuid.uuid4().hex,
    }

    dynamodb = boto3.resource("dynamodb")
    table = dynamodb.Table(Resources.todos_table.table_name)

    table.put_item(Item=todo)

    return {"statusCode": 200, "body": json.dumps(todo)}


def list_todos(event: dict[str, Any], context: object) -> dict[str, str | int]:
    del context

    user = event["pathParameters"]["user"]

    dynamodb = boto3.resource("dynamodb")
    table = dynamodb.Table(Resources.todos_table.table_name)

    response = table.query(KeyConditionExpression=boto3.dynamodb.conditions.Key("user").eq(user))
    items = response.get("Items", [])

    if not items:
        items = {"message": "No Items"}

    return {"statusCode": 200, "body": json.dumps(items)}


def cleanup(event: dict[str, Any], context: object) -> None:
    del event, context

    dynamodb = boto3.resource("dynamodb")
    table = dynamodb.Table(Resources.todos_table.table_name)

    scan = table.scan(FilterExpression=boto3.dynamodb.conditions.Attr("finished").eq(True))
    for item in scan["Items"]:
        table.delete_item(Key={"id": item["id"]})
