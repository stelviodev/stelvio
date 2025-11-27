import importlib
import pkgutil

from stelvio.aws.cloudfront.origins.base import ComponentCloudfrontAdapter
from stelvio.component import Component


class CloudfrontAdapterRegistry:
    classes: list[type[ComponentCloudfrontAdapter]] = []  # noqa: RUF012
    _initialized = False

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
        for adapter_cls in cls.classes:
            if adapter_cls.match(component):
                return adapter_cls
        raise ValueError(f"No adapter found for component: {component}")
