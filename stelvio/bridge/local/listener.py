import asyncio
import base64
import datetime
import json
import traceback
import uuid
from dataclasses import asdict

import websockets
from rich.console import Console

from stelvio.bridge.local.dtos import BridgeInvocationResult
from stelvio.bridge.local.handlers import WebsocketHandlers
from stelvio.bridge.remote.infrastructure import discover_or_create_appsync

NOT_A_TEAPOT = 418


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
    ack_data = json.loads(ack)
    if ack_data.get("type") != "connection_ack":
        raise ConnectionError(f"Expected connection_ack, got: {ack_data.get('type')}")

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


async def publish(  # noqa: PLR0913
    result: BridgeInvocationResult,
    ws: websockets.WebSocketClientProtocol,
    api_key: str,
    message: dict,
    app_name: str,
    stage: str,
) -> None:
    """Publish result back to stub lambda."""
    event_data = json.loads(message["event"])
    request_id = event_data["invoke_id"]

    if result.success_result is not None:
        response = {"requestId": request_id, "success": True, "result": result.success_result}
    else:
        response = {
            "requestId": request_id,
            "success": False,
            "error": str(result.error_result),
            "errorType": type(result.error_result).__name__,
            "stackTrace": traceback.format_exception(
                type(result.error_result), result.error_result, result.error_result.__traceback__
            ),
        }
    response_channel = f"/stelvio/{app_name}/{stage}/out"
    await publish_to_channel(ws, response_channel, response, api_key)


def log_invocation(result: BridgeInvocationResult) -> None:
    """Log invocation result."""

    console = Console()

    method = result.request_method
    path = result.request_path
    duration_ms = result.process_time_local
    status_code = result.status_code

    loop_time = asyncio.get_event_loop().time()
    wall_clock = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    timestamp = f"[grey][{wall_clock}: {loop_time:06.0f}][/grey]"

    if result.error_result is not None:
        console.print(
            f"{timestamp} [bold]{method:7s}[/bold] [bold blue]{path:48s}[/bold blue] "
            f"[bold red]ERR[/bold red] {duration_ms:7.2f}ms",
            highlight=False,
        )

        console.print(f"[red]{result.error_result}[/red]")
        tb_lines = traceback.format_exception(
            type(result.error_result), result.error_result, result.error_result.__traceback__
        )
        for line in tb_lines:
            console.print(f"[red]{line.rstrip()}[/red]")

    if result.error_result is None:
        if status_code == NOT_A_TEAPOT:
            status_code = "❌🫖"
        else:
            status_code = str(status_code)
            match status_code[0]:
                case "2":
                    status_color = "green"
                case "4":
                    status_color = "yellow"
                case "5":
                    status_color = "red"
                case _:
                    status_color = "white"
            status_code = f"[bold {status_color}]{status_code}[/bold {status_color}]"
        console.print(
            f"{timestamp} [bold]{method:7s}[/bold] [bold blue]{path:48s}[/bold blue] "
            f"[bold blue]{result.handler_name:48s}[/bold blue]"
            f"{status_code:3s} {duration_ms:7.2f}ms",
            highlight=False,
        )


async def main(region: str, profile: str, app_name: str, env: str) -> None:
    """Main loop."""

    # Discover AppSync API
    config = discover_or_create_appsync(region, profile)

    # Connect
    ws = await connect_to_appsync(asdict(config))

    # Subscribe to request channel
    request_channel = f"/stelvio/{app_name}/{env}/in"
    await subscribe_to_channel(ws, request_channel, config.api_key)

    console = Console()
    console.print("[bold cyan]Stelvio[/bold cyan] local dev server connected to AppSync.")
    console.print("Press Ctrl+C to stop.\n")

    # Handle messages
    async for message in ws:
        data = json.loads(message)

        # Debug: log all message types
        msg_type = data.get("type")

        match msg_type:
            # Keepalive
            case "ka":
                continue
            # Subscribe success/error
            case "subscribe_success" | "subscribe_error":
                continue
            # Publish success/error
            case "publish_success" | "publish_error":
                continue
            # Data message (Lambda invocation)
            case "data":
                for handler in WebsocketHandlers.all():
                    result = await handler.handle_bridge_event(data)
                    if result:
                        await publish(result, ws, config.api_key, data, app_name, env)
                        log_invocation(result)
            case _:
                pass


def run_bridge_server(region: str, profile: str, app_name: str, env: str) -> None:
    """Run the main loop in a blocking manner."""
    asyncio.run(main(region=region, profile=profile, app_name=app_name, env=env))
