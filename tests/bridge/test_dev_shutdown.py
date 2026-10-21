"""Bridge shutdown regressions for interrupted, still-running handlers."""

import asyncio
import json
import os
import selectors
import signal
import subprocess
import sys
import time
from threading import Event, Thread
from unittest.mock import AsyncMock, MagicMock

import pytest

from stelvio.bridge.local import listener
from stelvio.bridge.remote.infrastructure import AppSyncResource


class _WebSocket:
    def __init__(self, first_message: str | None):
        self.first_message = first_message
        self.close_called = False
        self.transport = MagicMock(spec=asyncio.Transport)

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
    websocket.transport.abort.assert_called_once_with()


def test_close_timeout_aborts_transport(monkeypatch, unresponsive_websocket):
    monkeypatch.setattr(listener, "APPSYNC_CLOSE_TIMEOUT", 0.01)

    async def check():
        websocket = unresponsive_websocket()
        with pytest.raises(TimeoutError):
            await listener._close_connection(websocket)
        websocket.transport.abort.assert_called_once_with()
        await asyncio.sleep(0)
        assert websocket.close_code is not None

    asyncio.get_event_loop().run_until_complete(check())


@pytest.mark.parametrize("error_type", [OSError, asyncio.CancelledError])
def test_close_failure_aborts_transport(error_type):
    websocket = _WebSocket(None)
    failure = error_type("close failed")
    websocket.close = AsyncMock(side_effect=failure)

    with pytest.raises(error_type) as caught:
        asyncio.get_event_loop().run_until_complete(listener._close_connection(websocket))

    assert caught.value is failure
    websocket.transport.abort.assert_called_once_with()


@pytest.mark.parametrize("phase", ["subscribe", "handler"])
@pytest.mark.parametrize("error_type", [ConnectionError, asyncio.CancelledError])
def test_main_preserves_failure_when_close_times_out(monkeypatch, phase, error_type):
    websocket = _WebSocket('{"type":"data"}')
    failure = error_type("original failure")

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
    monkeypatch.setattr(listener, "connect_to_appsync", AsyncMock(return_value=websocket))
    monkeypatch.setattr(
        listener,
        "subscribe_to_channel",
        AsyncMock(side_effect=failure if phase == "subscribe" else None),
    )
    monkeypatch.setattr(listener, "_handle_data_message", AsyncMock(side_effect=failure))

    with pytest.raises(error_type) as caught:
        asyncio.get_event_loop().run_until_complete(
            listener.main("us-east-1", "default", "app", "dev")
        )

    assert caught.value is failure
    websocket.transport.abort.assert_called_once_with()


@pytest.mark.parametrize("outcome", ["success", "error", "cancel", "interrupt"])
def test_bridge_restores_sigint_handler(monkeypatch, outcome):
    handlers = [signal.default_int_handler]

    def install_handler(_signum, handler):
        previous = handlers[-1]
        handlers.append(handler)
        return previous

    monkeypatch.setattr(signal, "getsignal", lambda _signum: handlers[-1])
    monkeypatch.setattr(signal, "signal", install_handler)
    cleanup_finished = False

    async def main(**_kwargs):
        nonlocal cleanup_finished
        try:
            if outcome == "interrupt":
                handlers[-1](signal.SIGINT, None)
                await asyncio.Future()
            elif outcome == "cancel":
                raise asyncio.CancelledError
            elif outcome == "error":
                raise ValueError("bridge failed")
        finally:
            await asyncio.sleep(0)
            cleanup_finished = True

    monkeypatch.setattr(listener, "main", main)
    expected = {
        "success": None,
        "error": ValueError,
        "cancel": asyncio.CancelledError,
        "interrupt": KeyboardInterrupt,
    }[outcome]
    if expected is None:
        listener.run_bridge_server("us-east-1", "default", "app", "dev")
    else:
        with pytest.raises(expected):
            listener.run_bridge_server("us-east-1", "default", "app", "dev")

    assert cleanup_finished
    assert len(handlers) == 3
    assert handlers[-1] is signal.default_int_handler


def test_sigint_cancellation_is_deferred_and_second_interrupt_is_immediate(monkeypatch):
    loop = asyncio.get_event_loop()
    task = loop.create_task(asyncio.sleep(0))
    installed = MagicMock(return_value=signal.default_int_handler)
    monkeypatch.setattr(signal, "getsignal", lambda _signum: signal.default_int_handler)
    monkeypatch.setattr(signal, "signal", installed)

    with listener._cancel_on_sigint(loop, task):
        handler = installed.call_args.args[1]
        handler(signal.SIGINT, None)
        assert task.cancelling() == 0
        with pytest.raises(KeyboardInterrupt):
            handler(signal.SIGINT, None)
        with pytest.raises(asyncio.CancelledError):
            loop.run_until_complete(task)


def test_bridge_preserves_custom_sigint_handler(monkeypatch):
    custom_handler = MagicMock()
    installed = MagicMock()
    monkeypatch.setattr(signal, "getsignal", lambda _signum: custom_handler)
    monkeypatch.setattr(signal, "signal", installed)
    monkeypatch.setattr(listener, "main", AsyncMock())

    listener.run_bridge_server("us-east-1", "default", "app", "dev")

    installed.assert_not_called()
    custom_handler.assert_not_called()


def test_bridge_runs_outside_main_thread(monkeypatch):
    installed = MagicMock()
    monkeypatch.setattr(signal, "signal", installed)
    monkeypatch.setattr(listener, "main", AsyncMock())
    errors = []

    def run():
        try:
            listener.run_bridge_server("us-east-1", "default", "app", "dev")
        except BaseException as error:
            errors.append(error)

    thread = Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert not errors
    installed.assert_not_called()


@pytest.mark.parametrize("interrupt_phase", ["parent", "registration"])
def test_cli_interrupt_exits_with_blocked_handler(interrupt_phase):
    # Exercise main's real os._exit path without deploying or contacting AWS.
    with subprocess.Popen(  # noqa: S603
        [sys.executable, "-u", "-m", "tests.bridge.shutdown_child", interrupt_phase],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as child:
        try:
            output = b""
            deadline = time.monotonic() + 15
            with selectors.DefaultSelector() as selector:
                selector.register(child.stdout, selectors.EVENT_READ)
                while b"HANDLER_STARTED\n" not in output:
                    remaining = deadline - time.monotonic()
                    assert remaining > 0, "Child did not start its handler"
                    assert selector.select(remaining), "Child did not start its handler"
                    chunk = os.read(child.stdout.fileno(), 4096)
                    assert chunk, child.stderr.read().decode()
                    output += chunk

            if interrupt_phase == "parent":
                child.send_signal(signal.SIGINT)
            stdout, stderr = child.communicate(timeout=5)
            output += stdout
            diagnostics = f"stdout:\n{output.decode()}\nstderr:\n{stderr.decode()}"
            assert child.returncode == 1, diagnostics
            assert b"SOCKET_CLOSED\n" in output, diagnostics
            assert b"HANDLER_FINISHED\n" not in output, diagnostics
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=5)
