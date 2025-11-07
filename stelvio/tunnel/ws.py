#!/usr/bin/env python3
"""
WebSocket client for stlv-tunnel

"""

import asyncio
import json
import sys
from typing import ClassVar, final

import websockets

# Force unbuffered output
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)


@final
class WebsocketHandlers:
    _handlers: ClassVar[list[callable]] = []

    @classmethod
    def register(cls, handler: callable) -> None:
        cls._handlers.append(handler)

    @classmethod
    async def handle_message(cls, data: any, client: "WebsocketClient") -> None:
        for handler in cls._handlers:
            await handler(data, client)

    @classmethod
    def all(cls) -> list[callable]:
        return cls._handlers


@final
class WebsocketClient:
    def __init__(self, url: str):
        self.url = url

    def register_handler(self, handler: callable) -> None:
        self.handlers.append(handler)

    async def connect(self) -> None:
        url = self.url
        try:
            async with websockets.connect(url) as websocket:
                self.websocket = websocket

                async for message in websocket:
                    try:
                        # Try to parse as JSON and pretty-print
                        data = json.loads(message)

                        tasks = [
                            asyncio.create_task(handler(data, self))
                            for handler in WebsocketHandlers.all()
                        ]

                        if tasks:
                            await asyncio.gather(*tasks)

                    except json.JSONDecodeError:
                        pass
        except ConnectionRefusedError:
            sys.exit(1)
        except KeyboardInterrupt:
            sys.exit(0)

    async def send_json(self, data: dict) -> None:
        """Send a JSON message to the WebSocket server."""
        return await self.websocket.send(json.dumps(data))
