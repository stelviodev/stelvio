"""Tests for context module functionality."""

from hashlib import sha256

import pytest

from stelvio.component import safe_name
from stelvio.context import _ContextStore


@pytest.fixture(autouse=True)
def clear_context():
    _ContextStore.clear()
    yield
    _ContextStore.clear()


def _calculate_expected_hash(name: str) -> str:
    return sha256(name.encode()).hexdigest()[:7]


def test_short_name_no_truncation():
    result = safe_name("myapp-prod-", "user-handler", 64, "-r")
    assert result == "myapp-prod-user-handler-r"


def test_role_name_with_pulumi_suffix():
    """Test role name accounting for Pulumi suffix."""
    # Available: 64 - 11 (prefix) - 2 (suffix) - 8 (pulumi) = 43 chars for name
    long_name = "a" * 50  # Exceeds available space
    result = safe_name("myapp-prod-", long_name, 64, "-r")

    expected_hash = _calculate_expected_hash(long_name)
    # 43 - 8 (hash+dash) = 35 chars for truncated name
    expected = f"myapp-prod-{'a' * 35}-{expected_hash}-r"

    assert result == expected
    assert len(result) == 64 - 8  # Leaves space for Pulumi suffix


def test_policy_name_128_limit():
    long_name = "very-long-policy-name-that-should-be-truncated-appropriately"
    result = safe_name("myapp-prod-", long_name, 128, "-p")

    # Should fit without truncation: 11 + 63 + 2 + 8 = 84 < 128
    assert result == f"myapp-prod-{long_name}-p"


def test_truncation_deterministic():
    long_name = "very-long-name-that-will-definitely-be-truncated"
    result1 = safe_name("test-", long_name, 30, "-r")
    result2 = safe_name("test-", long_name, 30, "-r")
    assert result1 == result2


def test_error_insufficient_space():
    """Test error when there's insufficient space for any name."""
    with pytest.raises(ValueError, match="Cannot create safe name"):
        safe_name("very-long-prefix-", "name", 10, "-suffix")


def test_error_insufficient_space_for_hash():
    """Test error when there's insufficient space for hash."""
    with pytest.raises(ValueError, match="Cannot create safe name"):
        safe_name("long-prefix-", "name", 20, "-r")  # 12 + 2 + 8 = 22 > 20


def test_empty_name_raises_error():
    """Test that empty names are rejected."""
    with pytest.raises(ValueError, match="Name cannot be empty or whitespace-only"):
        safe_name("prefix-", "", 20, "-r")

    with pytest.raises(ValueError, match="Name cannot be empty or whitespace-only"):
        safe_name("prefix-", "   ", 20, "-r")  # Whitespace-only


def test_no_suffix():
    result = safe_name("app-", "test", 20)
    assert result == "app-test"


@pytest.mark.parametrize(
    ("max_length", "suffix", "pulumi_suffix", "expected_available"),
    [
        (64, "-r", 8, 43),  # Role: 64 - 11 - 2 - 8 = 43
        (128, "-p", 8, 107),  # Policy: 128 - 11 - 2 - 8 = 107
        (140, "", 0, 129),  # Layer no suffix, no Pulumi: 140 - 11 - 0 - 0 = 129
    ],
)
def test_space_calculations(max_length, suffix, pulumi_suffix, expected_available):
    """Test that space calculations are correct for different limits."""
    prefix = "myapp-prod-"  # 11 chars

    # Test with name that exactly fits available space
    name = "a" * expected_available
    result = safe_name(prefix, name, max_length, suffix, pulumi_suffix)

    expected_length = len(prefix) + len(name) + len(suffix)
    assert len(result) == expected_length


def test_exactly_at_truncation_boundary():
    """Test names that are exactly at the truncation boundary."""
    # Available space: 30 - 5 (prefix) - 2 (suffix) - 8 (pulumi) = 15 chars
    # Name with exactly 15 chars should not truncate
    result = safe_name("test-", "a" * 15, 30, "-r")
    assert result == f"test-{'a' * 15}-r"

    # Name with 16 chars should truncate (15 available, need 8 for hash, so 7 chars + hash)
    result = safe_name("test-", "a" * 16, 30, "-r")
    expected_hash = _calculate_expected_hash("a" * 16)
    assert result == f"test-{'a' * 7}-{expected_hash}-r"


def test_very_long_truncation():
    """Test with extremely long names."""
    very_long_name = "a" * 1000  # Very long name
    result = safe_name("short-", very_long_name, 30, "-r")

    # Should be truncated to fit
    assert len(result) == 30 - 8  # Leave space for Pulumi
    expected_hash = _calculate_expected_hash(very_long_name)
    assert result.endswith(f"-{expected_hash}-r")
