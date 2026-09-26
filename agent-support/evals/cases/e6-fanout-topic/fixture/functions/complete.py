def handler(event, context):
    # Marks the order complete but does not yet notify dependents.
    return {"statusCode": 200, "body": '{"status": "complete"}'}
