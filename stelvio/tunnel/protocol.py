from typing import Any, Literal, TypedDict


class WebSocketProcessedBody(TypedDict):
    message: str


class WebSocketProcessedPayload(TypedDict):
    statusCode: int
    body: WebSocketProcessedBody


class WebSocketRequestProcessed(TypedDict):
    payload: WebSocketProcessedPayload
    requestId: str
    type: Literal["request-processed"]


class WebSocketContextCognitoIdentity(TypedDict):
    cognito_identity_id: str
    cognito_identity_pool_id: str


class WebSocketPayloadContext(TypedDict):
    invoke_id: str
    client_context: Any | None
    cognito_identity: WebSocketContextCognitoIdentity | None
    epoch_deadline_time_in_ms: int
    invoked_function_arn: str
    tenant_id: str


class WebSocketEventHttp(TypedDict):
    method: str
    path: str


class WebSocketEventRequestContext(TypedDict):
    http: WebSocketEventHttp


class WebSocketEvent(TypedDict):
    headers: dict[str, str]
    body: str
    queryStringParameters: dict[str, str]
    requestContext: WebSocketEventRequestContext


class WebSocketRequestReceivedPayload(TypedDict):
    context: WebSocketPayloadContext
    event: WebSocketEvent


class WebSocketRequestReceived(TypedDict):
    payload: WebSocketRequestReceivedPayload
    requestId: str
    type: Literal["request-received"]


# HTTP request payload structures for tunnel invocations.
class HttpRequestContext(TypedDict):
    invoke_id: str
    client_context: Any | None
    cognito_identity: WebSocketContextCognitoIdentity | None
    epoch_deadline_time_in_ms: int | str
    invoked_function_arn: str
    tenant_id: str


class HttpRequestPOST(TypedDict):
    method: Literal["POST", "GET", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"]
    channel: str
    endpoint: str
    event: WebSocketEvent
    context: HttpRequestContext


# const wrappedMessage = JSON.stringify({
# 	payload: JSON.parse(payload),
# 	requestId: requestId,
# 	type: "request-received"
# });

# response_message = {
#     "payload": payload,
#     "requestId": data.get("requestId"),
#     "type": "request-processed",
# }


# "context": {
#                     "invoke_id": context.aws_request_id,
#                     "client_context": context.client_context,  # TODO: may not be None!
#                     "cognito_identity": {
#                         "cognito_identity_id": context.identity.cognito_identity_id,
#                         "cognito_identity_pool_id": context.identity.cognito_identity_pool_id,
#                     },
#                     "epoch_deadline_time_in_ms": context._epoch_deadline_time_in_ms,
#                     "invoked_function_arn": context.invoked_function_arn,
#                     "tenant_id": context.tenant_id,
#                 },


WebSocketRequestProcessedExample: WebSocketRequestProcessed = {
    "payload": {"statusCode": 200, "body": {"message": "Request processed successfully"}},
    "requestId": "example-request-id",
    "type": "request-processed",
}

WebSocketRequestReceivedExample: WebSocketRequestReceived = {
    "payload": {
        "context": {
            "invoke_id": "example-invoke-id",
            "client_context": None,
            "cognito_identity": {
                "cognito_identity_id": "example-cognito-identity-id",
                "cognito_identity_pool_id": "example-cognito-identity-pool-id",
            },
            "epoch_deadline_time_in_ms": 1700000000000,
            "invoked_function_arn": "arn:aws:lambda:region:account-id:function:function-name",
            "tenant_id": "example-tenant-id",
        },
        "event": {
            "headers": {"Content-Type": "application/json"},
            "body": '{"key": "value"}',
            "queryStringParameters": {"param1": "value1"},
            "requestContext": {"http": {"method": "POST", "path": "/example/path"}},
        },
    },
    "requestId": "example-request-id",
    "type": "request-received",
}


HttpPostRequestExample: HttpRequestPOST = {
    "method": "GET",
    "channel": "channel-id",
    "endpoint": "endpoint-id",
    "event": {
        "headers": {"Content-Type": "application/json"},
        "body": '{"key": "value"}',
        "queryStringParameters": {"param1": "value1"},
        "requestContext": {"http": {"method": "POST", "path": "/example/path"}},
    },
    "context": {
        "invoke_id": "context.aws_request_id",
        "client_context": "context.client_context",
        "cognito_identity": {
            "cognito_identity_id": "context.identity.cognito_identity_id",
            "cognito_identity_pool_id": "context.identity.cognito_identity_pool_id",
        },
        "epoch_deadline_time_in_ms": "context._epoch_deadline_time_in_ms",
        "invoked_function_arn": "context.invoked_function_arn",
        "tenant_id": "context.tenant_id",
    },
}
