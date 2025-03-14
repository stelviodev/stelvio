from dataclasses import fields
from types import UnionType
from typing import Union, get_args, get_origin, get_type_hints

import pytest

from stelvio.aws.function import FunctionConfig, FunctionConfigDict


def normalize_type(type_hint):
    """Normalizes a type hint by stripping away Optional/Union."""
    origin = get_origin(type_hint)
    if origin is Union or origin is UnionType:  # Handles Optional[str] and str | None
        args = get_args(type_hint)
        # Find the non-None type in the Union
        for arg in args:
            if arg is not type(None):
                return arg
        return type(None)
    return type_hint


def test_function_config_dict_has_same_fields_as_function_config():
    """Tests that the FunctionConfigDict matches the FunctionConfig dataclass."""
    # noinspection PyTypeChecker
    dataclass_fields = {f.name: f.type for f in fields(FunctionConfig)}
    typeddict_fields = get_type_hints(FunctionConfigDict)
    # Check that all dataclass fields are in the typeddict
    assert set(dataclass_fields.keys()) == set(typeddict_fields.keys()), (
        "FunctionConfigDict and FunctionConfig dataclass have different fields."
    )

    for field_name, dataclass_type in dataclass_fields.items():
        typeddict_type = typeddict_fields[field_name]
        # We strip away optional because FunctionConfigDict has total=False so
        # all fields are optional
        normalized_dataclass_type = normalize_type(dataclass_type)

        assert normalized_dataclass_type == typeddict_type, (
            f"Type mismatch for field '{field_name}': FunctionConfig dataclass type is "
            f"{normalized_dataclass_type}, FunctionConfigDict type is {typeddict_type}"
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
        # Case 1: No folder, no :: in handler
        ("file.function", None, None),
        # Case 2: folder is specified, no :: in handler
        ("file.function", "my_folder", "my_folder"),
        ("file.function", "my_folder/subfolder", "my_folder/subfolder"),
        # Case 3: no folder, but :: in handler - should return part before ::
        ("my_folder::file.function", None, "my_folder"),
        ("my_folder/subfolder::file.function", None, "my_folder/subfolder"),
        # Case 4: Nested paths in handler with ::
        ("path/to/folder::file.function", None, "path/to/folder"),
        # Case 5: Both folder and :: in handler (validation should catch this,
        # but testing for completeness)
        # This case would actually raise an exception in __post_init__,
        # so we don't test it here
    ],
)
def test_function_config_folder_path(handler, folder, expected_folder_path):
    """Tests that the folder_path property returns the correct value."""
    # Skip test cases that would raise validation errors
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
    # Should not raise any exception
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
        # TODO:  This below might not work when creating lambda need to try
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


# TODO: Not sure about this type tests, need to check what happens if wrong type
#       provided but correct value e.g. '128' or 128.0.
# def test_function_config_memory_size_type_validation():
#     with pytest.raises(ValueError, match="memory_size must be an integer"):
#         FunctionConfig(handler="file.function", memory_size="512")


# def test_function_config_timeout_type_validation():
#     with pytest.raises(ValueError, match="timeout must be an integer"):
#         FunctionConfig(handler="file.function", timeout="30")
