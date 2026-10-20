"""The dev bridge rejects expired and network-dependent requests before handler dispatch."""

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from stelvio.bridge.local import listener
from stelvio.bridge.local.dtos import BridgeInvocationResult
from stelvio.tunnel.session import VpcUnavailableError


def _event(*, endpoint="endpoint-vpc", deadline=0):
    return {
        "type": "data",
        "event": json.dumps(
            {
                "invoke_id": "invoke-1",
                "endpointId": endpoint,
                "event": {"path": "/", "httpMethod": "GET"},
                "context": {"epoch_deadline_time_in_ms": deadline},
            }
        ),
    }


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _result_from_publish(publish_mock):
    return publish_mock.await_args.args[0]


def test_bridge_server_passes_session_admission_to_main(monkeypatch):
    calls = []
    admission = Mock(return_value=None)

    async def fake_main(**options):
        calls.append(options)

    monkeypatch.setattr(listener, "main", fake_main)

    listener.run_bridge_server("us-east-1", "default", "app", "dev", admission=admission)

    assert calls == [
        {
            "region": "us-east-1",
            "profile": "default",
            "app_name": "app",
            "env": "dev",
            "admission": admission,
        }
    ]


def test_unready_vpc_rejects_before_handler_dispatch(monkeypatch):
    dispatched = []
    handler = SimpleNamespace(
        handle_bridge_event=AsyncMock(side_effect=lambda _data: dispatched.append(True))
    )
    admission_error = VpcUnavailableError("VPC access unavailable for this invocation: vpc-1")

    def admission(endpoint):
        return admission_error if endpoint == "endpoint-vpc" else None

    publish = AsyncMock()
    monkeypatch.setattr(listener.WebsocketHandlers, "all", lambda: [handler])
    monkeypatch.setattr(listener, "publish", publish)
    monkeypatch.setattr(listener, "log_invocation", lambda _result: None)

    _run(listener._handle_data_message(_event(), None, "key", "app", "dev", admission))

    assert dispatched == []
    assert publish.await_count == 1
    result = _result_from_publish(publish)
    assert result.error_result is admission_error
    assert result.success_result is None


def test_expired_invocation_is_rejected_before_admission_or_handler(monkeypatch):
    admission = Mock(return_value=None)
    handler = SimpleNamespace(handle_bridge_event=AsyncMock())
    publish = AsyncMock()
    monkeypatch.setattr(listener.WebsocketHandlers, "all", lambda: [handler])
    monkeypatch.setattr(listener, "publish", publish)
    monkeypatch.setattr(listener, "log_invocation", lambda _result: None)
    expired = int(time.time() * 1000) - 1

    _run(
        listener._handle_data_message(
            _event(deadline=expired), None, "key", "app", "dev", admission
        )
    )

    admission.assert_not_called()
    handler.handle_bridge_event.assert_not_awaited()
    assert isinstance(_result_from_publish(publish).error_result, TimeoutError)


def test_non_vpc_invocation_keeps_ordinary_dispatch(monkeypatch):
    admission = Mock(return_value=None)
    result = BridgeInvocationResult(
        success_result={"statusCode": 200},
        error_result=None,
        request_path="/",
        request_method="GET",
        process_time_local=1.0,
        status_code=200,
    )
    handler = SimpleNamespace(handle_bridge_event=AsyncMock(return_value=result))
    publish = AsyncMock()
    monkeypatch.setattr(listener.WebsocketHandlers, "all", lambda: [handler])
    monkeypatch.setattr(listener, "publish", publish)
    monkeypatch.setattr(listener, "log_invocation", lambda _result: None)

    _run(
        listener._handle_data_message(
            _event(endpoint="endpoint-plain"), None, "key", "app", "dev", admission
        )
    )

    admission.assert_called_once_with("endpoint-plain")
    handler.handle_bridge_event.assert_awaited_once()
    assert publish.await_args.args[0] is result
