

import json


def handler(event, _context):
    channel_id = event["pathParameters"]["channel_id"]
    return {
        "statusCode": 200,
        "body": json.dumps(
            {
                "channel_id": channel_id
            }
        ),
    }