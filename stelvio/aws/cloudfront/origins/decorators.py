from __future__ import annotations


def _register_adapter(adapter_cls: type, component_cls: type) -> None:
    # Import here to avoid circular import
    from stelvio.aws.cloudfront.origins.registry import CloudfrontAdapterRegistry

    adapter_cls.component_class = component_cls
    CloudfrontAdapterRegistry.classes.append(adapter_cls)


def register_adapter(component_cls: type) -> callable:
    def wrapper(adapter_cls: type) -> type:
        _register_adapter(adapter_cls, component_cls)
        return adapter_cls

    return wrapper
