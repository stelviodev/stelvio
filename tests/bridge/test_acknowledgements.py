"""AppSync handshake behavior shared by the local bridge and deployed Lambda stub."""

import asyncio
import json
import re
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from stelvio.bridge import _chunking
from stelvio.bridge.local import listener

from .conftest import Socket

KA = '{"type":"ka"}'


class KeepaliveSocket(Socket):
    """Keep sending keepalives, failing after three seconds if the deadline breaks."""

    def __init__(self):
        super().__init__()
        self.started = time.monotonic()

    async def recv(self):
        await asyncio.sleep(0.001)
        assert time.monotonic() - self.started < 3, "keepalives extended the acknowledgement wait"
        return KA


@pytest.fixture(params=["listener", "stub"])
def side(request, monkeypatch):
    monkeypatch.setattr(_chunking, "APPSYNC_ACK_TIMEOUT", 0.02)
    if request.param == "listener":
        config = {
            "http_endpoint": "example.com",
            "realtime_endpoint": "rt.example.com",
            "api_key": "key",
        }
        return SimpleNamespace(
            connect=lambda: listener.connect_to_appsync(config),
            subscribe=lambda ws: listener.subscribe_to_channel(ws, "/stelvio/app/dev/in", "key"),
            subscription_id="request-sub",
            channel="/stelvio/app/dev/in",
            aborts_connect=False,
            use=lambda ws: monkeypatch.setattr(
                listener.websockets, "connect", AsyncMock(return_value=ws)
            ),
        )
    stub = request.getfixturevalue("reset_global_state")
    return SimpleNamespace(
        connect=stub.connect_to_appsync,
        subscribe=stub.subscribe_to_channel,
        subscription_id="response-sub",
        channel="/stelvio/test-app/dev/out",
        aborts_connect=True,
        use=lambda ws: monkeypatch.setattr(stub.websockets, "connect", AsyncMock(return_value=ws)),
    )


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_connect_accepts_ack_after_keepalive(side):
    ws = Socket([KA, '{"type":"connection_ack"}', "next"])
    side.use(ws)

    assert _run(side.connect()) is ws
    assert ws.frames == ["next"]


@pytest.mark.parametrize(
    ("frames", "error", "text"),
    [
        pytest.param(
            [KA, '{"type":"subscribe_success"}'],
            ConnectionError,
            "Expected AppSync connection_ack, received 'subscribe_success'",
            id="wrong-frame-after-keepalive",
        ),
        pytest.param(
            ['{"type":"connection_error","errors":[{"errorType":"UnauthorizedException"}]}'],
            ConnectionError,
            "Expected AppSync connection_ack, received 'connection_error': "
            "[{'errorType': 'UnauthorizedException'}]",
            id="unauthorized",
        ),
        pytest.param(
            [], TimeoutError, "Timed out waiting for AppSync connection_ack", id="silent"
        ),
    ],
)
def test_connect_failure_reports_appsync_error(side, frames, error, text):
    ws = Socket([*frames, "next"] if frames else [])
    side.use(ws)

    with pytest.raises(error, match=f"^{re.escape(text)}$"):
        _run(side.connect())

    assert ws.frames == (["next"] if frames else [])
    if side.aborts_connect:
        ws.transport.abort.assert_called_once_with()


def test_subscribe_accepts_ack_after_keepalive(side):
    ack = json.dumps({"type": "subscribe_success", "id": side.subscription_id})
    ws = Socket([KA, ack, "next"])

    _run(side.subscribe(ws))
    assert ws.frames == ["next"]


@pytest.mark.parametrize(
    ("frame", "text"),
    [
        pytest.param(
            '{"type":"data","event":"{}"}',
            "Expected AppSync subscribe_success, received 'data'",
            id="data-before-ack",
        ),
        pytest.param(
            '{"type":"publish_success"}',
            "Expected AppSync subscribe_success, received 'publish_success'",
            id="wrong-operation",
        ),
        pytest.param(
            '{"type":"subscribe_success","id":"other-sub"}',
            "AppSync subscribe_success acknowledgement had an unexpected operation ID",
            id="wrong-id",
        ),
        pytest.param(
            '{"type":"subscribe_success"}',
            "AppSync subscribe_success acknowledgement had an unexpected operation ID",
            id="missing-id",
        ),
        pytest.param(
            '{"type":"connection_error","errors":[{"errorType":"UnauthorizedException"}]}',
            "Expected AppSync subscribe_success, received 'connection_error': "
            "[{'errorType': 'UnauthorizedException'}]",
            id="unauthorized",
        ),
    ],
)
def test_subscribe_rejects_unexpected_ack_after_keepalive(side, frame, text):
    ws = Socket([KA, frame, "next"])

    with pytest.raises(ConnectionError, match=f"^{re.escape(text)}$"):
        _run(side.subscribe(ws))

    assert ws.frames == ["next"]


def test_subscribe_error_names_the_channel(side):
    ws = Socket([KA, '{"type":"subscribe_error","errors":[{"message":"denied"}]}', "next"])
    text = f"AppSync subscribe to {side.channel!r} failed: [{{'message': 'denied'}}]"

    with pytest.raises(ConnectionError, match=f"^{re.escape(text)}$"):
        _run(side.subscribe(ws))

    assert ws.frames == ["next"]


def test_subscribe_silent_socket_has_descriptive_timeout(side):
    with pytest.raises(TimeoutError, match=r"^Timed out waiting for AppSync subscribe_success$"):
        _run(side.subscribe(Socket()))


@pytest.mark.parametrize("operation", ["connect", "subscribe"])
def test_acknowledgement_has_one_deadline_across_keepalives(side, operation):
    ws = KeepaliveSocket()
    side.use(ws)
    expected_type = "connection_ack" if operation == "connect" else "subscribe_success"

    with pytest.raises(TimeoutError, match=f"^Timed out waiting for AppSync {expected_type}$"):
        _run(side.connect() if operation == "connect" else side.subscribe(ws))

    if operation == "connect" and side.aborts_connect:
        ws.transport.abort.assert_called_once_with()
