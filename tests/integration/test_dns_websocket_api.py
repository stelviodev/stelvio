import asyncio

import websockets
from pytest import mark

from stelvio.aws.api_gateway import WebsocketApi
from stelvio.aws.dns import Route53Dns

from .assert_helpers import assert_http_api_mapping, assert_websocket_api
from .export_helpers import export_websocket_api

pytestmark = mark.integration_dns


def test_websocket_api_custom_domain_mapping_and_connection(
    stelvio_env, project_dir, dns_domain, dns_zone_id
):
    dns = Route53Dns(zone_id=dns_zone_id)
    subdomain = f"websocket-api-{stelvio_env.run_id}.{dns_domain}"

    def infra():
        api = WebsocketApi("customwebsocket", domain_name=subdomain)
        api.route("$connect", "handlers/echo.main")
        export_websocket_api(api)

    outputs = stelvio_env.deploy(infra, dns=dns)

    assert outputs["websocket_api_customwebsocket_url"] == f"wss://{subdomain}"
    assert_websocket_api(
        outputs["websocket_api_customwebsocket_id"], expected_route_keys={"$connect"}
    )
    assert_http_api_mapping(
        subdomain,
        expected_api_id=outputs["websocket_api_customwebsocket_id"],
        expected_mapping_key=None,
    )

    async def connect() -> None:
        async with websockets.connect(outputs["websocket_api_customwebsocket_url"]):
            pass

    asyncio.run(connect())
