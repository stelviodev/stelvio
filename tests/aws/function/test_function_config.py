import re

import pytest

from stelvio.aws.cors import CorsConfig
from stelvio.aws.function import (
    FunctionConfig,
    FunctionConfigDict,
    FunctionUrlConfig,
    FunctionUrlConfigDict,
)
from stelvio.aws.function.constants import DEFAULT_ARCHITECTURE, DEFAULT_RUNTIME
from stelvio.aws.layer import Layer

from ...test_utils import assert_config_dict_matches_dataclass


def test_function_config_dict_has_same_fields_as_function_config():
    assert_config_dict_matches_dataclass(FunctionConfig, FunctionConfigDict)


def test_function_url_config_dict_has_same_fields_as_function_url_config():
    assert_config_dict_matches_dataclass(FunctionUrlConfig, FunctionUrlConfigDict)


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
    if folder and "::" in handler:
        pytest.skip("This combination would raise a validation error")

    config = FunctionConfig(handler=handler, folder=folder)
    assert config.folder_path == expected_folder_path


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


@pytest.mark.parametrize(
    ("auth", "should_raise", "error_match"),
    [
        ("default", False, None),
        ("iam", False, None),
        (None, False, None),
        ("invalid", True, "Invalid auth value: invalid. Must be 'default', 'iam', or None"),
        ("IAM", True, "Invalid auth value: IAM. Must be 'default', 'iam', or None"),
        ("none", True, "Invalid auth value: none. Must be 'default', 'iam', or None"),
    ],
    ids=[
        "valid_default",
        "valid_iam",
        "valid_none",
        "invalid_string",
        "uppercase_iam",
        "lowercase_none",
    ],
)
def test_function_url_config_auth_validation(auth, should_raise, error_match):
    if should_raise:
        with pytest.raises(ValueError, match=re.escape(error_match)):
            FunctionUrlConfig(auth=auth)
    else:
        config = FunctionUrlConfig(auth=auth)
        assert config.auth == auth


@pytest.mark.parametrize(
    ("streaming", "should_raise"),
    [
        (True, False),
        (False, False),
        ("true", True),
        (1, True),
    ],
    ids=["valid_true", "valid_false", "invalid_string", "invalid_int"],
)
def test_function_url_config_streaming_validation(streaming, should_raise):
    if should_raise:
        with pytest.raises(TypeError, match="streaming must be a boolean"):
            FunctionUrlConfig(streaming=streaming)
    else:
        config = FunctionUrlConfig(streaming=streaming)
        assert config.streaming == streaming


@pytest.mark.parametrize(
    ("cors_input", "expected_type", "expected_origins", "expected_methods", "expected_headers"),
    [
        (True, CorsConfig, "*", "*", "*"),
        (None, type(None), None, None, None),
        (False, type(None), None, None, None),
    ],
    ids=["cors_true", "cors_none", "cors_false"],
)
def test_function_url_config_normalized_cors(
    cors_input, expected_type, expected_origins, expected_methods, expected_headers
):
    config = FunctionUrlConfig(cors=cors_input)
    normalized = config.normalized_cors
    assert isinstance(normalized, expected_type)
    if normalized is not None:
        assert normalized.allow_origins == expected_origins
        assert normalized.allow_methods == expected_methods
        assert normalized.allow_headers == expected_headers


def test_function_url_config_normalized_cors_with_config_object():
    cors_config = CorsConfig(allow_origins="https://example.com")
    config = FunctionUrlConfig(cors=cors_config)
    normalized = config.normalized_cors
    assert normalized is cors_config


def test_function_url_config_normalized_cors_with_dict():
    config = FunctionUrlConfig(
        cors={"allow_origins": "https://example.com", "allow_methods": ["GET", "POST"]}
    )
    normalized = config.normalized_cors
    assert isinstance(normalized, CorsConfig)
    assert normalized.allow_origins == "https://example.com"
    assert normalized.allow_methods == ["GET", "POST"]


@pytest.mark.parametrize(
    ("url", "error_type", "error_match", "use_escape"),
    [
        (
            "Public",
            ValueError,
            "Invalid url shortcut: 'Public'. Must be 'public' or 'private'",
            True,
        ),
        (
            "PRIVATE",
            ValueError,
            "Invalid url shortcut: 'PRIVATE'. Must be 'public' or 'private'",
            True,
        ),
        ("none", ValueError, "Invalid url shortcut: 'none'. Must be 'public' or 'private'", True),
        (
            123,
            TypeError,
            "url must be 'public', 'private', FunctionUrlConfig, dict, or None. Got int",
            True,
        ),
        ({"auth": "invalid-auth"}, ValueError, "Invalid url configuration", False),
    ],
    ids=[
        "invalid_shortcut_public",
        "invalid_shortcut_private",
        "invalid_shortcut_none",
        "invalid_type",
        "invalid_dict",
    ],
)
def test_function_config_invalid_url(url, error_type, error_match, use_escape):
    match_pattern = re.escape(error_match) if use_escape else error_match
    with pytest.raises(error_type, match=match_pattern):
        FunctionConfig(handler="functions/simple.handler", url=url)


@pytest.mark.parametrize(
    ("url", "expected_url"),
    [
        ("public", "public"),
        ("private", "private"),
        (None, None),
    ],
    ids=["public_shortcut", "private_shortcut", "none"],
)
def test_function_config_url_shortcuts_and_none(url, expected_url):
    config = FunctionConfig(handler="functions/simple.handler", url=url)
    assert config.url == expected_url


def test_function_config_url_with_function_url_config():
    url_config = FunctionUrlConfig(auth="iam", cors=True)
    config = FunctionConfig(handler="functions/simple.handler", url=url_config)
    assert config.url == url_config


def test_function_config_url_with_dict():
    url_dict = {"auth": "iam", "cors": True}
    config = FunctionConfig(handler="functions/simple.handler", url=url_dict)
    assert config.url == url_dict
