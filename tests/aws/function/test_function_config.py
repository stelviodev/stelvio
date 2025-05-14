# Place these imports at the top of your test file if they aren't already there
import re
import typing
from dataclasses import fields
from types import UnionType
from typing import Union, get_args, get_origin, get_type_hints

import pytest

from stelvio.aws.function import (
    DEFAULT_ARCHITECTURE,
    DEFAULT_RUNTIME,
    FunctionConfig,
    FunctionConfigDict,
)
from stelvio.aws.layer import Layer

NoneType = type(None)


def normalize_type(type_hint: type) -> type:
    """
    Normalizes a type hint by removing 'NoneType' from its Union representation,
    if applicable. Keeps other Union members intact.

    Examples:
        Union[str, None]          -> str
        Union[str, list[str], None] -> Union[str, list[str]]
        Union[Literal["a", "b"], None] -> Literal["a", "b"]
        str                       -> str
        Union[str, int]           -> Union[str, int]
        NoneType                  -> NoneType
        Union[NoneType]           -> NoneType
    """
    origin = get_origin(type_hint)

    if origin is Union or origin is UnionType:
        args = get_args(type_hint)

        non_none_args = tuple(arg for arg in args if arg is not NoneType)

        if not non_none_args:
            return NoneType
        if len(non_none_args) == 1:
            return non_none_args[0]
        return typing.Union[non_none_args]  # noqa: UP007

    return type_hint


def test_function_config_dict_has_same_fields_as_function_config():
    """Tests that the FunctionConfigDict matches the FunctionConfig dataclass."""
    # noinspection PyTypeChecker
    dataclass_fields = {f.name: f.type for f in fields(FunctionConfig)}
    typeddict_fields = get_type_hints(FunctionConfigDict)
    assert set(dataclass_fields.keys()) == set(typeddict_fields.keys()), (
        "FunctionConfigDict and FunctionConfig dataclass have different fields."
    )

    for field_name, dataclass_type in dataclass_fields.items():
        if field_name not in typeddict_fields:
            continue

        typeddict_type = typeddict_fields[field_name]

        normalized_dataclass_type = normalize_type(dataclass_type)
        normalized_typeddict_type = normalize_type(typeddict_type)

        assert normalized_dataclass_type == normalized_typeddict_type, (
            f"Type mismatch for field '{field_name}':\n"
            f"  Dataclass (original): {dataclass_type}\n"
            f"  TypedDict (original): {typeddict_type}\n"
            f"  Dataclass (normalized): {normalized_dataclass_type}\n"
            f"  TypedDict (normalized): {normalized_typeddict_type}\n"
            f"  Comparison Failed: {normalized_dataclass_type} != {normalized_typeddict_type}"
        )


@pytest.mark.parametrize(
    ("handler", "match"),
    [
        (
            "missing_dot",
            "Handler must contain a dot separator between file path and function name",
        ),
        ("file.", "Both file path and function name must be non-empty"),
        ("file..", "Both file path and function name must be non-empty"),
        (".function", "Both file path and function name must be non-empty"),
        ("file..function", "File path part should not contain dots"),
        ("two::doublecolon::separators", "Handler can only contain one :: separator"),
        ("one.two::file.function", "Folder path should not contain dots"),
    ],
)
def test_function_config_invalid_handler_format(handler, match):
    with pytest.raises(ValueError, match=match):
        FunctionConfig(handler=handler)


def test_function_config_folder_handler_conflict():
    with pytest.raises(ValueError, match="Cannot specify both 'folder' and use '::' in handler"):
        FunctionConfig(handler="folder::file.function", folder="another_folder")


def test_function_config_invalid_folder_path():
    with pytest.raises(ValueError, match="Folder path should not contain dots"):
        FunctionConfig(handler="file.function", folder="path.with.dots")


@pytest.mark.parametrize(
    ("handler", "folder", "expected_folder_path"),
    [
        ("file.function", None, None),
        ("file.function", "my_folder", "my_folder"),
        ("file.function", "my_folder/subfolder", "my_folder/subfolder"),
        ("my_folder::file.function", None, "my_folder"),
        ("my_folder/subfolder::file.function", None, "my_folder/subfolder"),
        ("path/to/folder::file.function", None, "path/to/folder"),
    ],
)
def test_function_config_folder_path(handler, folder, expected_folder_path):
    """Tests that the folder_path property returns the correct value."""
    if folder and "::" in handler:
        pytest.skip("This combination would raise a validation error")

    config = FunctionConfig(handler=handler, folder=folder)
    assert config.folder_path == expected_folder_path, (
        f"folder_path incorrect for handler='{handler}', folder='{folder}'. "
        f"Expected '{expected_folder_path}', got '{config.folder_path}'"
    )


@pytest.mark.parametrize(
    ("handler", "folder"),
    [
        ("file.function", None),
        ("folder::file.function", None),
        ("folder/subfolder::file.function", None),
        ("file.function", "my_folder"),
        ("file.function", "my_folder/and_subfolder"),
        ("subfolder/file.function", "my_folder"),
        ("sub_subfolder/file.function", "my_folder/subfolder"),
    ],
)
def test_function_config_valid_config(handler, folder):
    FunctionConfig(handler=handler, folder=folder)


@pytest.mark.parametrize(
    ("handler", "expected_file_path"),
    [
        ("file.function", "file"),
        ("folder::file.function", "file"),
        ("path/to/file.function", "path/to/file"),
    ],
)
def test_function_config_handler_file_path(handler, expected_file_path):
    config = FunctionConfig(handler=handler)
    assert config.handler_file_path == expected_file_path


@pytest.mark.parametrize(
    ("handler", "expected_function_name"),
    [("file.function", "function"), ("folder::file.function", "function")],
)
def test_function_config_handler_function_name(handler, expected_function_name):
    config = FunctionConfig(handler=handler)
    assert config.handler_function_name == expected_function_name


@pytest.mark.parametrize(
    ("handler", "expected_handler_format"),
    [
        ("file.function", "file.function"),
        ("folder/file.function", "file.function"),
        ("folder::file.function", "file.function"),
        ("folder::subfolder/file.function", "subfolder/file.function"),
    ],
)
def test_function_config_handler_format(handler, expected_handler_format):
    config = FunctionConfig(handler=handler)
    assert config.handler_format == expected_handler_format


def test_function_config_default_values():
    config = FunctionConfig(handler="file.function")
    assert config.memory is None
    assert config.timeout is None


def test_function_config_immutability():
    config = FunctionConfig(handler="file.function")
    with pytest.raises(AttributeError):
        # noinspection PyDataclass
        config.handler = "another.function"


def create_layer(name=None, runtime=None, arch=None):
    return Layer(
        name=name or "mock-layer-1",
        requirements=["dummy-req"],
        runtime=runtime or DEFAULT_RUNTIME,
        architecture=arch or DEFAULT_ARCHITECTURE,
    )


@pytest.mark.parametrize(
    ("test_id", "layers_input_generator", "error_type", "error_match", "opts"),  # Use tuple
    [
        (
            "invalid_type_in_list",
            lambda: [create_layer(), "not-a-layer"],
            TypeError,
            "Item at index 1 in 'layers' list is not a Layer instance. Got str.",
            None,
        ),
        (
            "not_a_list",
            lambda: create_layer(),
            TypeError,
            "Expected 'layers' to be a list of Layer objects, but got Layer.",
            None,
        ),
        (
            "duplicate_names",
            # Use one layer twice
            lambda: [create_layer()] * 2,
            ValueError,
            "Duplicate layer names found: mock-layer-1. "
            "Layer names must be unique for a function.",
            None,
        ),
        (
            "too_many_layers",
            # Generate 6 layers
            lambda: [Layer(name=f"l{i}", requirements=["req"]) for i in range(6)],
            ValueError,
            "A function cannot have more than 5 layers. Found 6.",
            None,
        ),
        (
            "incompatible_runtime",
            lambda: [create_layer(name="mock-layer-py313", runtime="python3.13")],
            ValueError,
            f"Function runtime '{DEFAULT_RUNTIME}' is incompatible with "
            f"Layer 'mock-layer-py313' runtime 'python3.13'.",
            None,
        ),
        (
            "incompatible_architecture",
            lambda: [create_layer(name="mock-layer-arm", arch="arm64")],
            ValueError,
            f"Function architecture '{DEFAULT_ARCHITECTURE}' is incompatible with "
            f"Layer 'mock-layer-arm' architecture 'arm64'.",
            None,
        ),
        (
            "incompatible_architecture_explicit_func",
            lambda: [create_layer()],
            ValueError,
            "Function architecture 'arm64' is incompatible with "
            "Layer 'mock-layer-1' architecture 'x86_64'.",
            {"architecture": "arm64"},
        ),
        (
            "incompatible_runtime_explicit_func",
            lambda: [create_layer()],
            ValueError,
            "Function runtime 'python3.13' is incompatible with "
            "Layer 'mock-layer-1' runtime 'python3.12'.",
            {"runtime": "python3.13"},
        ),
    ],
    ids=lambda x: x if isinstance(x, str) else "",
)
def test_function_layer_validation(test_id, layers_input_generator, error_type, error_match, opts):
    with pytest.raises(error_type, match=error_match):
        FunctionConfig(
            handler="functions/simple.handler", layers=layers_input_generator(), **opts or {}
        )


@pytest.mark.parametrize(
    ("opts", "error_type", "error_match"),
    [
        (
            {"requirements": [1, True]},
            TypeError,
            "If 'requirements' is a list, all its elements must be strings.",
        ),
        (
            {"requirements": {}},
            TypeError,
            re.escape("'requirements' must be a string (path), list of strings, False, or None."),
        ),
        (
            {"requirements": True},
            ValueError,
            re.escape(
                "If 'requirements' is a boolean, it must be False (to disable). "
                "True is not allowed."
            ),
        ),
    ],
    ids=[
        "list_not_strings",
        "not_list_or_str_or_false_or_none",
        "true_not_allowed_if_bool",
    ],
)
def test_function_config_raises_when_requirements___(opts, error_type, error_match):
    # Act & Assert
    with pytest.raises(error_type, match=error_match):
        FunctionConfig(handler="functions/simple.handler", **opts)
