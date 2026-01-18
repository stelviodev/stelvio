from typing import ClassVar, final

from stelvio.component import BridgeableMixin


@final
class WebsocketHandlers:
    _handlers: ClassVar[list[BridgeableMixin]] = []

    @classmethod
    def register(cls, handler: BridgeableMixin) -> None:
        cls._handlers.append(handler)

    @classmethod
    def all(cls) -> list[BridgeableMixin]:
        return cls._handlers
