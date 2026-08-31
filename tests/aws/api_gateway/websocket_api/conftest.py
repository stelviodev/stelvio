from typing import Any

from ...pulumi_mocks import ACCOUNT_ID, DEFAULT_REGION, R, tid

TP = "test-test-"

# PulumiTestMocks use tid(name) as the API id (no truncation — ids must be unique).
WEBSOCKET_API_ID = tid(TP + "chat")
LAMBDA_INVOKE_ARN_TEMPLATE = (
    f"arn:aws:apigateway:{DEFAULT_REGION}:lambda:path/2015-03-31/functions/"
    f"arn:aws:lambda:{DEFAULT_REGION}:{ACCOUNT_ID}:function:{{function_name}}/invocations"
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
        "apiId": tid(TP + api_name),
        "domainName": domain_name,
        "stage": tid(TP + f"{api_name}-stage"),
    }
    if mapping_key is not None:
        inputs["apiMappingKey"] = mapping_key
    mocks.assert_res(f"{api_name}-api-mapping", R.HTTP_API_MAPPING, inputs)
