from .config import FunctionConfig, FunctionConfigDict, FunctionUrlConfig, FunctionUrlConfigDict
from .function import Function, FunctionCustomizationDict, FunctionResources


def parse_handler_config(
    handler: str | FunctionConfig | FunctionConfigDict | None,
    opts: FunctionConfigDict,
) -> FunctionConfig:
    """Parse handler configuration from various input forms.

    Supports multiple configuration styles:
    - FunctionConfig object: returned as-is
    - dict: converted to FunctionConfig
    - str: used as handler path, combined with opts
    - None: handler must be provided in opts

    Args:
        handler: Handler specification (string path, config object, dict, or None)
        opts: Additional function configuration options

    Raises:
        ValueError: If configuration is ambiguous or incomplete
        TypeError: If handler type is invalid
    """
    if isinstance(handler, dict | FunctionConfig) and opts:
        raise ValueError(
            "Invalid configuration: cannot combine complete handler "
            "configuration with additional options"
        )

    if isinstance(handler, FunctionConfig):
        return handler

    if isinstance(handler, dict):
        return FunctionConfig(**handler)

    if isinstance(handler, str):
        if "handler" in opts:
            raise ValueError(
                "Ambiguous handler configuration: handler is specified both as positional "
                "argument and in options"
            )
        return FunctionConfig(handler=handler, **opts)

    if handler is None:
        if "handler" not in opts:
            raise ValueError(
                "Missing handler configuration: when handler argument is None, "
                "'handler' option must be provided"
            )
        return FunctionConfig(**opts)

    raise TypeError(
        f"Invalid handler type: {type(handler).__name__}; expected str, dict, FunctionConfig "
        "or None"
    )


def resolve_handler(
    handler: str | FunctionConfig | FunctionConfigDict | Function | None,
    opts: FunctionConfigDict,
) -> FunctionConfig | Function:
    """`parse_handler_config` for callers that also take a ready `Function`.

    Subscriptions keep `parse_handler_config`: they always build their own Lambda (Queue and
    DynamoTable merge a poll-permission link into its config), so a ready Function can't go in.

    Raises:
        ValueError: If a Function comes with options, which it would silently ignore
    """
    if isinstance(handler, Function):
        if opts:
            raise ValueError(
                "Cannot combine a Function handler with function options "
                f"({', '.join(sorted(opts))}); set them on the Function."
            )
        return handler
    return parse_handler_config(handler, opts)


__all__ = [
    "Function",
    "FunctionConfig",
    "FunctionConfigDict",
    "FunctionCustomizationDict",
    "FunctionResources",
    "FunctionUrlConfig",
    "FunctionUrlConfigDict",
]
