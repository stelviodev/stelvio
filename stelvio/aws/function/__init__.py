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

    raise TypeError(f"Invalid handler type: {type(handler).__name__}")


__all__ = [
    "Function",
    "FunctionConfig",
    "FunctionConfigDict",
    "FunctionCustomizationDict",
    "FunctionResources",
    "FunctionUrlConfig",
    "FunctionUrlConfigDict",
    "parse_handler_config",
]
