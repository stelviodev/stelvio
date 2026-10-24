"""Subprocess fixture for a real CLI exit with a blocked executor worker."""

import asyncio
import importlib
import json
import signal
import sys
from threading import Event, get_ident

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


def blocked_handler(loop):
    print("HANDLER_STARTED", flush=True)
    handler_started.set()
    if sys.argv[1:] == ["worker"]:
        # The loop may already be asleep if this worker was slow to start.
        loop.call_soon_threadsafe(lambda: None)
        # The parent releases this read only after the loop enters its selector.
        assert sys.stdin.buffer.read(1), "Parent did not release worker signal"
        signal.pthread_kill(get_ident(), signal.SIGINT)
    Event().wait()
    print("HANDLER_FINISHED", flush=True)


class BridgeHandler:
    async def handle_bridge_event(self, _data):
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(None, blocked_handler, loop)
        if isinstance(future, InterruptFuture):
            assert handler_started.wait(timeout=5), "Handler did not start"
            future.interrupt_on_registration = True
        return await future


async def connect(_config):
    return WebSocket()


def run_bridge():
    listener.run_bridge_server("us-east-1", "default", "app", "dev")


def announce_selector_wait(loop):
    select = loop._selector.select
    announced = False

    def select_when_ready(timeout=None):
        nonlocal announced
        if handler_started.is_set() and not announced:
            announced = True
            print("LOOP_WAITING", flush=True)
        return select(timeout)

    loop._selector.select = select_when_ready
    return loop


if __name__ == "__main__":
    if sys.argv[1:] == ["worker"]:
        new_event_loop = asyncio.new_event_loop
        asyncio.new_event_loop = lambda: announce_selector_wait(new_event_loop())
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
