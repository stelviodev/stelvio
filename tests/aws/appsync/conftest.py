"""Test fixtures for AppSync tests."""

import pytest
from pulumi.runtime import set_mocks

from tests.aws.pulumi_mocks import PulumiTestMocks


@pytest.fixture
def pulumi_mocks():
    """Provide Pulumi test mocks with AppSync resource support."""
    mocks = PulumiTestMocks()
    set_mocks(mocks)
    return mocks
