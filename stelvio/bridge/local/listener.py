import asyncio
import base64
import contextlib
import datetime
import json
import signal
import socket
import threading
import time
import traceback
import uuid
from collections.abc import Callable, Iterator
from dataclasses import asdict

import websockets
from rich.console import Console
from rich.markup import escape

from stelvio.bridge._chunking import (
    ChunkBuffer,
    channel_segment,
    cleanup_stale_buffers,
    is_chunked_message,
    reassemble_chunk,
    split_message,
    wait_for_appsync_ack,
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

    # Send connection_init (optional but recommended)
    init_message = {"type": "connection_init"}
    await ws.send(json.dumps(init_message))
    await wait_for_appsync_ack(ws, "connection_ack")
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
    await wait_for_appsync_ack(
        ws, "subscribe_success", operation_id="request-sub", channel=channel
    )


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


async def _handle_data_message(  # noqa: PLR0913 - bridge dispatch context plus admission boundary
    data: dict,
    ws: object,
    api_key: str,
    app_name: str,
    env: str,
    admission: Callable[[str], BaseException | None] | None = None,
) -> None:
    event_data = json.loads(data["event"])

    if is_chunked_message(event_data):
        complete_msg, is_complete = reassemble_chunk(event_data, _request_chunk_buffers)
        if not is_complete:
            return
        data = {"type": "data", "event": json.dumps(complete_msg)}
        event_data = complete_msg

    if admission is not None:
        context_data = event_data.get("context") or {}
        deadline = context_data.get("epoch_deadline_time_in_ms")
        if type(deadline) is int and deadline > 0 and time.time() * 1000 >= deadline:
            error: BaseException | None = TimeoutError(
                "Invocation deadline expired before local dispatch"
            )
        else:
            endpoint_id = event_data.get("endpointId")
            try:
                error = admission(endpoint_id if isinstance(endpoint_id, str) else "")
            except Exception as admission_error:
                # Fail closed if the status boundary itself cannot produce a decision.
                error = admission_error
        if error is not None:
            request = event_data.get("event")
            request = request if isinstance(request, dict) else {}
            result = BridgeInvocationResult(
                success_result=None,
                error_result=error,
                request_path=request.get("rawPath") or request.get("path") or "N/A",
                request_method=request.get("httpMethod") or "N/A",
                process_time_local=0.0,
                status_code=-1,
            )
            await publish(result, ws, api_key, data, app_name, env)
            log_invocation(result)
            return

    for handler in WebsocketHandlers.all():
        result = await handler.handle_bridge_event(data)
        if result:
            await publish(result, ws, api_key, data, app_name, env)
            log_invocation(result)


async def main(
    region: str,
    profile: str,
    app_name: str,
    env: str,
    admission: Callable[[str], BaseException | None] | None = None,
) -> None:
    """Main loop."""

    # Discover AppSync API
    config = discover_or_create_appsync(region, profile)

    # Connect
    ws = await connect_to_appsync(asdict(config))

    # Subscribe to request channel
    request_channel = f"/stelvio/{channel_segment(app_name)}/{channel_segment(env)}/in"
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
            # Keepalive - also clean up stale buffers
            case "ka":
                cleanup_stale_buffers(_request_chunk_buffers)
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
                # Anyone with the API key can publish here; one bad message must not end the
                # server
                try:
                    await _handle_data_message(data, ws, config.api_key, app_name, env, admission)
                except Exception:
                    console.print(
                        "[bold red]Error handling a request:[/bold red]\n"
                        f"[red]{escape(traceback.format_exc())}[/red]"
                    )
            case _:
                pass


@contextlib.contextmanager
def _signal_wakeup(loop: asyncio.AbstractEventLoop) -> Iterator[None]:
    """Wake the selector even when a worker receives the OS signal.

    Python runs signal handlers on the main thread. Without a wakeup fd, that
    thread can stay asleep in select before our handler can schedule cancellation.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    reader, writer = socket.socketpair()
    with reader, writer:
        reader.setblocking(False)
        writer.setblocking(False)

        def drain() -> None:
            with contextlib.suppress(BlockingIOError):
                while reader.recv(4096):
                    pass

        try:
            loop.add_reader(reader.fileno(), drain)
        except NotImplementedError:
            # ProactorEventLoop manages its own wakeup descriptor.
            yield
            return
        try:
            previous_fd = signal.set_wakeup_fd(writer.fileno(), warn_on_full_buffer=False)
            try:
                yield
            finally:
                signal.set_wakeup_fd(previous_fd)
        finally:
            loop.remove_reader(reader.fileno())


def run_bridge_server(
    region: str,
    profile: str,
    app_name: str,
    env: str,
    admission: Callable[[str], BaseException | None] | None = None,
) -> None:
    """Run the bridge without joining its handler pool on interruption."""
    with asyncio.Runner() as runner, _signal_wakeup(runner.get_loop()):
        runner.run(
            main(region=region, profile=profile, app_name=app_name, env=env, admission=admission)
        )
