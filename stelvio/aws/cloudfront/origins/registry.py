from __future__ import annotations

import importlib
import pkgutil
from typing import TYPE_CHECKING, ClassVar

from stelvio.aws.cloudfront.origins.base import ComponentCloudfrontAdapter

if TYPE_CHECKING:
    from collections.abc import Callable

    from stelvio.component import Component


class CloudfrontAdapterRegistry:
    _adapters: ClassVar[list[type[ComponentCloudfrontAdapter]]] = []
    _initialized = False

    @classmethod
    def add_adapter(cls, adapter_cls: type[ComponentCloudfrontAdapter]) -> None:
        cls._adapters.append(adapter_cls)

    @classmethod
    def all_adapters(cls) -> list[type[ComponentCloudfrontAdapter]]:
        return cls._adapters

    @classmethod
    def _ensure_adapters_loaded(cls) -> None:
        """Lazy load all adapter modules to avoid circular imports."""
        if cls._initialized:
            return

        # components import register_adapter from this module; top-level would cycle
        import stelvio.aws.cloudfront.origins.components  # noqa: PLC0415

        # Find all modules in stelvio.aws.cloudfront.origins.components, register their adapters
        for _, module_name, _ in pkgutil.iter_modules(
            stelvio.aws.cloudfront.origins.components.__path__
        ):
            importlib.import_module(f"stelvio.aws.cloudfront.origins.components.{module_name}")

        cls._initialized = True

    @classmethod
    def get_adapter_for_component(cls, component: Component) -> type[ComponentCloudfrontAdapter]:
        cls._ensure_adapters_loaded()
        for adapter_cls in cls.all_adapters():
            if adapter_cls.match(component):
                return adapter_cls
        raise ValueError(f"No adapter found for component: {component}")


def register_adapter[A: type[ComponentCloudfrontAdapter]](
    component_cls: type[Component],
) -> Callable[[A], A]:
    """Class decorator: bind an adapter to the component class it serves and register it."""

    def wrapper(adapter_cls: A) -> A:
        adapter_cls.component_class = component_cls
        CloudfrontAdapterRegistry.add_adapter(adapter_cls)
        return adapter_cls

    return wrapper
