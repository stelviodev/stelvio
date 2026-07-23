import pulumi

from stelvio.aws.api_gateway.http_api import HttpApi

TP = "test-test-"


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
