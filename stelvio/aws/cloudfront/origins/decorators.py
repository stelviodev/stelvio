from __future__ import annotations

from stelvio.aws.cloudfront.origins.registry import CloudfrontAdapterRegistry


def _register_adapter(adapter_cls: type, component_cls: type) -> None:
    adapter_cls.component_class = component_cls
    CloudfrontAdapterRegistry.add_adapter(adapter_cls)


def register_adapter(component_cls: type) -> callable:
    def wrapper(adapter_cls: type) -> type:
        _register_adapter(adapter_cls, component_cls)
        return adapter_cls

    return wrapper
