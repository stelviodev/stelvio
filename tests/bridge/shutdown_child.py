"""Subprocess fixture for a real CLI exit with a blocked executor worker."""

import asyncio
import importlib
import json
import signal
import sys
from threading import Event

from stelvio.bridge.local import listener
from stelvio.bridge.remote.infrastructure import AppSyncResource

handler_started = Event()


class InterruptFuture(asyncio.Future):
    interrupt_on_registration = False

    def add_done_callback(self, callback, *, context=None):
        if self.interrupt_on_registration:
            self.interrupt_on_registration = False
            # The coroutine has yielded, but its task cannot resume until this
            # callback is registered. A raw KeyboardInterrupt strands the task.
            signal.raise_signal(signal.SIGINT)
        return super().add_done_callback(callback, context=context)


class WebSocket:
    def __aiter__(self):
        return self

    async def __anext__(self):
        return json.dumps({"type": "data", "event": json.dumps({"invoke_id": "request-1"})})

    async def close(self):
        print("SOCKET_CLOSED", flush=True)


def blocked_handler():
    print("HANDLER_STARTED", flush=True)
    handler_started.set()
    Event().wait()
    print("HANDLER_FINISHED", flush=True)


class BridgeHandler:
    async def handle_bridge_event(self, _data):
        future = asyncio.get_running_loop().run_in_executor(None, blocked_handler)
        if isinstance(future, InterruptFuture):
            assert handler_started.wait(timeout=5), "Handler did not start"
            future.interrupt_on_registration = True
        return await future


async def connect(_config):
    return WebSocket()


def run_bridge():
    listener.run_bridge_server("us-east-1", "default", "app", "dev")


if __name__ == "__main__":
    if sys.argv[1:] == ["registration"]:
        asyncio.BaseEventLoop.create_future = lambda self: InterruptFuture(loop=self)
    listener.discover_or_create_appsync = lambda *_args: AppSyncResource(
        api_id="api-id",
        http_endpoint="https://example.com",
        realtime_endpoint="realtime.example.com",
        api_key="api-key",
    )
    listener.connect_to_appsync = connect
    listener.subscribe_to_channel = lambda *_args: asyncio.sleep(0)
    listener.WebsocketHandlers.all = lambda: [BridgeHandler()]
    cli_module = importlib.import_module("stelvio.cli")
    cli_module.cli = run_bridge
    cli_module.main()
