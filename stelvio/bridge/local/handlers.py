from typing import ClassVar, final

from stelvio.component import BridgeableComponent


@final
class WebsocketHandlers:
    _handlers: ClassVar[list[BridgeableComponent]] = []

    @classmethod
    def register(cls, handler: BridgeableComponent) -> None:
        cls._handlers.append(handler)

    # @classmethod
    # async def handle_message(cls, data: any, client: "WebsocketClient") -> None:
    #     for handler in cls._handlers:
    #         await handler(data, client)

    @classmethod
    def all(cls) -> list[BridgeableComponent]:
        return cls._handlers
