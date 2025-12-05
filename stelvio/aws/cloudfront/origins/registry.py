import importlib
import pkgutil
from typing import ClassVar

from stelvio.aws.cloudfront.origins.base import ComponentCloudfrontAdapter
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

        # Import here to avoid circular import during module loading
        import stelvio.aws.cloudfront.origins.components

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
