"""Bridge shutdown regressions for interrupted, still-running handlers."""

import asyncio
import json
import time
from threading import Event

import pytest

from stelvio.bridge.local import listener
from stelvio.bridge.remote.infrastructure import AppSyncResource


class _WebSocket:
    def __init__(self, first_message: str | None):
        self.first_message = first_message
        self.close_called = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.first_message is None:
            raise StopAsyncIteration
        message = self.first_message
        self.first_message = None
        return message

    async def close(self):
        self.close_called = True


def test_interrupt_closes_bridge_without_waiting_for_handler(monkeypatch):
    handler_started = Event()
    release_handler = Event()
    handler_finished = Event()
    event_message = json.dumps(
        {
            "type": "data",
            "event": json.dumps({"invoke_id": "request-1", "event": {}}),
        }
    )
    websocket = _WebSocket(event_message)
    interrupt_tasks = set()
    config = AppSyncResource(
        api_id="api-id",
        http_endpoint="https://example.com",
        realtime_endpoint="realtime.example.com",
        api_key="api-key",
    )

    def handler():
        handler_started.set()
        try:
            release_handler.wait(timeout=15)
        finally:
            handler_finished.set()

    class BridgeHandler:
        async def handle_bridge_event(self, _data):
            return await asyncio.get_running_loop().run_in_executor(None, handler)

    async def connect(_config):
        loop = asyncio.get_running_loop()

        async def interrupt_when_handler_starts():
            while not handler_started.is_set():
                await asyncio.sleep(0.01)
            loop.call_soon(lambda: (_ for _ in ()).throw(KeyboardInterrupt()))

        interrupt_tasks.add(loop.create_task(interrupt_when_handler_starts()))
        return websocket

    monkeypatch.setattr(listener, "discover_or_create_appsync", lambda *_args: config)
    monkeypatch.setattr(listener, "connect_to_appsync", connect)
    monkeypatch.setattr(listener, "subscribe_to_channel", lambda *_args: asyncio.sleep(0))
    monkeypatch.setattr(listener.WebsocketHandlers, "all", lambda: [BridgeHandler()])

    started = time.monotonic()
    try:
        with pytest.raises(KeyboardInterrupt):
            listener.run_bridge_server("us-east-1", "default", "app", "dev")

        assert time.monotonic() - started < 5
        assert handler_started.is_set()
        assert not handler_finished.is_set()
        assert websocket.close_called
    finally:
        release_handler.set()
        assert handler_finished.wait(timeout=3)


def test_websocket_close_is_bounded(monkeypatch):
    websocket = _WebSocket(None)

    async def slow_close():
        await asyncio.Future()

    websocket.close = slow_close
    monkeypatch.setattr(listener, "APPSYNC_CLOSE_TIMEOUT", 0.01)
    monkeypatch.setattr(
        listener,
        "discover_or_create_appsync",
        lambda *_args: AppSyncResource(
            api_id="api-id",
            http_endpoint="https://example.com",
            realtime_endpoint="realtime.example.com",
            api_key="api-key",
        ),
    )
    monkeypatch.setattr(
        listener, "connect_to_appsync", lambda _config: asyncio.sleep(0, result=websocket)
    )
    monkeypatch.setattr(listener, "subscribe_to_channel", lambda *_args: asyncio.sleep(0))

    with pytest.raises(TimeoutError):
        asyncio.get_event_loop().run_until_complete(
            listener.main("us-east-1", "default", "app", "dev")
        )
