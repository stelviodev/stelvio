import asyncio

import websockets
from pytest import mark

from stelvio.aws.api_gateway import WebsocketApi

from .export_helpers import export_websocket_api

pytestmark = mark.integration


def test_websocket_api_connect(stelvio_env, project_dir):
    def infra():
        api = WebsocketApi("chat")
        api.route("$connect", "handlers/echo.main")
        export_websocket_api(api)

    outputs = stelvio_env.deploy(infra)
    url = outputs["websocket_api_chat_url"]
    assert url.startswith("wss://")

    async def connect() -> None:
        async with websockets.connect(url):
            pass

    asyncio.run(connect())
