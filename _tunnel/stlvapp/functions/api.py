import json
import uuid

import urllib3

def handler(event, context):
    incoming_request = event.get("requestContext", {}).get("http", {})

    # channel = uuid.uuid4()
    channel = "demo"

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
            "queryStringParameters": event.get("queryStringParameters", {}),
        }).encode('utf-8'),
    )

    response_data = json.loads(response.data.decode('utf-8')).get("response", {})
    return response_data

    # return {
    #     "statusCode": response.status,
    #     "body": response.data.decode('utf-8'),
    # }


def handler_real(event, context):
    a = 1
    b = 2
    c = a + b
    return {
        "statusCode": 200,
        "body": json.dumps({
            "message": "Hello from Stelvio API!",
            "data": {
                "a": a,
                "b": b,
                "c": c
            }
        }),
    }