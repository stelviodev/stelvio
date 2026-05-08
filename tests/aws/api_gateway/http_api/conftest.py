import pulumi
import pytest
from pulumi.runtime import set_mocks

from stelvio.aws.api_gateway.http_api import HttpApi

from ...pulumi_mocks import PulumiTestMocks


def reset_caches() -> None:
    """Clear cached IAM role creation for API Gateway."""
    from stelvio.aws.api_gateway.rest_api.iam import _create_api_gateway_account_and_role

    if hasattr(_create_api_gateway_account_and_role, "cache_clear"):
        _create_api_gateway_account_and_role.cache_clear()


def when_http_api_ready(api: HttpApi, callback) -> None:
    """Trigger callback after all HTTP API resources are created.

    Waits on stage.id, permissions, routes, and authorizers to ensure all
    resources are registered before running assertions.
    """
    outputs = [api.resources.stage.id]
    outputs.extend(p.id for p in api._permissions)
    outputs.extend(r.id for r in api._route_resources)
    outputs.extend(a.id for a in api._authorizer_resources)
    if api.resources.api_mapping is not None:
        outputs.append(api.resources.api_mapping.id)
    pulumi.Output.all(*outputs).apply(callback)


@pytest.fixture
def pulumi_mocks() -> PulumiTestMocks:
    reset_caches()
    mocks = PulumiTestMocks()
    set_mocks(mocks)
    return mocks
