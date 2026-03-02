import pytest
from pulumi.runtime import set_mocks

from ..pulumi_mocks import PulumiTestMocks


def reset_api_gateway_caches() -> None:
    """Clear API Gateway IAM cache to avoid cross-test contamination."""
    from stelvio.aws.api_gateway.iam import _create_api_gateway_account_and_role

    if hasattr(_create_api_gateway_account_and_role, "cache_clear"):
        _create_api_gateway_account_and_role.cache_clear()


@pytest.fixture
def pulumi_mocks() -> PulumiTestMocks:
    reset_api_gateway_caches()
    mocks = PulumiTestMocks()
    set_mocks(mocks)
    return mocks
