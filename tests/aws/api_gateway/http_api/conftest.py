import pulumi
import pytest
from pulumi.runtime import set_mocks

from stelvio.aws.api_gateway.http_api import HttpApi

from ...pulumi_mocks import PulumiTestMocks

TP = "test-test-"


def reset_caches() -> None:
    """Clear cached IAM role creation for API Gateway."""
    from stelvio.aws.api_gateway.iam import _create_api_gateway_account_and_role

    if hasattr(_create_api_gateway_account_and_role, "cache_clear"):
        _create_api_gateway_account_and_role.cache_clear()


def when_http_api_ready(api: HttpApi, callback) -> None:
    """Trigger callback after all HTTP API resources are created.

    Waits on stage, permissions, and routes to ensure all resources
    are registered before running assertions.
    """
    resources = api.resources
    outputs = [resources.stage.id]
    outputs.extend(permission.id for permission in resources.permissions)
    outputs.extend(route.id for route in resources.routes)
    if resources.api_mapping is not None:
        outputs.append(resources.api_mapping.id)
    pulumi.Output.all(*outputs).apply(callback)


@pytest.fixture
def pulumi_mocks() -> PulumiTestMocks:
    reset_caches()
    mocks = PulumiTestMocks()
    set_mocks(mocks)
    return mocks
