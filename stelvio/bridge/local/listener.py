"""
Local dev server - receives Lambda invocations and executes handlers locally.
"""

import asyncio
import base64
import contextlib
import json
import runpy
import sys
from dataclasses import asdict
from pathlib import Path

import websockets

from stelvio.bridge.local.handlers import WebsocketHandlers
from stelvio.bridge.remote.infrastructure import discover_or_create_appsync



# class MockContext:
#     """Mock Lambda context for local execution."""

#     def __init__(self, context_data: dict) -> None:
#         self.request_id = context_data["requestId"]
#         self.function_name = context_data["functionName"]
#         self.memory_limit_in_mb = context_data["memoryLimitInMB"]
#         self._remaining_time = context_data["remainingTimeInMillis"]

#     def get_remaining_time_in_millis(self) -> int:
#         return self._remaining_time


# def load_handler() -> callable:
#     """Load handler using runpy - fresh reload each invocation."""
#     # Navigate to project root (3 parents back from dev_server.py)
#     project_root = Path(__file__).parent.parent.parent.parent
#     handler_file = project_root / HANDLER_PATH

#     # Load the module in a fresh namespace
#     module = runpy.run_path(str(handler_file))

#     # Return the handler function
#     return module["handler"]


async def connect_to_appsync(config: dict) -> websockets.WebSocketClientProtocol:
    """Connect to AppSync Events WebSocket."""
    # Create auth header
    auth_header = {"host": config["http_endpoint"], "x-api-key": config["api_key"]}

    # Encode as base64 subprotocol
    auth_b64 = base64.b64encode(json.dumps(auth_header).encode()).decode()
    auth_b64 = auth_b64.replace("+", "-").replace("/", "_").replace("=", "")

    # Connect
    uri = f"wss://{config['realtime_endpoint']}/event/realtime"

    ws = await websockets.connect(uri, subprotocols=["aws-appsync-event-ws", f"header-{auth_b64}"])

    # Send connection_init (optional but recommended)
    init_message = {"type": "connection_init"}
    await ws.send(json.dumps(init_message))

    # Wait for connection_ack
    ack = await asyncio.wait_for(ws.recv(), timeout=10)
    json.loads(ack)

    return ws


async def subscribe_to_channel(
    ws: websockets.WebSocketClientProtocol, channel: str, api_key: str
) -> None:
    """Subscribe to AppSync channel."""
    await ws.send(
        json.dumps(
            {
                "type": "subscribe",
                "id": "request-sub",
                "channel": channel,
                "authorization": {"x-api-key": api_key},
            }
        )
    )

    # Wait for subscribe_success
    await ws.recv()


async def publish_to_channel(
    ws: websockets.WebSocketClientProtocol, channel: str, data: dict, api_key: str
) -> None:
    """Publish message to AppSync channel."""
    import uuid

    await ws.send(
        json.dumps(
            {
                "id": str(uuid.uuid4()),  # Required by AppSync Events!
                "type": "publish",
                "channel": channel,
                "events": [json.dumps(data)],
                "authorization": {"x-api-key": api_key},
            }
        )
    )


# async def handle_invocation(
#     ws: websockets.WebSocketClientProtocol, message: dict, api_key: str
# ) -> None:
#     """Handle a Lambda invocation."""
#     import time

#     # Parse the invocation
#     event_data = json.loads(message["event"])

#     request_id = event_data["requestId"]
#     event_data["functionName"]
#     event = event_data["event"]
#     context_data = event_data["context"]

#     # Track timing
#     t_start = time.time()

#     # Create mock context
#     context = MockContext(context_data)

#     # Execute user's handler
#     try:
#         t_handler_start = time.time()

#         # Load handler fresh each invocation for hot reload
#         handler_fn = load_handler()
#         result = handler_fn(event, context)

#         int((time.time() - t_handler_start) * 1000)

#         # Publish success response
#         response = {"requestId": request_id, "success": True, "result": result}
#     except Exception as e:
#         import traceback

#         int((time.time() - t_handler_start) * 1000)

#         # Publish error response
#         response = {
#             "requestId": request_id,
#             "success": False,
#             "error": str(e),
#             "errorType": type(e).__name__,
#             "stackTrace": traceback.format_exc().split("\n"),
#         }

#     # Send response back
#     t_publish_start = time.time()
#     response_channel = f"/stelvio/{APP_NAME}/{STAGE}/out"
#     await publish_to_channel(ws, response_channel, response, api_key)
#     int((time.time() - t_publish_start) * 1000)

#     int((time.time() - t_start) * 1000)


async def publish(result, ws, api_key, message, app_name, stage):
    """Publish result (placeholder)."""
    event_data = json.loads(message["event"])
    # request_id = event_data["requestId"]
    request_id = event_data["invoke_id"]
    
    response = {"requestId": request_id, "success": True, "result": result}
    response_channel = f"/stelvio/{app_name}/{stage}/out"
    await publish_to_channel(ws, response_channel, response, api_key)


async def main(region, profile, app_name, stage) -> None:
    """Main loop."""

    # Discover AppSync API
    config = discover_or_create_appsync(region, profile)

    # Connect
    ws = await connect_to_appsync(asdict(config))

    # Subscribe to request channel
    request_channel = f"/stelvio/{app_name}/{stage}/in"
    await subscribe_to_channel(ws, request_channel, config.api_key)

    # Handle messages
    async for message in ws:
        data = json.loads(message)

        # print(f"{data['event']=}")

        # Debug: log all message types
        msg_type = data.get("type")

        # Keepalive
        if msg_type == "ka":
            continue

        # Subscribe success/error
        if msg_type in ("subscribe_success", "subscribe_error"):
            continue

        # Publish success/error
        if msg_type in ("publish_success", "publish_error"):
            continue

        # Data message (Lambda invocation)
        if msg_type == "data":
            # print("Received invocation")
            # await handle_invocation(ws, data, config.api_key)
            for handler in WebsocketHandlers.all():
                result = await handler.handle_bridge_event(data, None, None)
                if result:
                    print("Publishing")
                    await publish(result, ws, config.api_key, data, app_name, stage)
        else:
            pass


def blocking_run(region: str, profile: str, app_name: str, stage: str) -> None:
    """Run the main loop in a blocking manner."""
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main(region=region, profile=profile, app_name=app_name, stage=stage))

# if __name__ == "__main__":
#     with contextlib.suppress(KeyboardInterrupt):
#         asyncio.run(main(region="us-east-1", profile="default", app_name="tunnel", stage="dev"))
