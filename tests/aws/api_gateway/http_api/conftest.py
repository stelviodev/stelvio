from ...pulumi_mocks import ACCOUNT_ID, DEFAULT_REGION, tid

TP = "test-test-"

# PulumiTestMocks use tid(name) as the API id (no truncation — ids must be unique).
HTTP_API_ID = tid(TP + "my-api")
LAMBDA_INVOKE_ARN_TEMPLATE = (
    f"arn:aws:apigateway:{DEFAULT_REGION}:lambda:path/2015-03-31/functions/"
    f"arn:aws:lambda:{DEFAULT_REGION}:{ACCOUNT_ID}:function:{{function_name}}/invocations"
)
