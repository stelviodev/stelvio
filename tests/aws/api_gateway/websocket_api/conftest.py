import json
from collections.abc import Sequence
from typing import Any

import pulumi

from stelvio.aws.api_gateway.websocket_api import WebsocketApi

from ...pulumi_mocks import ACCOUNT_ID, DEFAULT_REGION, R, tid, tn

TP = "test-test-"

# PulumiTestMocks derive the API id from the resource id: api_id = tid(name)[:8].
WEBSOCKET_API_ID = tid(TP + "chat")[:8]
LAMBDA_INVOKE_ARN_TEMPLATE = (
    f"arn:aws:apigateway:{DEFAULT_REGION}:lambda:path/2015-03-31/functions/"
    f"arn:aws:lambda:{DEFAULT_REGION}:{ACCOUNT_ID}:function:{{function_name}}/invocations"
)
LAMBDA_ASSUME_ROLE_POLICY = [
    {
        "actions": ["sts:AssumeRole"],
        "principals": [{"identifiers": ["lambda.amazonaws.com"], "type": "Service"}],
    }
]

_DOMAIN_GRAPH_COUNTS = {
    R.CERTIFICATE: 1,
    R.CLOUDFLARE_RECORD: 2,
    R.CERTIFICATE_VALIDATION: 1,
    R.HTTP_API_DOMAIN_NAME: 1,
}


def when_websocket_api_ready(api: WebsocketApi | Sequence[WebsocketApi], callback) -> None:
    """Trigger callback after all WebSocket API resources are created.

    Waits on stage, permissions, routes, and api mapping (when present)
    so all resources are registered before assertions run. Accepts one
    API or a sequence when multiple APIs must finish together.
    """
    apis = (api,) if isinstance(api, WebsocketApi) else api
    outputs = []
    for websocket_api in apis:
        resources = websocket_api.resources
        outputs.append(resources.stage.id)
        outputs.extend(permission.id for permission in resources.permissions)
        outputs.extend(route.id for route in resources.routes)
        if resources.api_mapping is not None:
            outputs.append(resources.api_mapping.id)
    pulumi.Output.all(*outputs).apply(callback)


def websocket_api_counts(  # noqa: PLR0913
    *,
    function_count: int = 0,
    route_count: int = 0,
    integration_count: int | None = None,
    permission_count: int | None = None,
    authorizer_count: int = 0,
    mapping_count: int = 0,
    with_domain: bool = False,
    policy_count: int = 0,
    extra: dict[R, int] | None = None,
) -> dict[R, int]:
    """Exact resource counts for a single WebsocketApi resource graph."""
    integration_count = function_count if integration_count is None else integration_count
    permission_count = function_count if permission_count is None else permission_count
    counts: dict[R, int] = {
        R.HTTP_API: 1,
        R.HTTP_API_STAGE: 1,
    }
    if function_count:
        counts[R.FUNCTION] = function_count
        counts[R.ROLE] = function_count
        counts[R.ROLE_POLICY_ATTACHMENT] = function_count + policy_count
    if policy_count:
        counts[R.POLICY] = policy_count
    if integration_count:
        counts[R.HTTP_API_INTEGRATION] = integration_count
    if permission_count:
        counts[R.LAMBDA_PERMISSION] = permission_count
    if route_count:
        counts[R.HTTP_API_ROUTE] = route_count
    if authorizer_count:
        counts[R.HTTP_API_AUTHORIZER] = authorizer_count
    if mapping_count:
        counts[R.HTTP_API_MAPPING] = mapping_count
    if with_domain:
        counts |= _DOMAIN_GRAPH_COUNTS
    if extra:
        counts |= extra
    return counts


def assert_api_domain_graph(
    mocks,
    *,
    domain_component: str,
    domain_name: str,
    domain_extra_inputs: dict[str, Any] | None = None,
) -> None:
    """Assert ACM, DomainName, and DNS resources for an ApiDomain."""
    certificate_arn = (
        f"arn:aws:acm:{DEFAULT_REGION}:{ACCOUNT_ID}:certificate/"
        f"{tid(TP + f'{domain_component}-cert-certificate')}"
    )
    mocks.assert_res(
        f"{domain_component}-cert-certificate",
        R.CERTIFICATE,
        {"domainName": domain_name, "validationMethod": "DNS"},
    )
    mocks.assert_res(
        f"{domain_component}-cert-certificate-validation",
        R.CERTIFICATE_VALIDATION,
        {
            "certificateArn": certificate_arn,
            "validationRecordFqdns": [f"_test.{domain_name}"],
        },
    )
    domain_inputs: dict[str, Any] = {
        "domainName": domain_name,
        "domainNameConfiguration": {
            "certificateArn": certificate_arn,
            "endpointType": "REGIONAL",
            "securityPolicy": "TLS_1_2",
        },
    }
    if domain_extra_inputs:
        domain_inputs.update(domain_extra_inputs)
    mocks.assert_res(
        f"{domain_component}-domain",
        R.HTTP_API_DOMAIN_NAME,
        domain_inputs,
        partial=domain_extra_inputs is not None,
    )
    mocks.assert_res(
        f"{domain_component}-cert-certificate-validation-record",
        R.CLOUDFLARE_RECORD,
        {
            "name": f"_test.{domain_name}",
            "type": "CNAME",
            "content": f"test-validation.{domain_name}",
            "ttl": 1.0,
        },
        partial=True,
    )
    mocks.assert_res(
        f"{domain_component}-dns-record",
        R.CLOUDFLARE_RECORD,
        {
            "name": domain_name,
            "type": "CNAME",
            "content": (
                f"d-{tid(TP + f'{domain_component}-domain')}"
                f".execute-api.{DEFAULT_REGION}.amazonaws.com"
            ),
            "ttl": 300.0,
        },
        partial=True,
    )


def assert_api_mapping(
    mocks,
    *,
    api_name: str,
    domain_name: str,
    mapping_key: str | None = None,
) -> None:
    """Assert a WebsocketApi ApiMapping with exact inputs."""
    inputs: dict[str, Any] = {
        "apiId": tid(TP + api_name)[:8],
        "domainName": domain_name,
        "stage": tid(TP + f"{api_name}-stage"),
    }
    if mapping_key is not None:
        inputs["apiMappingKey"] = mapping_key
    mocks.assert_res(f"{api_name}-api-mapping", R.HTTP_API_MAPPING, inputs)


def assert_lambda_role_and_attachment(mocks, function_name: str) -> None:
    role_name = f"{function_name}-r"
    mocks.assert_res(
        role_name,
        R.ROLE,
        {"assumeRolePolicy": json.dumps(LAMBDA_ASSUME_ROLE_POLICY)},
    )
    mocks.assert_res(
        f"{function_name}-basic-execution-r-p-attachment",
        R.ROLE_POLICY_ATTACHMENT,
        {
            "policyArn": "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
            "role": tn(TP + role_name),
        },
    )
