"""Tests for the Lambda function stub that forwards invocations to local dev server."""

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# We need to set up environment variables before importing the module
@pytest.fixture(autouse=True)
def stub_env_vars(monkeypatch):
    """Set required environment variables for the stub module."""
    monkeypatch.setenv("STLV_APPSYNC_REALTIME", "realtime.example.com")
    monkeypatch.setenv("STLV_APPSYNC_HTTP", "https://example.com")
    monkeypatch.setenv("STLV_APPSYNC_API_KEY", "test_api_key")
    monkeypatch.setenv("STLV_APP_NAME", "test_app")
    monkeypatch.setenv("STLV_STAGE", "dev")
    monkeypatch.setenv("STLV_FUNCTION_NAME", "test_function")
    monkeypatch.setenv("STLV_DEV_ENDPOINT_ID", "test_endpoint_id")


@pytest.fixture
def reset_global_state():
    """Reset global state before and after each test."""
    # Import here to ensure env vars are set
    from stelvio.bridge.remote.stub import function_stub

    # Reset before test
    function_stub._event_loop = None
    function_stub._ws_connection = None
    function_stub._last_connected = None
    function_stub._subscribed = False

    yield function_stub

    # Reset after test
    function_stub._event_loop = None
    function_stub._ws_connection = None
    function_stub._last_connected = None
    function_stub._subscribed = False


def test_creates_new_loop_when_none_exists(reset_global_state):
    """Should create a new event loop when none exists."""
    stub = reset_global_state

    loop = stub.get_or_create_loop()

    assert loop is not None
    assert isinstance(loop, asyncio.AbstractEventLoop)
    assert stub._event_loop is loop


def test_reuses_existing_loop(reset_global_state):
    """Should reuse existing event loop if available."""
    stub = reset_global_state

    loop1 = stub.get_or_create_loop()
    loop2 = stub.get_or_create_loop()

    assert loop1 is loop2


def test_creates_new_loop_when_closed(reset_global_state):
    """Should create new loop if existing one is closed."""
    stub = reset_global_state

    loop1 = stub.get_or_create_loop()
    loop1.close()

    loop2 = stub.get_or_create_loop()

    assert loop2 is not loop1
    assert not loop2.is_closed()


@patch("stelvio.bridge.remote.stub.function_stub.websockets.connect", new_callable=AsyncMock)
def test_connect_with_correct_uri_and_subprotocols(mock_connect, reset_global_state):
    """Should connect to AppSync with correct URI and auth subprotocols."""
    stub = reset_global_state

    mock_ws = AsyncMock()
    mock_connect.return_value = mock_ws
    mock_ws.recv = AsyncMock(return_value='{"type":"connection_ack"}')

    result = asyncio.run(stub.connect_to_appsync())

    # Verify connection was made
    mock_connect.assert_called_once()
    call_args = mock_connect.call_args

    # Check URI
    assert call_args[0][0] == "wss://realtime.example.com/event/realtime"

    # Check subprotocols contain auth header
    subprotocols = call_args[1]["subprotocols"]
    assert "aws-appsync-event-ws" in subprotocols
    assert any(sp.startswith("header-") for sp in subprotocols)

    # Verify connection_init was sent
    mock_ws.send.assert_called_once()
    sent_data = json.loads(mock_ws.send.call_args[0][0])
    assert sent_data["type"] == "connection_init"

    assert result == mock_ws


@patch("stelvio.bridge.remote.stub.function_stub.websockets.connect", new_callable=AsyncMock)
def test_waits_for_connection_ack(mock_connect, reset_global_state):
    """Should wait for connection_ack after sending connection_init."""
    stub = reset_global_state

    mock_ws = AsyncMock()
    mock_connect.return_value = mock_ws
    mock_ws.recv = AsyncMock(return_value='{"type":"connection_ack"}')

    asyncio.run(stub.connect_to_appsync())

    # Verify recv was called to wait for ack
    mock_ws.recv.assert_called_once()


def test_sends_subscribe_message(reset_global_state):
    """Should send correct subscribe message to response channel."""
    stub = reset_global_state
    mock_ws = AsyncMock()

    asyncio.run(stub.subscribe_to_channel(mock_ws))

    # Verify subscribe message sent
    mock_ws.send.assert_called_once()
    sent_data = json.loads(mock_ws.send.call_args[0][0])

    assert sent_data["type"] == "subscribe"
    assert sent_data["id"] == "response-sub"
    assert sent_data["channel"] == "/stelvio/test_app/dev/out"
    assert sent_data["authorization"]["x-api-key"] == "test_api_key"


def test_waits_for_subscribe_success(reset_global_state):
    """Should wait for subscribe_success response."""
    stub = reset_global_state
    mock_ws = AsyncMock()
    mock_ws.recv = AsyncMock(return_value='{"type":"subscribe_success"}')

    asyncio.run(stub.subscribe_to_channel(mock_ws))

    mock_ws.recv.assert_called_once()


@patch("stelvio.bridge.remote.stub.function_stub.connect_to_appsync", new_callable=AsyncMock)
def test_creates_new_connection_when_none_exists(mock_connect, reset_global_state):
    """Should create new connection when no existing connection."""
    stub = reset_global_state

    mock_ws = AsyncMock()
    mock_ws.close_code = None
    mock_connect.return_value = mock_ws

    ws, reused = asyncio.run(stub.get_or_create_connection())

    assert ws == mock_ws
    assert reused is False
    mock_connect.assert_called_once()


@patch("stelvio.bridge.remote.stub.function_stub.connect_to_appsync", new_callable=AsyncMock)
def test_reuses_valid_connection(mock_connect, reset_global_state):
    """Should reuse existing valid connection."""
    stub = reset_global_state

    # Set up existing connection
    mock_ws = AsyncMock()
    mock_ws.close_code = None  # Connection is open
    stub._ws_connection = mock_ws
    stub._last_connected = time.time()  # Recently connected

    ws, reused = asyncio.run(stub.get_or_create_connection())

    assert ws == mock_ws
    assert reused is True
    mock_connect.assert_not_called()


@patch("stelvio.bridge.remote.stub.function_stub.connect_to_appsync", new_callable=AsyncMock)
def test_creates_new_connection_when_stale(mock_connect, reset_global_state):
    """Should create new connection when existing one is stale."""
    stub = reset_global_state

    # Set up stale connection (connected more than 4 minutes ago)
    old_ws = AsyncMock()
    old_ws.close_code = None
    stub._ws_connection = old_ws
    stub._last_connected = time.time() - 300  # 5 minutes ago

    new_ws = AsyncMock()
    new_ws.close_code = None
    mock_connect.return_value = new_ws

    ws, reused = asyncio.run(stub.get_or_create_connection())

    assert ws == new_ws
    assert reused is False
    mock_connect.assert_called_once()


@patch("stelvio.bridge.remote.stub.function_stub.connect_to_appsync", new_callable=AsyncMock)
def test_creates_new_connection_when_closed(mock_connect, reset_global_state):
    """Should create new connection when existing one is closed."""
    stub = reset_global_state

    # Set up closed connection
    closed_ws = AsyncMock()
    closed_ws.close_code = 1000  # Normal closure
    stub._ws_connection = closed_ws
    stub._last_connected = time.time()

    new_ws = AsyncMock()
    new_ws.close_code = None
    mock_connect.return_value = new_ws

    ws, reused = asyncio.run(stub.get_or_create_connection())

    assert ws == new_ws
    assert reused is False


@patch("stelvio.bridge.remote.stub.function_stub.connect_to_appsync", new_callable=AsyncMock)
def test_resets_subscribed_flag_on_new_connection(mock_connect, reset_global_state):
    """Should reset subscribed flag when creating new connection."""
    stub = reset_global_state
    stub._subscribed = True

    mock_ws = AsyncMock()
    mock_ws.close_code = None
    mock_connect.return_value = mock_ws

    asyncio.run(stub.get_or_create_connection())

    assert stub._subscribed is False


@patch("stelvio.bridge.remote.stub.function_stub.connect_to_appsync", new_callable=AsyncMock)
def test_resets_state_on_connection_error(mock_connect, reset_global_state):
    """Should reset state when connection fails."""
    stub = reset_global_state

    mock_connect.side_effect = Exception("Connection failed")

    with pytest.raises(Exception, match="Connection failed"):
        asyncio.run(stub.get_or_create_connection())

    assert stub._ws_connection is None
    assert stub._last_connected is None
    assert stub._subscribed is False


@patch("stelvio.bridge.remote.stub.function_stub.subscribe_to_channel", new_callable=AsyncMock)
def test_subscribes_when_not_subscribed(mock_subscribe, reset_global_state):
    """Should subscribe when not already subscribed."""
    stub = reset_global_state
    mock_ws = AsyncMock()

    asyncio.run(stub.ensure_subscribed(mock_ws))

    mock_subscribe.assert_called_once_with(mock_ws)
    assert stub._subscribed is True


@patch("stelvio.bridge.remote.stub.function_stub.subscribe_to_channel", new_callable=AsyncMock)
def test_skips_subscribe_when_already_subscribed(mock_subscribe, reset_global_state):
    """Should skip subscription when already subscribed."""
    stub = reset_global_state
    stub._subscribed = True
    mock_ws = AsyncMock()

    asyncio.run(stub.ensure_subscribed(mock_ws))

    mock_subscribe.assert_not_called()


@patch("uuid.uuid4")
def test_publishes_correct_message(mock_uuid, reset_global_state):
    """Should publish message with correct format."""
    stub = reset_global_state
    mock_uuid.return_value = "test-uuid-1234"
    mock_ws = AsyncMock()

    channel = "/stelvio/test_app/dev/in"
    data = {"requestId": "req-123", "event": {"test": "data"}}

    asyncio.run(stub.publish_to_appsync(mock_ws, channel, data))

    mock_ws.send.assert_called_once()
    sent_data = json.loads(mock_ws.send.call_args[0][0])

    assert sent_data["id"] == "test-uuid-1234"
    assert sent_data["type"] == "publish"
    assert sent_data["channel"] == channel
    assert json.loads(sent_data["events"][0]) == data
    assert sent_data["authorization"]["x-api-key"] == "test_api_key"


def test_returns_matching_response(reset_global_state):
    """Should return response when requestId matches."""
    stub = reset_global_state
    mock_ws = AsyncMock()

    response_data = {
        "requestId": "test-request-123",
        "success": True,
        "result": {"statusCode": 200},
    }

    mock_ws.recv = AsyncMock(
        return_value=json.dumps({"type": "data", "event": json.dumps(response_data)})
    )

    result = asyncio.run(stub.wait_for_response(mock_ws, "test-request-123", timeout=5))

    assert result == response_data


def test_skips_keepalive_messages(reset_global_state):
    """Should skip keepalive messages and continue waiting."""
    stub = reset_global_state
    mock_ws = AsyncMock()

    response_data = {
        "requestId": "test-request-123",
        "success": True,
        "result": {"statusCode": 200},
    }

    # First return keepalive, then return actual response
    mock_ws.recv = AsyncMock(
        side_effect=[
            json.dumps({"type": "ka"}),
            json.dumps({"type": "data", "event": json.dumps(response_data)}),
        ]
    )

    result = asyncio.run(stub.wait_for_response(mock_ws, "test-request-123", timeout=5))

    assert result == response_data
    assert mock_ws.recv.call_count == 2


def test_skips_non_matching_request_ids(reset_global_state):
    """Should skip responses with non-matching requestId."""
    stub = reset_global_state
    mock_ws = AsyncMock()

    wrong_response = {"requestId": "wrong-id", "success": True}
    correct_response = {"requestId": "correct-id", "success": True}

    mock_ws.recv = AsyncMock(
        side_effect=[
            json.dumps({"type": "data", "event": json.dumps(wrong_response)}),
            json.dumps({"type": "data", "event": json.dumps(correct_response)}),
        ]
    )

    result = asyncio.run(stub.wait_for_response(mock_ws, "correct-id", timeout=5))

    assert result == correct_response


def test_returns_none_on_timeout(reset_global_state):
    """Should return None when timeout is reached."""
    stub = reset_global_state
    mock_ws = AsyncMock()

    async def slow_recv():
        await asyncio.sleep(10)
        return json.dumps({"type": "data"})

    mock_ws.recv = slow_recv

    result = asyncio.run(stub.wait_for_response(mock_ws, "test-id", timeout=0.1))

    assert result is None


@patch("stelvio.bridge.remote.stub.function_stub.async_handler", new_callable=AsyncMock)
def test_handler_calls_async_handler(mock_async_handler, reset_global_state):
    """Should call async_handler with event and context."""
    stub = reset_global_state
    mock_async_handler.return_value = {"statusCode": 200}

    event = {"httpMethod": "GET", "path": "/test"}
    context = MagicMock()

    result = stub.handler(event, context)

    mock_async_handler.assert_called_once_with(event, context)
    assert result == {"statusCode": 200}


@patch("stelvio.bridge.remote.stub.function_stub.wait_for_response", new_callable=AsyncMock)
@patch("stelvio.bridge.remote.stub.function_stub.publish_to_appsync", new_callable=AsyncMock)
@patch("stelvio.bridge.remote.stub.function_stub.ensure_subscribed", new_callable=AsyncMock)
@patch("stelvio.bridge.remote.stub.function_stub.get_or_create_connection", new_callable=AsyncMock)
def test_successful_invocation(
    mock_get_connection,
    mock_ensure_subscribed,
    mock_publish,
    mock_wait_response,
    reset_global_state,
):
    """Should return response from local dev server on success."""
    stub = reset_global_state

    mock_ws = AsyncMock()
    mock_get_connection.return_value = (mock_ws, True)

    mock_wait_response.return_value = {
        "requestId": "req-123",
        "success": True,
        "result": {"statusCode": 200, "body": "OK"},
    }

    event = {"httpMethod": "GET", "path": "/test"}
    context = MagicMock()
    context.aws_request_id = "req-123"
    context.identity = MagicMock()

    result = asyncio.run(stub.async_handler(event, context))

    assert result == {"statusCode": 200, "body": "OK"}


@patch("stelvio.bridge.remote.stub.function_stub.get_or_create_connection", new_callable=AsyncMock)
def test_connection_failure_returns_500(mock_get_connection, reset_global_state):
    """Should return 500 error when connection fails."""
    stub = reset_global_state

    mock_get_connection.side_effect = Exception("Connection failed")

    event = {"httpMethod": "GET"}
    context = MagicMock()

    result = asyncio.run(stub.async_handler(event, context))

    assert result["statusCode"] == 500
    assert "Failed to connect to AppSync" in result["body"]


@patch("stelvio.bridge.remote.stub.function_stub.ensure_subscribed", new_callable=AsyncMock)
@patch("stelvio.bridge.remote.stub.function_stub.get_or_create_connection", new_callable=AsyncMock)
def test_subscription_failure_resets_state(
    mock_get_connection, mock_ensure_subscribed, reset_global_state
):
    """Should reset connection state when subscription fails."""
    stub = reset_global_state

    mock_ws = AsyncMock()
    mock_get_connection.return_value = (mock_ws, False)
    mock_ensure_subscribed.side_effect = Exception("Subscribe failed")

    # Set up some state that should be reset
    stub._ws_connection = mock_ws
    stub._subscribed = True

    event = {"httpMethod": "GET"}
    context = MagicMock()

    result = asyncio.run(stub.async_handler(event, context))

    assert result["statusCode"] == 500
    assert "Failed to subscribe" in result["body"]
    assert stub._ws_connection is None
    assert stub._subscribed is False


@patch("stelvio.bridge.remote.stub.function_stub.wait_for_response", new_callable=AsyncMock)
@patch("stelvio.bridge.remote.stub.function_stub.publish_to_appsync", new_callable=AsyncMock)
@patch("stelvio.bridge.remote.stub.function_stub.ensure_subscribed", new_callable=AsyncMock)
@patch("stelvio.bridge.remote.stub.function_stub.get_or_create_connection", new_callable=AsyncMock)
def test_publish_failure_returns_500(
    mock_get_connection,
    mock_ensure_subscribed,
    mock_publish,
    mock_wait_response,
    reset_global_state,
):
    """Should return 500 error when publish fails."""
    stub = reset_global_state

    mock_ws = AsyncMock()
    mock_get_connection.return_value = (mock_ws, True)
    mock_publish.side_effect = Exception("Publish failed")

    event = {"httpMethod": "GET"}
    context = MagicMock()
    context.aws_request_id = "req-123"
    context.client_context = None
    context.identity = MagicMock()
    context.identity.cognito_identity_id = None
    context.identity.cognito_identity_pool_id = None
    context._epoch_deadline_time_in_ms = None
    context.invoked_function_arn = None
    context.tenant_id = None

    result = asyncio.run(stub.async_handler(event, context))

    assert result["statusCode"] == 500
    assert "Failed to publish to AppSync" in result["body"]


@patch("stelvio.bridge.remote.stub.function_stub.wait_for_response", new_callable=AsyncMock)
@patch("stelvio.bridge.remote.stub.function_stub.publish_to_appsync", new_callable=AsyncMock)
@patch("stelvio.bridge.remote.stub.function_stub.ensure_subscribed", new_callable=AsyncMock)
@patch("stelvio.bridge.remote.stub.function_stub.get_or_create_connection", new_callable=AsyncMock)
def test_timeout_returns_helpful_error(
    mock_get_connection,
    mock_ensure_subscribed,
    mock_publish,
    mock_wait_response,
    reset_global_state,
):
    """Should return helpful error when local dev server times out."""
    stub = reset_global_state

    mock_ws = AsyncMock()
    mock_get_connection.return_value = (mock_ws, True)
    mock_wait_response.return_value = None  # Timeout

    event = {"httpMethod": "GET"}
    context = MagicMock()
    context.aws_request_id = "req-123"
    context.identity = MagicMock()

    result = asyncio.run(stub.async_handler(event, context))

    assert result["statusCode"] == 500
    body = json.loads(result["body"])
    assert "Local dev server not responding" in body["error"]
    assert "Is 'stlv dev' running?" in body["hint"]


@patch("stelvio.bridge.remote.stub.function_stub.wait_for_response", new_callable=AsyncMock)
@patch("stelvio.bridge.remote.stub.function_stub.publish_to_appsync", new_callable=AsyncMock)
@patch("stelvio.bridge.remote.stub.function_stub.ensure_subscribed", new_callable=AsyncMock)
@patch("stelvio.bridge.remote.stub.function_stub.get_or_create_connection", new_callable=AsyncMock)
def test_error_from_local_dev_returned(
    mock_get_connection,
    mock_ensure_subscribed,
    mock_publish,
    mock_wait_response,
    reset_global_state,
):
    """Should return error details when local dev server returns error."""
    stub = reset_global_state

    mock_ws = AsyncMock()
    mock_get_connection.return_value = (mock_ws, True)
    mock_wait_response.return_value = {
        "requestId": "req-123",
        "success": False,
        "error": "Function raised exception",
        "errorType": "ValueError",
        "stackTrace": ["line1", "line2"],
    }

    event = {"httpMethod": "GET"}
    context = MagicMock()
    context.aws_request_id = "req-123"
    context.identity = MagicMock()

    result = asyncio.run(stub.async_handler(event, context))

    assert result["statusCode"] == 500
    body = json.loads(result["body"])
    assert body["error"] == "Function raised exception"
    assert body["errorType"] == "ValueError"
    assert body["stackTrace"] == ["line1", "line2"]


@patch("stelvio.bridge.remote.stub.function_stub.wait_for_response", new_callable=AsyncMock)
@patch("stelvio.bridge.remote.stub.function_stub.publish_to_appsync", new_callable=AsyncMock)
@patch("stelvio.bridge.remote.stub.function_stub.ensure_subscribed", new_callable=AsyncMock)
@patch("stelvio.bridge.remote.stub.function_stub.get_or_create_connection", new_callable=AsyncMock)
def test_publishes_correct_request_message(
    mock_get_connection,
    mock_ensure_subscribed,
    mock_publish,
    mock_wait_response,
    reset_global_state,
):
    """Should publish invocation with correct message format."""
    stub = reset_global_state

    mock_ws = AsyncMock()
    mock_get_connection.return_value = (mock_ws, True)
    mock_wait_response.return_value = {
        "requestId": "req-123",
        "success": True,
        "result": {"statusCode": 200},
    }

    event = {"httpMethod": "GET", "path": "/api/test"}
    context = MagicMock()
    context.aws_request_id = "req-123"
    context.client_context = None
    context.identity = MagicMock()
    context.identity.cognito_identity_id = None
    context.identity.cognito_identity_pool_id = None
    context._epoch_deadline_time_in_ms = 12345678
    context.invoked_function_arn = "arn:aws:lambda:us-east-1:123456789:function:test"
    context.tenant_id = None

    asyncio.run(stub.async_handler(event, context))

    # Verify publish was called with correct channel and message
    mock_publish.assert_called_once()
    call_args = mock_publish.call_args[0]
    channel = call_args[1]
    message = call_args[2]

    assert channel == "/stelvio/test_app/dev/in"
    assert message["requestId"] == "req-123"
    assert message["functionName"] == "test_function"
    assert message["endpointId"] == "test_endpoint_id"
    assert message["event"] == event


@patch("stelvio.bridge.remote.stub.function_stub.wait_for_response", new_callable=AsyncMock)
@patch("stelvio.bridge.remote.stub.function_stub.publish_to_appsync", new_callable=AsyncMock)
@patch("stelvio.bridge.remote.stub.function_stub.ensure_subscribed", new_callable=AsyncMock)
@patch("stelvio.bridge.remote.stub.function_stub.get_or_create_connection", new_callable=AsyncMock)
def test_wait_for_response_error_returns_500(
    mock_get_connection,
    mock_ensure_subscribed,
    mock_publish,
    mock_wait_response,
    reset_global_state,
):
    """Should return 500 error when waiting for response fails."""
    stub = reset_global_state

    mock_ws = AsyncMock()
    mock_get_connection.return_value = (mock_ws, True)
    mock_wait_response.side_effect = Exception("Wait error")

    event = {"httpMethod": "GET"}
    context = MagicMock()
    context.aws_request_id = "req-123"
    context.identity = MagicMock()

    result = asyncio.run(stub.async_handler(event, context))

    assert result["statusCode"] == 500
    assert "Error waiting for response" in result["body"]
