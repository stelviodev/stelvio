import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

from stelvio.bridge.local.dtos import BridgeInvocationResult
from stelvio.bridge.local.listener import (
    connect_to_appsync,
    log_invocation,
    publish,
    publish_to_channel,
    subscribe_to_channel,
)


@patch("stelvio.bridge.local.listener.websockets.connect", new_callable=AsyncMock)
@patch("stelvio.bridge.local.listener.base64.b64encode")
@patch("stelvio.bridge.local.listener.json.dumps")
def test_connect_to_appsync(mock_json_dumps, mock_b64encode, mock_connect):
    # Mock config
    config = {
        "http_endpoint": "https://example.com",
        "api_key": "test_key",
        "realtime_endpoint": "realtime.example.com",
    }

    # Mock auth header encoding
    mock_json_dumps.side_effect = [
        '{"host":"https://example.com","x-api-key":"test_key"}',
        '{"type": "connection_init"}',
    ]
    mock_encoded = MagicMock()
    mock_encoded.decode.return_value = "encoded_auth"
    mock_b64encode.return_value = mock_encoded

    # Mock websocket
    mock_ws = AsyncMock()
    mock_connect.return_value = mock_ws
    mock_ws.recv = AsyncMock(return_value='{"type":"connection_ack"}')

    # Call function
    result = asyncio.run(connect_to_appsync(config))

    # Assertions
    mock_connect.assert_called_once_with(
        "wss://realtime.example.com/event/realtime",
        subprotocols=["aws-appsync-event-ws", "header-encoded_auth"],
    )
    mock_ws.send.assert_called_once_with('{"type": "connection_init"}')
    mock_ws.recv.assert_called_once()
    assert result == mock_ws


def test_subscribe_to_channel():
    mock_ws = AsyncMock()
    channel = "test_channel"
    api_key = "test_key"

    # Call function
    asyncio.run(subscribe_to_channel(mock_ws, channel, api_key))

    # Assertions
    expected_message = {
        "type": "subscribe",
        "id": "request-sub",
        "channel": channel,
        "authorization": {"x-api-key": api_key},
    }
    import json

    mock_ws.send.assert_called_once_with(json.dumps(expected_message))
    mock_ws.recv.assert_called_once()


@patch("uuid.uuid4", return_value="test-uuid")
def test_publish_to_channel(mock_uuid):
    mock_ws = AsyncMock()
    channel = "test_channel"
    data = {"key": "value"}
    api_key = "test_key"

    # Call function
    asyncio.run(publish_to_channel(mock_ws, channel, data, api_key))

    # Assertions
    expected_message = {
        "id": "test-uuid",
        "type": "publish",
        "channel": channel,
        "events": [json.dumps(data)],
        "authorization": {"x-api-key": api_key},
    }
    mock_ws.send.assert_called_once_with(json.dumps(expected_message))


@patch("stelvio.bridge.local.listener.publish_to_channel")
@patch("stelvio.bridge.local.listener.json.loads")
def test_publish_success(mock_json_loads, mock_publish_to_channel):
    # Mock result
    result = BridgeInvocationResult(
        success_result={"statusCode": 200, "body": "OK"},
        error_result=None,
        request_path="/test",
        request_method="GET",
        process_time_local=100,
        status_code=200,
    )

    mock_ws = AsyncMock()
    api_key = "test_key"
    message = {"event": json.dumps({"invoke_id": "test_id"})}
    app_name = "test_app"
    stage = "dev"

    mock_json_loads.return_value = {"invoke_id": "test_id"}

    # Call function
    asyncio.run(publish(result, mock_ws, api_key, message, app_name, stage))

    # Assertions
    expected_response = {
        "requestId": "test_id",
        "success": True,
        "result": {"statusCode": 200, "body": "OK"},
    }
    mock_publish_to_channel.assert_called_once_with(
        mock_ws, "/stelvio/test_app/dev/out", expected_response, api_key
    )


@patch("stelvio.bridge.local.listener.publish_to_channel")
@patch("stelvio.bridge.local.listener.json.loads")
@patch("stelvio.bridge.local.listener.traceback.format_exception")
def test_publish_error(mock_format_exception, mock_json_loads, mock_publish_to_channel):
    # Mock result
    error = ValueError("test error")
    result = BridgeInvocationResult(
        success_result=None,
        error_result=error,
        request_path="/test",
        request_method="GET",
        process_time_local=100,
        status_code=-1,
    )

    mock_ws = AsyncMock()
    api_key = "test_key"
    message = {"event": json.dumps({"invoke_id": "test_id"})}
    app_name = "test_app"
    stage = "dev"

    mock_json_loads.return_value = {"invoke_id": "test_id"}
    mock_format_exception.return_value = ["trace1", "trace2"]

    # Call function
    asyncio.run(publish(result, mock_ws, api_key, message, app_name, stage))

    # Assertions
    expected_response = {
        "requestId": "test_id",
        "success": False,
        "error": "test error",
        "errorType": "ValueError",
        "stackTrace": ["trace1", "trace2"],
    }
    mock_publish_to_channel.assert_called_once_with(
        mock_ws, "/stelvio/test_app/dev/out", expected_response, api_key
    )


@patch("stelvio.bridge.local.listener.Console")
@patch("stelvio.bridge.local.listener.datetime.datetime")
@patch("stelvio.bridge.local.listener.asyncio.get_event_loop")
@patch("stelvio.bridge.local.listener.NOT_A_TEAPOT", 418)
def test_log_invocation_success(mock_get_event_loop, mock_datetime_class, mock_console_class):
    mock_console = MagicMock()
    mock_console_class.return_value = mock_console

    mock_now = MagicMock()
    mock_now.strftime.return_value = "12:00:00"
    mock_now.time.return_value = 3600.0
    mock_datetime_class.now.return_value = mock_now

    mock_loop = MagicMock()
    mock_loop.time.return_value = 3600.0
    mock_get_event_loop.return_value = mock_loop

    result = BridgeInvocationResult(
        success_result={"statusCode": 200},
        error_result=None,
        request_path="/test",
        request_method="GET",
        process_time_local=123.45,
        status_code=200,
        handler_name="test_handler",
    )

    log_invocation(result)

    # Check print calls
    assert mock_console.print.call_count == 1
    call_args = mock_console.print.call_args[0][0]
    assert "[bold]GET    [/bold]" in call_args
    assert "/test" in call_args
    assert "[bold green]200[/bold green]" in call_args
    assert "123.45ms" in call_args


@patch("stelvio.bridge.local.listener.Console")
@patch("stelvio.bridge.local.listener.datetime")
@patch("stelvio.bridge.local.listener.asyncio.get_event_loop")
@patch("stelvio.bridge.local.listener.traceback.format_exception")
def test_log_invocation_error(
    mock_format_exception, mock_get_event_loop, mock_datetime, mock_console_class
):
    mock_console = MagicMock()
    mock_console_class.return_value = mock_console

    mock_datetime.datetime.now.return_value.strftime.return_value = "12:00:00"
    mock_datetime.datetime.now.return_value.time.return_value = 3600.0

    mock_loop = MagicMock()
    mock_loop.time.return_value = 3600.0
    mock_get_event_loop.return_value = mock_loop

    error = RuntimeError("test error")
    result = BridgeInvocationResult(
        success_result=None,
        error_result=error,
        request_path="/test",
        request_method="POST",
        process_time_local=67.89,
        status_code=-1,
    )

    mock_format_exception.return_value = ["line1", "line2"]

    log_invocation(result)

    # Check that traceback and log are printed
    calls = [call[0][0] for call in mock_console.print.call_args_list]
    assert any("line1" in call for call in calls)
    assert any("line2" in call for call in calls)
    assert any(
        "[bold]POST   [/bold]" in call and "[bold red]ERR[/bold red]" in call for call in calls
    )


@patch("stelvio.bridge.local.listener.Console")
@patch("stelvio.bridge.local.listener.datetime")
@patch("stelvio.bridge.local.listener.asyncio.get_event_loop")
def test_log_invocation_teapot(mock_get_event_loop, mock_datetime, mock_console_class):
    mock_console = MagicMock()
    mock_console_class.return_value = mock_console

    mock_datetime.datetime.now.return_value.strftime.return_value = "12:00:00"
    mock_datetime.datetime.now.return_value.time.return_value = 3600.0

    mock_loop = MagicMock()
    mock_loop.time.return_value = 3600.0
    mock_get_event_loop.return_value = mock_loop

    result = BridgeInvocationResult(
        success_result=None,
        error_result=None,
        request_path="/test",
        request_method="GET",
        process_time_local=100.0,
        status_code=418,
        handler_name="test_handler",
    )

    log_invocation(result)

    # Check print calls
    assert mock_console.print.call_count == 1
    call_args = mock_console.print.call_args[0][0]
    assert "[bold]GET    [/bold]" in call_args
    assert "❌🫖" in call_args
    assert "100.00ms" in call_args
