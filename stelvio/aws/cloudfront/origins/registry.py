import importlib
import pkgutil

from stelvio.component import Component


class CloudfrontBridgeRegistry:
    classes = []  # noqa: RUF012
    _initialized = False

    @classmethod
    def _ensure_bridges_loaded(cls) -> None:
        """Lazy load all bridge modules to avoid circular imports."""
        if cls._initialized:
            return

        # Import here to avoid circular import during module loading
        import stelvio.aws.cloudfront.origins

        # Find all modules in stelvio.aws.cloudfront.origins, register their bridges
        # (exclude registry.py, base.py, decorators.py):
        for _, module_name, _ in pkgutil.iter_modules(stelvio.aws.cloudfront.origins.__path__):
            if module_name not in ("registry", "base", "decorators"):
                importlib.import_module(f"stelvio.aws.cloudfront.origins.{module_name}")

        cls._initialized = True

    @classmethod
    def get_bridge_for_component(cls, component: Component) -> any:
        cls._ensure_bridges_loaded()
        for bridge_cls in cls.classes:
            if bridge_cls.match(component):
                return bridge_cls
        raise ValueError(f"No bridge found for component: {component}")
