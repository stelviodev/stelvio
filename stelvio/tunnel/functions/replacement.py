import json
import random

import urllib3


CHANNEL_ID = "${channelId}"
ENDPOINT_ID = "${endpointId}"

def handler(event, context):
    # return {
    #     "statusCode": 200,
    #     "headers": {
    #         "Content-Type": "application/json"
    #     },
    #     "body": json.dumps({"message": "Hello from Stelvio Tunnel Lambda!"})
    # }
    incoming_request = event.get("requestContext", {}).get("http", {})

    channel = CHANNEL_ID
    endpoint = ENDPOINT_ID

    ENDPOINT = f"https://stlv-tunnel.contact-c10.workers.dev/{channel}"

    http = urllib3.PoolManager()
    response = http.request(
        "POST",
        ENDPOINT,
        headers={"User-Agent": "Stelvio-Tunnel-App/1.0"},
        body=json.dumps({
            "method": incoming_request.get("method"),
            "path": incoming_request.get("path"),
            "headers": event.get("headers", {}),
            "body": event.get("body", {}),
            "queryStringParameters": event.get("queryStringParameters", {}),
            "path": incoming_request.get("path"),
            "channel": channel,
            "endpoint": endpoint,
        }).encode('utf-8'),
    )

    response_data = json.loads(response.data.decode('utf-8')).get("response", {})
    return response_data