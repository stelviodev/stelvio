"""
Tests for validating that Pulumi *Args classes (specifically FunctionArgs) convert
correctly to dictionaries via vars(), ensuring the _customizer method's assumption
about clean __dict__ representation holds true.

Key discovery: Pulumi *Args classes use a sparse dict pattern - only keys that are
explicitly set are stored in __dict__. This is ideal for the _customizer merge pattern
since unset keys won't override defaults.
"""

import pytest
from pulumi_aws import lambda_

from stelvio.aws.function import Function
from stelvio.pulumi import normalize_pulumi_args_to_dict

# Known valid FunctionArgs property names from Pulumi's lambda_.FunctionArgs
# This set helps verify no unexpected internal keys (like Pulumi metadata) appear in __dict__
VALID_FUNCTION_ARGS_KEYS = {
    "architectures",
    "code",
    "code_signing_config_arn",
    "dead_letter_config",
    "description",
    "environment",
    "ephemeral_storage",
    "file_system_config",
    "handler",
    "image_config",
    "image_uri",
    "kms_key_arn",
    "layers",
    "logging_config",
    "memory_size",
    "name",
    "package_type",
    "publish",
    "replace_security_groups_on_destroy",
    "replacement_security_group_ids",
    "reserved_concurrent_executions",
    "role",
    "runtime",
    "s3_bucket",
    "s3_key",
    "s3_object_version",
    "skip_destroy",
    "snap_start",
    "source_code_hash",
    "tags",
    "timeout",
    "tracing_config",
    "vpc_config",
}


def test_function_args_sparse_dict_only_set_keys():
    """Verify FunctionArgs uses sparse dict - only explicitly set keys appear in __dict__.

    This is critical for _customizer: unset optional fields don't appear in the dict,
    so they won't override defaults when merging.
    """
    args = lambda_.FunctionArgs(role="arn:aws:iam::123456789012:role/test-role")
    result = vars(args)

    # Only the explicitly set 'role' key should be present
    assert "role" in result
    assert result["role"] == "arn:aws:iam::123456789012:role/test-role"

    # Unset optional keys should NOT be present (sparse dict behavior)
    assert "memory_size" not in result
    assert "timeout" not in result
    assert "handler" not in result


def test_function_args_with_values_converts_to_dict():
    """Set memory_size, timeout, description and verify vars() returns matching pairs."""
    args = lambda_.FunctionArgs(
        role="arn:aws:iam::123456789012:role/test-role",
        memory_size=512,
        timeout=30,
        description="Test function description",
    )
    result = vars(args)

    # Explicitly set keys should be present with correct values
    assert result["role"] == "arn:aws:iam::123456789012:role/test-role"
    assert result["memory_size"] == 512
    assert result["timeout"] == 30
    assert result["description"] == "Test function description"

    # Only the 4 set keys should be in the dict
    assert len(result) == 4


def test_function_args_no_internal_pulumi_keys():
    """Verify FunctionArgs.__dict__ contains no internal Pulumi metadata keys.

    This ensures vars() returns a clean dict suitable for merging with default props.
    """
    args = lambda_.FunctionArgs(
        role="arn:aws:iam::123456789012:role/test-role",
        memory_size=1024,
        timeout=60,
    )
    result = vars(args)

    # All keys in result should be valid FunctionArgs properties
    for key in result:
        assert key in VALID_FUNCTION_ARGS_KEYS, (
            f"Unexpected key '{key}' in FunctionArgs.__dict__. "
            "This may be internal Pulumi metadata that could break _customizer merging."
        )


def test_function_args_nested_environment_converts_correctly():
    """Test FunctionArgs with environment converts cleanly."""
    env_args = lambda_.FunctionEnvironmentArgs(variables={"KEY1": "value1", "KEY2": "value2"})
    args = lambda_.FunctionArgs(
        role="arn:aws:iam::123456789012:role/test-role",
        memory_size=256,
        environment=env_args,
    )
    result = vars(args)

    assert result["memory_size"] == 256
    assert result["environment"] is env_args

    # The nested FunctionEnvironmentArgs should also convert cleanly
    env_result = vars(env_args)
    assert env_result["variables"] == {"KEY1": "value1", "KEY2": "value2"}


class _MockArgsWithDict:
    """Mock class with __dict__ for testing conversion."""

    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)


class _InvalidType:
    """Mock class without proper __dict__ for testing ValueError."""

    __slots__ = ()  # No __dict__


def test_normalize_pulumi_args_none_returns_empty_dict():
    """Test that None input returns empty dict."""
    assert normalize_pulumi_args_to_dict(None) == {}


def test_normalize_pulumi_args_dict_passthrough():
    """Test that dict input passes through unchanged."""
    input_dict = {"memory_size": 512, "timeout": 30}
    result = normalize_pulumi_args_to_dict(input_dict)

    assert result is input_dict  # Same reference, not copied
    assert result == {"memory_size": 512, "timeout": 30}


def test_normalize_pulumi_args_object_with_dict():
    """Test that object with __dict__ converts via vars()."""
    mock_args = _MockArgsWithDict(memory_size=1024, description="test")
    result = normalize_pulumi_args_to_dict(mock_args)

    assert result == {"memory_size": 1024, "description": "test"}


def test_normalize_pulumi_args_invalid_type_raises():
    """Test that invalid type raises ValueError."""
    # Use a type that doesn't have __dict__ (uses __slots__)
    invalid = _InvalidType()

    with pytest.raises(ValueError, match="Cannot convert customization value to dict"):
        normalize_pulumi_args_to_dict(invalid)


def test_normalize_pulumi_args_function_args():
    """Test that actual FunctionArgs converts correctly via normalize_pulumi_args_to_dict."""
    args = lambda_.FunctionArgs(
        role="arn:aws:iam::123456789012:role/test-role",
        memory_size=512,
        timeout=60,
        description="Production function",
    )
    result = normalize_pulumi_args_to_dict(args)

    assert result["memory_size"] == 512
    assert result["timeout"] == 60
    assert result["description"] == "Production function"


@pytest.mark.usefixtures("project_cwd", "app_context")
def test_customizer_merges_function_args_with_defaults():
    """Test that Function._customizer properly merges FunctionArgs with defaults."""
    # Note: role is required by FunctionArgs, but in customize context we're only
    # providing overrides - the role will come from default_props in real usage.
    # We include it here to satisfy the FunctionArgs constructor.
    custom_args = lambda_.FunctionArgs(
        role="arn:aws:iam::123456789012:role/custom-role",
        memory_size=1024,  # Override default
        description="Custom description",  # Add new property
    )

    function = Function(
        "test-customizer-fn",
        handler="functions/simple.handler",
        customize={"function": custom_args},
    )

    default_props = {"memory_size": 256, "timeout": 10}
    result = function._customizer("function", default_props)

    # Instance customization should override defaults
    assert result["memory_size"] == 1024
    # Default should be preserved when not overridden
    assert result["timeout"] == 10
    # New properties from customization should be added
    assert result["description"] == "Custom description"


@pytest.mark.usefixtures("project_cwd", "app_context")
def test_customizer_dict_also_works():
    """Test that plain dict customization also works (for comparison)."""
    function = Function(
        "test-dict-fn",
        handler="functions/simple.handler",
        customize={"function": {"memory_size": 2048, "tags": {"env": "test"}}},
    )

    default_props = {"memory_size": 256, "timeout": 10}
    result = function._customizer("function", default_props)

    assert result["memory_size"] == 2048
    assert result["timeout"] == 10
    assert result["tags"] == {"env": "test"}


@pytest.mark.usefixtures("project_cwd", "app_context")
def test_customizer_sparse_dict_preserves_all_defaults():
    """Test that sparse FunctionArgs only overrides explicitly set properties.

    This is the key benefit of the sparse dict pattern: users can set just the
    properties they want to override, and all other defaults are preserved.
    """
    # Only override memory_size, leave everything else to defaults
    custom_args = lambda_.FunctionArgs(
        role="arn:aws:iam::123456789012:role/custom-role",
        memory_size=2048,
    )

    function = Function(
        "test-sparse-fn",
        handler="functions/simple.handler",
        customize={"function": custom_args},
    )

    default_props = {
        "memory_size": 256,
        "timeout": 30,
        "runtime": "python3.12",
        "handler": "index.handler",
    }
    result = function._customizer("function", default_props)

    # Only memory_size and role should be overridden
    assert result["memory_size"] == 2048
    assert result["role"] == "arn:aws:iam::123456789012:role/custom-role"

    # All other defaults should be preserved
    assert result["timeout"] == 30
    assert result["runtime"] == "python3.12"
    assert result["handler"] == "index.handler"
