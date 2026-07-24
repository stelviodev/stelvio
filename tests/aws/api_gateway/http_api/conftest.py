from collections.abc import Sequence

import pulumi
import pytest

from stelvio.aws.api_gateway.http_api import HttpApi

TP = "test-test-"


def reset_caches() -> None:
    """Clear cached IAM role creation for API Gateway."""
    from stelvio.aws.api_gateway.rest_api.iam import _create_api_gateway_account_and_role

    if hasattr(_create_api_gateway_account_and_role, "cache_clear"):
        _create_api_gateway_account_and_role.cache_clear()


@pytest.fixture(autouse=True)
def reset_api_gateway_caches():
    reset_caches()
    yield
    reset_caches()


def when_http_api_ready(api: HttpApi | Sequence[HttpApi], callback) -> None:
    """Trigger callback after all HTTP API resources are created.

    Waits on stage, permissions, routes, and api mapping (when present)
    so all resources are registered before assertions run. Accepts one
    API or a sequence when multiple APIs must finish together.
    """
    apis = (api,) if isinstance(api, HttpApi) else api
    outputs = []
    for http_api in apis:
        resources = http_api.resources
        outputs.append(resources.stage.id)
        outputs.extend(permission.id for permission in resources.permissions)
        outputs.extend(route.id for route in resources.routes)
        if resources.api_mapping is not None:
            outputs.append(resources.api_mapping.id)
    pulumi.Output.all(*outputs).apply(callback)
