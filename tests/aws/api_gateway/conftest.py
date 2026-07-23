import pytest
from pulumi.runtime import set_mocks

from stelvio.aws.api_gateway.iam import _create_api_gateway_account_and_role

from ..pulumi_mocks import PulumiTestMocks


@pytest.fixture
def pulumi_mocks() -> PulumiTestMocks:
    _create_api_gateway_account_and_role.cache_clear()
    mocks = PulumiTestMocks()
    set_mocks(mocks)
    return mocks
