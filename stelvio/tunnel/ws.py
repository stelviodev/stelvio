#!/usr/bin/env python3
"""
WebSocket client for stlv-tunnel

"""

import asyncio
import json
import sys
from abc import ABC, abstractmethod
from typing import ClassVar, final

import websockets
from rich.console import Console

# Force unbuffered output
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

NOT_A_TEAPOT = 418


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


class TunnelLogger(ABC):
    @abstractmethod
    def log(  # noqa: PLR0913
        self,
        protocol: str,
        method: str,
        path: str,
        source_ip: str,
        status_code: int,
        duration_ms: float,
    ) -> None:
        raise NotImplementedError


@final
class WebsocketClient(TunnelLogger):
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

                        # Tunnel: Step 2: Handle incoming tunnel events and dispatch
                        # to registered handlers
                        tasks = [
                            asyncio.create_task(
                                handler(data, self, self)
                            )  # TODO: Generate separate Logging class
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

    # Tunnel: Step 3a: Send JSON messages back to the tunnel service
    async def send_json(self, data: dict) -> None:
        """Send a JSON message to the WebSocket server."""
        return await self.websocket.send(json.dumps(data))

    def log(  # noqa: PLR0913
        self,
        protocol: str,
        method: str,
        path: str,
        source_ip: str,
        status_code: int,
        duration_ms: float,
    ) -> None:
        console = Console(soft_wrap=True)

        if status_code == NOT_A_TEAPOT:
            status_code = "❌🫖"
        else:
            status_code = str(status_code)
            match status_code[0]:
                case "2":
                    status_color = "green"
                case "4":
                    status_color = "yellow"
                case "5":
                    status_color = "red"
                case _:
                    status_color = "white"
            status_code = f"[bold {status_color}]{status_code}[/bold {status_color}]"
        console.print(
            f"\n[bold]{protocol}[/bold] [bold blue]{method}[/bold blue] "
            f"[cyan]{path}[/cyan] [grey]{source_ip}[/grey] {status_code} {duration_ms}ms",
            highlight=False,
        )
