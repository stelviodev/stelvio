import json
import os


def main(event, context):
    return {
        "statusCode": 200,
        "body": json.dumps(
            {
                "marker": os.environ.get("DEV_TEST_MARKER", "no-marker"),
                "path": event.get("rawPath") or event.get("path"),
            }
        ),
    }
