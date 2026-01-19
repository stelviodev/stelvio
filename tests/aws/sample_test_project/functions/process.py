"""Sample handler for queue tests."""


def handler(event, context):
    """Process SQS messages."""
    for record in event["Records"]:
        print(f"Processing message: {record['body']}")
    return {"statusCode": 200}
