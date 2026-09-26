def get(event, context):
    user_id = event["pathParameters"]["id"]
    return {
        "statusCode": 200,
        "body": f'{{"id": "{user_id}", "message": "not implemented"}}',
    }
