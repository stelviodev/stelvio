"""AWS-specific test fixtures shared across aws test modules."""

import pytest
from pulumi.runtime import set_mocks

from .pulumi_mocks import PulumiTestMocks


@pytest.fixture
def pulumi_mocks():
    """Provide shared Pulumi mocks for AWS resource testing."""
    mocks = PulumiTestMocks()
    set_mocks(mocks)
    return mocks
