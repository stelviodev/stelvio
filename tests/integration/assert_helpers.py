import os

import boto3


def _boto3_session() -> boto3.Session:
    return boto3.Session(
        profile_name=os.environ.get("STLV_TEST_AWS_PROFILE"),
        region_name=os.environ.get("STLV_TEST_AWS_REGION", "us-east-1"),
    )


def assert_dynamo_table(
    arn: str,
    *,
    hash_key: str | None = None,
    sort_key: str | None = None,
    billing_mode: str | None = None,
) -> None:
    """Assert a DynamoDB table exists and has expected properties."""
    # ARN format: arn:aws:dynamodb:region:account:table/name
    table_name = arn.split("/", 1)[1]

    client = _boto3_session().client("dynamodb")
    resp = client.describe_table(TableName=table_name)
    table = resp["Table"]

    if hash_key is not None:
        key_schema = {k["AttributeName"]: k["KeyType"] for k in table["KeySchema"]}
        assert key_schema.get(hash_key) == "HASH", (
            f"Expected hash key '{hash_key}', got schema: {key_schema}"
        )

    if sort_key is not None:
        key_schema = {k["AttributeName"]: k["KeyType"] for k in table["KeySchema"]}
        assert key_schema.get(sort_key) == "RANGE", (
            f"Expected sort key '{sort_key}', got schema: {key_schema}"
        )

    if billing_mode is not None:
        actual = table.get("BillingModeSummary", {}).get("BillingMode", "PROVISIONED")
        assert actual == billing_mode, f"Expected billing mode '{billing_mode}', got '{actual}'"
