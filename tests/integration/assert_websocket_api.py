"""Boto3 read-back assertions for WebsocketApi deployed resources."""

from __future__ import annotations

import asyncio
import time

import websockets
from websockets.exceptions import InvalidStatus

from .assert_helpers import _boto3_session

# DNS/TLS and custom-domain ELB 400s settle over a couple of minutes; pause
# between websocket_connect retries when retry_timeout is set.
_WEBSOCKET_CONNECT_RETRY_INTERVAL = 2


def websocket_execute_api_url(scheme: str, *, api_id: str, region: str, stage: str) -> str:
    """Same shape as WebsocketApi._execute_api_url (scheme + id + region + stage)."""
    return f"{scheme}://{api_id}.execute-api.{region}.amazonaws.com/{stage}"


def websocket_connect(url: str, *, retry_timeout: float | None = None) -> None:
    """Open a WebSocket connection and close it.

    When ``retry_timeout`` is set, retry ``OSError`` (DNS/TLS) and
    ``InvalidStatus`` (API Gateway 400 while a custom-domain ELB settles).
    Other errors fail immediately.
    """

    async def run() -> None:
        async with websockets.connect(url, open_timeout=10):
            pass

    if retry_timeout is None:
        asyncio.run(run())
        return

    deadline = time.monotonic() + retry_timeout
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        try:
            asyncio.run(run())
        except (OSError, InvalidStatus) as exc:
            last_error = exc
            time.sleep(_WEBSOCKET_CONNECT_RETRY_INTERVAL)
        else:
            return
    assert last_error is not None
    raise last_error


def assert_websocket_api(
    api_id: str,
    *,
    expected_route_keys: set[str],
    expected_integration_count: int | None = None,
    route_selection_expression: str = "$request.body.action",
    expected_stage_name: str = "$default",
) -> None:
    """Assert a WebSocket API's protocol, routes, and Lambda integrations.

    Integration count is not assumed equal to route count: shared handlers
    intentionally create one integration for many routes.
    """
    client = _boto3_session().client("apigatewayv2")
    api = client.get_api(ApiId=api_id)
    assert api["ProtocolType"] == "WEBSOCKET"
    assert api["RouteSelectionExpression"] == route_selection_expression

    stage = client.get_stage(ApiId=api_id, StageName=expected_stage_name)
    assert stage["StageName"] == expected_stage_name

    routes = client.get_routes(ApiId=api_id)["Items"]
    actual_route_keys = {route["RouteKey"] for route in routes}
    assert actual_route_keys == expected_route_keys, (
        f"Expected WebSocket route keys {expected_route_keys}, got {actual_route_keys}"
    )

    integrations = client.get_integrations(ApiId=api_id)["Items"]
    assert integrations, f"Expected integrations on WebSocket API {api_id}"
    assert {integration["IntegrationType"] for integration in integrations} == {"AWS_PROXY"}
    if expected_integration_count is not None:
        assert len(integrations) == expected_integration_count, (
            f"Expected {expected_integration_count} integrations, got {len(integrations)}"
        )

    integration_ids = {integration["IntegrationId"] for integration in integrations}
    for route in routes:
        target_id = route.get("Target", "").removeprefix("integrations/")
        assert target_id in integration_ids, route
