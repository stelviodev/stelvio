"""$connect handler that always accepts the WebSocket connection."""


def main(event, context):
    return {"statusCode": 200}
