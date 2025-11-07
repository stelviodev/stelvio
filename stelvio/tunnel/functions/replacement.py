import json

import urllib3

CHANNEL_ID = "${channelId}"
ENDPOINT_ID = "${endpointId}"


def handler(event: any, context: any) -> any:
    del context  # Unused

    incoming_request = event.get("requestContext", {}).get("http", {})

    channel = CHANNEL_ID
    endpoint = ENDPOINT_ID

    endpoint_url = f"https://stlv-tunnel.contact-c10.workers.dev/{channel}"

    http = urllib3.PoolManager()
    response = http.request(
        "POST",
        endpoint_url,
        headers={"User-Agent": "Stelvio-Tunnel-App/1.0"},
        body=json.dumps(
            {
                "method": incoming_request.get("method"),
                "path": incoming_request.get("path"),
                "headers": event.get("headers", {}),
                "body": event.get("body", {}),
                "queryStringParameters": event.get("queryStringParameters", {}),
                "channel": channel,
                "endpoint": endpoint,
            }
        ).encode("utf-8"),
    )

    return json.loads(response.data.decode("utf-8")).get("response", {})
