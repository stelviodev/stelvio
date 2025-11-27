def _register_bridge(bridge_cls: type, component_cls: type) -> None:
    # Import here to avoid circular import
    from stelvio.aws.cloudfront.origins.registry import CloudfrontBridgeRegistry

    bridge_cls.component_class = component_cls
    CloudfrontBridgeRegistry.classes.append(bridge_cls)


def register_bridge(component_cls: type) -> callable:
    def wrapper(bridge_cls: type) -> type:
        _register_bridge(bridge_cls, component_cls)
        return bridge_cls

    return wrapper
