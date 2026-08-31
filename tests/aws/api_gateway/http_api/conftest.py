from collections.abc import Sequence

import pulumi

from stelvio.aws.api_gateway.http_api import HttpApi

from ...pulumi_mocks import ACCOUNT_ID, DEFAULT_REGION, tid

TP = "test-test-"

# PulumiTestMocks use tid(name) as the API id (no truncation — ids must be unique).
HTTP_API_ID = tid(TP + "my-api")
LAMBDA_INVOKE_ARN_TEMPLATE = (
    f"arn:aws:apigateway:{DEFAULT_REGION}:lambda:path/2015-03-31/functions/"
    f"arn:aws:lambda:{DEFAULT_REGION}:{ACCOUNT_ID}:function:{{function_name}}/invocations"
)


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
