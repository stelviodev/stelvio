from typing import Any

from stelvio.aws.function import Function, FunctionConfig
from stelvio.component import ComponentRegistry


def normalize_handler_input_to_function_config(
    handler_input: str | dict[str, Any] | FunctionConfig,
) -> FunctionConfig:
    if isinstance(handler_input, str):
        return FunctionConfig(handler=handler_input)
    if isinstance(handler_input, dict):
        return FunctionConfig(**handler_input)
    if isinstance(handler_input, FunctionConfig):
        return handler_input
    raise TypeError(f"Unsupported handler input type: {type(handler_input)}")


def verify_stelvio_function_for_subscription(
    source_name: str,
    subscription_name: str,
    expected_link_name: str,
    expected_handler_input: str | dict[str, Any] | FunctionConfig | None = None,
    expected_link_count: int | None = None,
) -> Function:
    functions = ComponentRegistry._instances.get(Function, [])
    function_map = {function.name: function for function in functions}

    expected_fn_name = f"{source_name}-{subscription_name}"
    assert expected_fn_name in function_map, (
        f"Stelvio Function '{expected_fn_name}' not found in ComponentRegistry. "
        f"Available functions: {list(function_map.keys())}"
    )

    created_function = function_map[expected_fn_name]
    matching_links = [
        link
        for link in created_function.config.links
        if hasattr(link, "name") and link.name == expected_link_name
    ]
    link_names = [getattr(link, "name", str(link)) for link in created_function.config.links]

    if expected_link_count is None:
        assert len(matching_links) >= 1, (
            f"Function '{expected_fn_name}' missing link '{expected_link_name}'. "
            f"Links: {link_names}"
        )
    else:
        assert len(matching_links) == expected_link_count, (
            f"Function '{expected_fn_name}' should have exactly {expected_link_count} link(s) "
            f"named '{expected_link_name}'. Links: {link_names}"
        )

    if expected_handler_input is not None:
        expected_config = normalize_handler_input_to_function_config(expected_handler_input)
        assert created_function.config.handler == expected_config.handler, (
            f"Function handler mismatch: expected {expected_config.handler}, "
            f"got {created_function.config.handler}"
        )
        if expected_config.memory is not None:
            assert created_function.config.memory == expected_config.memory, (
                f"Function memory mismatch: expected {expected_config.memory}, "
                f"got {created_function.config.memory}"
            )
        if expected_config.timeout is not None:
            assert created_function.config.timeout == expected_config.timeout, (
                f"Function timeout mismatch: expected {expected_config.timeout}, "
                f"got {created_function.config.timeout}"
            )

    return created_function
