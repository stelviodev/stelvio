import pulumi
import pytest
from pulumi.runtime import set_mocks

from stelvio.aws.api_gateway import Api

from ..pulumi_mocks import PulumiTestMocks


def when_api_ready(api: Api, callback):
    """Trigger callback after all API resources (including permissions) are created."""
    outputs = [api.resources.stage.id]
    outputs.extend(p.id for p in api._permissions)
    if api.resources.base_path_mapping is not None:
        outputs.append(api.resources.base_path_mapping.id)
    pulumi.Output.all(*outputs).apply(callback)


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
