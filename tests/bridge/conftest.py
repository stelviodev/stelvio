import asyncio
from unittest.mock import MagicMock

import pytest
from websockets.asyncio.connection import Connection
from websockets.client import ClientProtocol
from websockets.protocol import OPEN
from websockets.uri import parse_uri


@pytest.fixture
def unresponsive_websocket():
    """Real websocket close logic with a transport whose peer never acknowledges close."""
    connections = []

    def create():
        loop = asyncio.get_running_loop()
        protocol = ClientProtocol(parse_uri("wss://example.com"), state=OPEN)
        websocket = Connection(protocol, ping_interval=None)
        transport = MagicMock(spec=asyncio.Transport)
        transport.is_closing.return_value = False
        transport.abort.side_effect = lambda: loop.call_soon(websocket.connection_lost, None)
        websocket.connection_made(transport)
        connections.append(websocket)
        return websocket

    yield create

    for websocket in connections:
        if not websocket.connection_lost_waiter.done():
            websocket.connection_lost(None)
