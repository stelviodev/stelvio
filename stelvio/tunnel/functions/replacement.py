import json

import urllib3

# from awslambdaric.lambda_context import LambdaContext
LambdaContext = any  # --- IGNORE ---

CHANNEL_ID = "${channelId}"
ENDPOINT_ID = "${endpointId}"


# Tunnel: Step 1: Deploy a replacement Lambda function to forward requests to
# Stelvio Tunnel Service
def handler(event: dict, context: LambdaContext) -> any:
    incoming_request = event.get("requestContext", {}).get("http", {})

    channel = CHANNEL_ID
    channel = "dev-test"  # --- IGNORE ---
    endpoint = ENDPOINT_ID

    # endpoint_url = f"https://stlv-tunnel.contact-c10.workers.dev/{channel}"
    endpoint_url = f"https://r1g9pcls4l.execute-api.us-east-1.amazonaws.com/v1/tunnel/{channel}/" # TODO: Inject correct URL via env

    http = urllib3.PoolManager()
    response = http.request(
        "POST",
        endpoint_url,
        headers={"User-Agent": "Stelvio-Tunnel-App/1.0"},
        body=json.dumps(
            {
                "method": incoming_request.get("method"),
                "channel": channel,
                "endpoint": endpoint,
                # "payload": {
                    "event": event,
                    "context": {
                        "invoke_id": context.aws_request_id,
                        "client_context": context.client_context,  # TODO: may not be None!
                        "cognito_identity": {
                            "cognito_identity_id": context.identity.cognito_identity_id,
                            "cognito_identity_pool_id": context.identity.cognito_identity_pool_id,
                        },
                        "epoch_deadline_time_in_ms": context._epoch_deadline_time_in_ms,  # noqa: SLF001
                        "invoked_function_arn": context.invoked_function_arn,
                        "tenant_id": context.tenant_id,
                    },
                # }
            }
        ).encode("utf-8"),
    )
    return {
        "statusCode": response.status,
        "body": response.data.decode("utf-8"),
    }
    return json.loads(response.data.decode("utf-8")).get("response", {})
