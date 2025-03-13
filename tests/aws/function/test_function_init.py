from typing import Any

import pytest

from stelvio.aws.function import Function, FunctionConfig


@pytest.mark.parametrize(
    "name, config, opts, expected_error",
    [
        (
            "my_function",
            None,
            {},
            "Missing function handler: must provide either a complete configuration "
            "via 'config' parameter or at least the 'handler' option",
        ),
        (
            "my_function",
            {"handler": "functions/handler.main"},
            {"handler": "functions/handler.main"},
            "Invalid configuration: cannot combine 'config' parameter with additional "
            "options - provide all settings either in 'config' or as separate options",
        ),
        (
            "my_function",
            {"handler": "functions/handler.main"},
            {"memory": 256},
            "Invalid configuration: cannot combine 'config' parameter with additional "
            "options - provide all settings either in 'config' or as separate options",
        ),
        (
            "my_function",
            {"folder": "functions/handler"},
            {"memory": 256},
            "Invalid configuration: cannot combine 'config' parameter with additional "
            "options - provide all settings either in 'config' or as separate options",
        ),
    ],
)
def test_invalid_config(name: str, config: Any, opts: dict, expected_error: str):
    """Test that Function raises ValueError with invalid configurations."""
    with pytest.raises(ValueError, match=expected_error):
        Function(name, config=config, **opts)


@pytest.mark.parametrize(
    "name, config, opts, wrong_type",
    [
        ("my_function", 123, {}, "int"),
        ("my_function", "hello", {}, "str"),
        ("my_function", [4, 5, 6], {}, "list"),
    ],
)
def test_invalid_config_type_error(name: str, config: Any, opts: dict, wrong_type: str):
    """Test that Function raises ValueError with invalid configurations."""
    with pytest.raises(
        TypeError,
        match=f"Invalid config type: expected FunctionConfig or dict, got {wrong_type}",
    ):
        Function(name, config=config, **opts)


def test_function_config_property():
    """Test that the config property returns the correct FunctionConfig object."""
    config = {"handler": "functions/handler.main", "memory": 128}
    function = Function("my_function", config=config)
    assert isinstance(function.config, FunctionConfig)
    assert function.config.handler == "functions/handler.main"
    assert function.config.memory == 128


@pytest.mark.parametrize(
    "name, config, opts",
    [
        ("my_function", {"handler": "folder::handler.main"}, {}),
        ("my_function", None, {"handler": "folder::handler.main"}),
        ("my_function", FunctionConfig(handler="folder::handler.main"), {}),
    ],
)
def test_valid_folder_config(name: str, config: Any, opts: dict):
    """
    Test that Function initializes correctly with valid folder-based configurations.
    """
    try:
        Function(name, config=config, **opts)
    except Exception as e:
        pytest.fail(f"Function initialization failed with valid config: {e}")


@pytest.mark.parametrize(
    "name, config, opts, expected_error",
    [
        (
            "my_function",
            {"handler": "invalid.handler.format"},
            {},
            "File path part should not contain dots",
        ),
        (
            "my_function",
            {"handler": "handler."},
            {},
            "Both file path and function name must be non-empty",
        ),
        (
            "my_function",
            {"handler": ".handler"},
            {},
            "Both file path and function name must be non-empty",
        ),
    ],
)
def test_invalid_handler_format(
    name: str, config: Any, opts: dict, expected_error: str
):
    """
    Test that Function raises ValueError with invalid handler formats.
    This is already thoroughly tested in FunctionConfig tests, there we just do simple
    sanity tests.
    """
    with pytest.raises(ValueError, match=expected_error):
        Function(name, config=config, **opts)
