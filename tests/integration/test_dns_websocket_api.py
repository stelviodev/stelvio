import asyncio
import time

import websockets
from pytest import mark

from stelvio.aws.api_gateway import ApiDomain, WebsocketApi
from stelvio.aws.dns import Route53Dns

from .assert_helpers import assert_http_api_mapping
from .assert_websocket_api import assert_websocket_api
from .export_helpers import export_http_api_domain, export_websocket_api

pytestmark = mark.integration_dns

# Custom-domain DNS/ACM can lag after deploy before wss:// accepts.
_WEBSOCKET_DNS_CONNECT_TIMEOUT = 180


def _connect(url: str) -> None:
    deadline = time.monotonic() + _WEBSOCKET_DNS_CONNECT_TIMEOUT
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:

            async def run() -> None:
                async with websockets.connect(url, open_timeout=10):
                    pass

            asyncio.run(run())
        except Exception as exc:  # retry transient DNS/TLS/connect failures
            last_error = exc
            time.sleep(2)
        else:
            return
    assert last_error is not None
    raise last_error


def test_websocket_api_custom_domain_mapping_and_connection(
    stelvio_env, project_dir, dns_domain, dns_zone_id
):
    dns = Route53Dns(zone_id=dns_zone_id)
    subdomain = f"websocket-api-{stelvio_env.run_id}.{dns_domain}"

    def infra():
        api = WebsocketApi("customwebsocket", domain_name=subdomain)
        api.route("$connect", "handlers/websocket_connect.main")
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

    _connect(outputs["websocket_api_customwebsocket_url"])


def test_websocket_api_shared_domain_mapping_key_and_connection(
    stelvio_env, project_dir, dns_domain, dns_zone_id
):
    dns = Route53Dns(zone_id=dns_zone_id)
    subdomain = f"websocket-shared-{stelvio_env.run_id}.{dns_domain}"

    def infra():
        domain = ApiDomain("shared-ws-domain", domain_name=subdomain)
        api = WebsocketApi(
            "sharedwebsocket",
            domain=domain,
            api_mapping_key="v1",
        )
        api.route("$connect", "handlers/websocket_connect.main")
        export_http_api_domain(domain)
        export_websocket_api(api)

    outputs = stelvio_env.deploy(infra, dns=dns)

    assert outputs["websocket_api_sharedwebsocket_url"] == f"wss://{subdomain}/v1"
    assert_websocket_api(
        outputs["websocket_api_sharedwebsocket_id"],
        expected_route_keys={"$connect"},
    )
    assert_http_api_mapping(
        subdomain,
        expected_api_id=outputs["websocket_api_sharedwebsocket_id"],
        expected_mapping_key="v1",
    )
    assert outputs["http_api_domain_shared-ws-domain_domain_name"] == subdomain
    assert outputs["http_api_domain_shared-ws-domain_target_domain_name"].endswith(
        f".execute-api.{stelvio_env.aws_region}.amazonaws.com"
    )

    _connect(outputs["websocket_api_sharedwebsocket_url"])
