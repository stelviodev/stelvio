import asyncio
import json
from unittest.mock import MagicMock

import pytest


class Socket:
    """Play frames, then stay silent for at most three seconds."""

    def __init__(self, frames=()):
        self.frames = list(frames)
        self.sent = []
        self.close_code = None
        self.transport = MagicMock(spec=asyncio.Transport)

    async def send(self, message):
        self.sent.append(json.loads(message))

    async def recv(self):
        if self.frames:
            return self.frames.pop(0)
        await asyncio.sleep(3)
        raise AssertionError("a silent socket had no deadline")


@pytest.fixture
def stub_env_vars(monkeypatch):
    """Set required environment variables for the stub module."""
    monkeypatch.setenv("STLV_APPSYNC_REALTIME", "realtime.example.com")
    monkeypatch.setenv("STLV_APPSYNC_HTTP", "https://example.com")
    monkeypatch.setenv("STLV_APPSYNC_API_KEY", "test_api_key")
    monkeypatch.setenv("STLV_APP_NAME", "test_app")
    monkeypatch.setenv("STLV_STAGE", "dev")
    monkeypatch.setenv("STLV_FUNCTION_NAME", "test_function")
    monkeypatch.setenv("STLV_DEV_ENDPOINT_ID", "test_endpoint_id")


@pytest.fixture(scope="session")
def mock_stlv_chunking():
    """Alias stlv_chunking to _chunking - stlv_chunking only exists when deployed to Lambda."""
    import sys

    from stelvio.bridge import _chunking

    sys.modules["stlv_chunking"] = _chunking
    yield
    del sys.modules["stlv_chunking"]


@pytest.fixture
def reset_global_state(stub_env_vars, mock_stlv_chunking):
    """Reset global state before and after each test."""
    # Import here to ensure env vars are set
    from stelvio.bridge.remote.stub import function_stub

    # Reset before test
    function_stub._event_loop = None
    function_stub._ws_connection = None
    function_stub._last_connected = None
    function_stub._subscribed = False
    function_stub._response_chunk_buffers = {}

    yield function_stub

    # Reset after test
    function_stub._event_loop = None
    function_stub._ws_connection = None
    function_stub._last_connected = None
    function_stub._subscribed = False
    function_stub._response_chunk_buffers = {}
