import json


def process_order(order: dict) -> None:
    # Simulated long-running work
    _ = order


def accept(event, context):
    order = json.loads(event.get("body") or "{}")
    process_order(order)
    return {"statusCode": 200, "body": '{"status": "processed"}'}
