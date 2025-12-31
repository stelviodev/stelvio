from typing import ClassVar, final

from stelvio.component import BridgeableComponent


@final
class WebsocketHandlers:
    _handlers: ClassVar[list[BridgeableComponent]] = []

    @classmethod
    def register(cls, handler: BridgeableComponent) -> None:
        cls._handlers.append(handler)

    @classmethod
    def all(cls) -> list[BridgeableComponent]:
        return cls._handlers
