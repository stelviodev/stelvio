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


def assert_sqs_queue(
    url: str,
    *,
    visibility_timeout: int | None = None,
    delay: int | None = None,
    fifo: bool | None = None,
) -> None:
    """Assert an SQS queue exists and has expected properties."""
    client = _boto3_session().client("sqs")
    resp = client.get_queue_attributes(QueueUrl=url, AttributeNames=["All"])
    attrs = resp["Attributes"]

    if visibility_timeout is not None:
        actual = int(attrs["VisibilityTimeout"])
        assert actual == visibility_timeout, (
            f"Expected visibility timeout {visibility_timeout}, got {actual}"
        )

    if delay is not None:
        actual = int(attrs["DelaySeconds"])
        assert actual == delay, f"Expected delay {delay}, got {actual}"

    if fifo is not None:
        actual = attrs.get("FifoQueue", "false") == "true"
        assert actual == fifo, f"Expected fifo={fifo}, got {actual}"


def assert_sns_topic(arn: str) -> None:
    """Assert an SNS topic exists."""
    client = _boto3_session().client("sns")
    client.get_topic_attributes(TopicArn=arn)


def assert_lambda_function(
    arn: str,
    *,
    runtime: str | None = None,
    timeout: int | None = None,
    memory: int | None = None,
) -> None:
    """Assert a Lambda function exists and has expected properties."""
    client = _boto3_session().client("lambda")
    resp = client.get_function(FunctionName=arn)
    config = resp["Configuration"]

    if runtime is not None:
        actual = config["Runtime"]
        assert actual == runtime, f"Expected runtime '{runtime}', got '{actual}'"

    if timeout is not None:
        actual = config["Timeout"]
        assert actual == timeout, f"Expected timeout {timeout}, got {actual}"

    if memory is not None:
        actual = config["MemorySize"]
        assert actual == memory, f"Expected memory {memory}, got {actual}"


def assert_eventbridge_rule(
    arn: str,
    *,
    schedule: str | None = None,
    state: str | None = None,
) -> None:
    """Assert an EventBridge rule exists and has expected properties."""
    # ARN format: arn:aws:events:region:account:rule/name
    rule_name = arn.split("/", 1)[1]

    client = _boto3_session().client("events")
    resp = client.describe_rule(Name=rule_name)

    if schedule is not None:
        actual = resp["ScheduleExpression"]
        assert actual == schedule, f"Expected schedule '{schedule}', got '{actual}'"

    if state is not None:
        actual = resp["State"]
        assert actual == state, f"Expected state '{state}', got '{actual}'"


def assert_s3_bucket(
    name: str,
    *,
    public_access_blocked: bool | None = None,
) -> None:
    """Assert an S3 bucket exists and has expected properties."""
    client = _boto3_session().client("s3")
    client.head_bucket(Bucket=name)

    if public_access_blocked is not None:
        resp = client.get_public_access_block(Bucket=name)
        config = resp["PublicAccessBlockConfiguration"]
        all_blocked = all(
            [
                config["BlockPublicAcls"],
                config["IgnorePublicAcls"],
                config["BlockPublicPolicy"],
                config["RestrictPublicBuckets"],
            ]
        )
        assert all_blocked == public_access_blocked, (
            f"Expected public access blocked={public_access_blocked}, got config: {config}"
        )
