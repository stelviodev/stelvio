import asyncio
import base64
import datetime
import json
import traceback
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict

import websockets
from rich.console import Console
from rich.markup import escape

from stelvio import signals
from stelvio._signals import _current, _diagnostic, _record_failure
from stelvio.bridge._chunking import (
    ChunkBuffer,
    channel_segment,
    cleanup_stale_buffers,
    is_chunked_message,
    reassemble_chunk,
    split_message,
)
from stelvio.bridge.local.dtos import BridgeInvocationResult
from stelvio.bridge.local.handlers import WebsocketHandlers
from stelvio.bridge.remote.infrastructure import discover_or_create_appsync

NOT_A_TEAPOT = 418

# Chunk buffers for reassembling chunked requests from Lambda stub
_request_chunk_buffers: dict[str, ChunkBuffer] = {}


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

    try:
        await ws.send(json.dumps({"type": "connection_init"}))
        await _wait_for_connection_ack(ws)
    except BaseException as error:
        _record_failure(error)
        await _stop_bridge(ws, error)
        raise

    return ws


async def _wait_for_connection_ack(ws: websockets.WebSocketClientProtocol) -> None:
    async with asyncio.timeout(10):
        while True:
            ack_data = json.loads(await ws.recv())
            if ack_data.get("type") == "ka":
                continue
            if ack_data.get("type") != "connection_ack":
                raise ConnectionError(f"Expected connection_ack, got: {ack_data.get('type')}")
            return


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
    async with asyncio.timeout(10):
        while True:
            ack = json.loads(await ws.recv())
            if ack.get("type") == "ka":
                continue
            if ack.get("type") in ("subscribe_success", "subscribe_error"):
                if ack.get("id") not in (None, "request-sub"):
                    continue
                if ack.get("type") == "subscribe_success" and ack.get("id") == "request-sub":
                    return
            raise ConnectionError(f"AppSync subscription to {channel!r} failed: {ack}")


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
    response_channel = f"/stelvio/{channel_segment(app_name)}/{channel_segment(stage)}/out"

    # Split response into chunks if needed
    chunks = split_message(response, request_id)
    for chunk in chunks:
        await publish_to_channel(ws, response_channel, chunk, api_key)


def log_invocation(result: BridgeInvocationResult) -> None:
    """Log invocation result."""

    console = Console()

    method = result.request_method
    path = result.request_path
    duration_ms = result.process_time_local
    status_code = result.status_code

    loop_time = asyncio.get_running_loop().time()
    wall_clock = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    timestamp = f"[grey][{wall_clock}: {loop_time:06.0f}][/grey]"

    if result.error_result is not None:
        console.print(
            f"{timestamp} [bold]{method:7s}[/bold] [bold blue]{path:48s}[/bold blue] "
            f"[bold red]ERR[/bold red] {duration_ms:7.2f}ms",
            highlight=False,
        )

        console.print(f"[red]{escape(str(result.error_result))}[/red]")
        tb_lines = traceback.format_exception(
            type(result.error_result), result.error_result, result.error_result.__traceback__
        )
        for line in tb_lines:
            console.print(f"[red]{escape(line.rstrip())}[/red]")

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


async def _handle_data_message(
    data: dict, ws: object, api_key: str, app_name: str, env: str
) -> None:
    event_data = json.loads(data["event"])

    if is_chunked_message(event_data):
        complete_msg, is_complete = reassemble_chunk(event_data, _request_chunk_buffers)
        if not is_complete:
            return
        data = {"type": "data", "event": json.dumps(complete_msg)}

    for handler in WebsocketHandlers.all():
        result = await handler.handle_bridge_event(data)
        if result:
            await publish(result, ws, api_key, data, app_name, env)
            log_invocation(result)


async def main(region: str, profile: str, app_name: str, env: str) -> None:
    """Main loop."""

    session = _current.get()
    if session is not None:
        await session.emit_async(signals.before_dev_bridge_start)
    ws = None
    primary = None
    try:
        # Discover AppSync API
        config = discover_or_create_appsync(region, profile)

        # Connect
        ws = await connect_to_appsync(asdict(config))

        # Subscribe to request channel
        request_channel = f"/stelvio/{channel_segment(app_name)}/{channel_segment(env)}/in"
        await subscribe_to_channel(ws, request_channel, config.api_key)

        if session is not None:
            session.bridge_ready = True
            await session.emit_async(signals.after_dev_bridge_start)
            session.phase = "dev_bridge"

        console = Console()
        console.print("[bold cyan]Stelvio[/bold cyan] local dev server connected to AppSync.")
        console.print("Press Ctrl+C to stop.\n")

        await _listen(ws, config.api_key, app_name, env, console)
    except BaseException as error:
        primary = error
        _record_failure(error)
        raise
    finally:
        if ws is not None:
            await _stop_bridge(ws, primary)


async def _listen(
    ws: websockets.WebSocketClientProtocol,
    api_key: str,
    app_name: str,
    env: str,
    console: Console,
) -> None:
    # Handle messages
    async for message in ws:
        data = json.loads(message)

        # Debug: log all message types
        msg_type = data.get("type")

        match msg_type:
            # Keepalive - also clean up stale buffers
            case "ka":
                cleanup_stale_buffers(_request_chunk_buffers)
                continue
            # Subscribe success/error
            case "subscribe_success":
                continue
            case "subscribe_error":
                errors = data.get("errors", [])
                console.print(
                    f"[bold red]AppSync subscribe_error:[/bold red] {escape(str(errors))}"
                )
                continue
            # Publish success
            case "publish_success":
                continue
            # Publish error - surface so silent failures don't masquerade as timeouts
            case "publish_error":
                errors = data.get("errors", [])
                console.print(f"[bold red]AppSync publish_error:[/bold red] {escape(str(errors))}")
                continue
            # Data message (Lambda invocation)
            case "data":
                await _handle_data_message(data, ws, api_key, app_name, env)
            case _:
                pass


async def _stop_bridge(
    ws: websockets.WebSocketClientProtocol, primary: BaseException | None
) -> None:
    session = _current.get()
    failure = primary

    async def attempt(action: Callable[[], Awaitable[object]]) -> bool:
        nonlocal failure
        try:
            await action()
        except BaseException as error:
            if failure is None:
                failure = error
                _record_failure(error)
            else:
                _diagnostic(error)
            return False
        return True

    if session is not None:
        await attempt(lambda: session.emit_async(signals.before_dev_bridge_stop))
    closed = await attempt(ws.close)
    if session is not None and closed:
        await attempt(lambda: session.emit_async(signals.after_dev_bridge_stop))
    if primary is None and failure is not None:
        raise failure


def run_bridge_server(region: str, profile: str, app_name: str, env: str) -> None:
    """Run the main loop in a blocking manner."""
    asyncio.run(main(region=region, profile=profile, app_name=app_name, env=env))
