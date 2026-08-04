"""Post a reply to the WebSocket client via the management API.

Used by ManageConnections / custom-action integration scenarios. Needs
``links=[api]`` so the role can call ``execute-api:ManageConnections``.
"""

import json

import boto3


def main(event, context):
    ctx = event["requestContext"]
    endpoint = f"https://{ctx['domainName']}/{ctx['stage']}"
    client = boto3.client("apigatewaymanagementapi", endpoint_url=endpoint)

    raw_body = event.get("body") or "{}"
    try:
        body = json.loads(raw_body)
    except json.JSONDecodeError:
        body = {"raw": raw_body}

    payload = json.dumps(
        {
            "routeKey": ctx.get("routeKey"),
            "body": body,
        }
    )
    client.post_to_connection(
        ConnectionId=ctx["connectionId"],
        Data=payload.encode("utf-8"),
    )
    return {"statusCode": 200}
