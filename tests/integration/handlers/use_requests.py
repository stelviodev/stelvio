"""Handler that imports requests from a layer to verify packaging works."""

import requests


def main(event, context):
    return {"statusCode": 200, "body": f"requests {requests.__version__}"}
