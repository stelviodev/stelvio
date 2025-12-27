# noqa: INP001

"""
Stub Lambda handler - forwards invocations to local dev server via AppSync.
Uses connection reuse for better performance.
"""

import asyncio
import base64
import json
import os
import time
import uuid

import websockets

# Environment variables (set by deployment)
APPSYNC_REALTIME = os.environ["STLV_APPSYNC_REALTIME"]
APPSYNC_HTTP = os.environ["STLV_APPSYNC_HTTP"]
API_KEY = os.environ["STLV_APPSYNC_API_KEY"]
APP_NAME = os.environ.get("STLV_APP_NAME", "stlv")
STAGE = os.environ.get("STLV_STAGE", "dev")
FUNCTION_NAME = os.environ.get("STLV_FUNCTION_NAME", "unknown")
ENDPOINT_ID = os.environ.get("STLV_DEV_ENDPOINT_ID", "endpoint_id")

WEBSOCKET_STALE_TIMEOUT = 240  # 4 minutes

# Global state for connection reuse (survives across warm container invocations)
_event_loop = None
_ws_connection = None
_last_connected = None
_subscribed = False


def get_or_create_loop() -> asyncio.AbstractEventLoop:
    """Get existing event loop or create new one."""
    global _event_loop  # noqa: PLW0603
    if _event_loop is None or _event_loop.is_closed():
        _event_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_event_loop)
    return _event_loop


async def connect_to_appsync() -> websockets.WebSocketClientProtocol:
    """Connect to AppSync Events WebSocket."""
    # Create auth header
    auth_header = {"host": APPSYNC_HTTP, "x-api-key": API_KEY}

    # Encode as base64 subprotocol
    auth_b64 = base64.b64encode(json.dumps(auth_header).encode()).decode()
    auth_b64 = auth_b64.replace("+", "-").replace("/", "_").replace("=", "")

    # Connect
    uri = f"wss://{APPSYNC_REALTIME}/event/realtime"
    ws = await websockets.connect(uri, subprotocols=["aws-appsync-event-ws", f"header-{auth_b64}"])

    # Send connection_init
    await ws.send(json.dumps({"type": "connection_init"}))

    # Wait for connection_ack
    await ws.recv()

    return ws


async def subscribe_to_channel(ws: websockets.WebSocketClientProtocol) -> None:
    """Subscribe to response channel."""
    response_channel = f"/stelvio/{APP_NAME}/{STAGE}/out"
    await ws.send(
        json.dumps(
            {
                "type": "subscribe",
                "id": "response-sub",
                "channel": response_channel,
                "authorization": {"x-api-key": API_KEY},
            }
        )
    )

    # Wait for subscribe_success
    await ws.recv()


async def get_or_create_connection() -> tuple[websockets.WebSocketClientProtocol, bool]:
    """Get existing connection or create new one."""
    global _ws_connection, _last_connected, _subscribed  # noqa: PLW0603

    # Check if we have a valid connection
    if _ws_connection is not None:
        try:
            # Check if connection is still open (websockets uses close_code)
            # If close_code is None, connection is still open
            if _ws_connection.close_code is None:
                # Check if it's not stale (AppSync timeout is 5 min)
                age = time.time() - _last_connected
                if age < WEBSOCKET_STALE_TIMEOUT:  # 4 min safety margin
                    return _ws_connection, True  # Reused!
        except Exception:  # noqa: S110
            pass  # Connection is bad, create new one

    # Need fresh connection
    try:
        _ws_connection = await connect_to_appsync()
        _last_connected = time.time()
        _subscribed = False  # Will need to subscribe
    except Exception:
        # Reset state on error
        _ws_connection = None
        _last_connected = None
        _subscribed = False
        raise
    else:
        return _ws_connection, False  # New connection


async def ensure_subscribed(ws: websockets.WebSocketClientProtocol) -> None:
    """Ensure we're subscribed to response channel."""
    global _subscribed  # noqa: PLW0603
    if not _subscribed:
        await subscribe_to_channel(ws)
        _subscribed = True


async def publish_to_appsync(
    ws: websockets.WebSocketClientProtocol, channel: str, data: dict
) -> None:
    """Publish message to AppSync channel."""

    await ws.send(
        json.dumps(
            {
                "id": str(uuid.uuid4()),  # Required by AppSync Events!
                "type": "publish",
                "channel": channel,
                "events": [json.dumps(data)],
                "authorization": {"x-api-key": API_KEY},
            }
        )
    )


async def wait_for_response(
    ws: websockets.WebSocketClientProtocol, request_id: str, timeout: int = 16
) -> dict | None:
    """Wait for response from local dev server."""
    start = time.time()

    while time.time() - start < timeout:
        try:
            message = await asyncio.wait_for(ws.recv(), timeout=timeout - (time.time() - start))

            data = json.loads(message)

            # Check if this is a data message
            if data.get("type") == "data":
                event_data = json.loads(data["event"])

                # Check if it matches our request ID
                if event_data.get("requestId") == request_id:
                    return event_data

            # Check for keepalive
            if data.get("type") == "ka":
                continue

        except TimeoutError:
            break

    return None


def handler(event: dict, context: object) -> dict:
    """Lambda handler - manages event loop manually for connection reuse."""
    loop = get_or_create_loop()
    return loop.run_until_complete(async_handler(event, context))


async def async_handler(event: dict, context: object) -> dict:  # noqa: PLR0911
    """Async handler implementation."""

    # Track timing
    t_start = time.time()
    timings = {}

    # Get or create connection
    t_connect_start = time.time()
    try:
        ws, _reused = await get_or_create_connection()
        timings["connect"] = int((time.time() - t_connect_start) * 1000)
    except Exception as e:
        return {
            "statusCode": 500,
            "body": json.dumps({"error": f"Failed to connect to AppSync: {e!s}"}),
        }

    # Ensure subscribed
    t_subscribe_start = time.time()
    try:
        await ensure_subscribed(ws)
        _subscribe_time = int((time.time() - t_subscribe_start) * 1000)
    except Exception as e:
        # Reset connection state on subscription failure
        global _ws_connection, _subscribed  # noqa: PLW0603
        _ws_connection = None
        _subscribed = False
        return {"statusCode": 500, "body": json.dumps({"error": f"Failed to subscribe: {e!s}"})}

    # Publish invocation to local dev server
    request_channel = f"/stelvio/{APP_NAME}/{STAGE}/in"
    cognito_identity = None
    if getattr(context, "identity", None) is not None:
        cognito_identity = {
            "cognito_identity_id": getattr(context.identity, "cognito_identity_id", None),
            "cognito_identity_pool_id": getattr(
                context.identity, "cognito_identity_pool_id", None
            ),
        }
    request_message = {
        "requestId": getattr(context, "aws_request_id", None),
        "invoke_id": getattr(context, "aws_request_id", None),
        "endpointId": ENDPOINT_ID,
        "functionName": FUNCTION_NAME,
        "event": event,
        "context": {
            "invoke_id": getattr(context, "aws_request_id", None),
            "client_context": getattr(context, "client_context", None),
            "cognito_identity": cognito_identity,
            "epoch_deadline_time_in_ms": getattr(context, "_epoch_deadline_time_in_ms", None),
            "invoked_function_arn": getattr(context, "invoked_function_arn", None),
            "tenant_id": getattr(context, "tenant_id", None),
        },
    }

    t_publish_start = time.time()
    try:
        await publish_to_appsync(ws, request_channel, request_message)
        timings["publish"] = int((time.time() - t_publish_start) * 1000)
    except Exception as e:
        return {
            "statusCode": 500,
            "body": json.dumps(
                {
                    "error": f"Failed to publish to AppSync: {e!s}",
                    "request_channel": request_channel,
                    "request_message": request_message,
                }
            ),
        }

    # Wait for response
    t_wait_start = time.time()
    try:
        response = await wait_for_response(ws, context.aws_request_id, timeout=16)
        timings["wait"] = int((time.time() - t_wait_start) * 1000)
    except Exception as e:
        return {
            "statusCode": 500,
            "body": json.dumps({"error": f"Error waiting for response: {e!s}"}),
        }

    if response is None:
        timings["total"] = int((time.time() - t_start) * 1000)
        return {
            "statusCode": 500,
            "body": json.dumps(
                {
                    "error": "Local dev server not responding",
                    "hint": "Is 'stlv dev' running?",
                    "timings": timings,
                    "env": {
                        "APP_NAME": APP_NAME,
                        "STAGE": STAGE,
                        "FUNCTION_NAME": FUNCTION_NAME,
                        "ENDPOINT_ID": ENDPOINT_ID,
                    },
                }
            ),
        }

    # Calculate total time
    timings["total"] = int((time.time() - t_start) * 1000)

    if response.get("success"):
        return response["result"]
    # Error from local dev
    return {
        "statusCode": 500,
        "body": json.dumps(
            {
                "error": response.get("error"),
                "errorType": response.get("errorType"),
                "stackTrace": response.get("stackTrace"),
            }
        ),
    }
