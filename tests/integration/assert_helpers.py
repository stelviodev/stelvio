import json
import os

import boto3


def _boto3_session() -> boto3.Session:
    return boto3.Session(
        profile_name=os.environ.get("STLV_TEST_AWS_PROFILE"),
        region_name=os.environ.get("STLV_TEST_AWS_REGION", "us-east-1"),
    )


def assert_dynamo_table(  # noqa: PLR0913
    arn: str,
    *,
    hash_key: str | None = None,
    sort_key: str | None = None,
    billing_mode: str | None = None,
    stream_enabled: bool | None = None,
    stream_view_type: str | None = None,
    gsi_names: list[str] | None = None,
    lsi_names: list[str] | None = None,
) -> None:
    """Assert a DynamoDB table exists and has expected properties."""
    # ARN format: arn:aws:dynamodb:region:account:table/name
    table_name = arn.split("/", 1)[1]

    client = _boto3_session().client("dynamodb")
    resp = client.describe_table(TableName=table_name)
    table = resp["Table"]

    if hash_key is not None or sort_key is not None:
        key_schema = {k["AttributeName"]: k["KeyType"] for k in table["KeySchema"]}
        if hash_key is not None:
            assert key_schema.get(hash_key) == "HASH", (
                f"Expected hash key '{hash_key}', got schema: {key_schema}"
            )
        if sort_key is not None:
            assert key_schema.get(sort_key) == "RANGE", (
                f"Expected sort key '{sort_key}', got schema: {key_schema}"
            )

    if billing_mode is not None:
        actual = table.get("BillingModeSummary", {}).get("BillingMode", "PROVISIONED")
        assert actual == billing_mode, f"Expected billing mode '{billing_mode}', got '{actual}'"

    if stream_enabled is not None:
        actual = table.get("StreamSpecification", {}).get("StreamEnabled", False)
        assert actual == stream_enabled, f"Expected stream_enabled={stream_enabled}, got {actual}"

    if stream_view_type is not None:
        actual = table.get("StreamSpecification", {}).get("StreamViewType")
        assert actual == stream_view_type, (
            f"Expected stream_view_type '{stream_view_type}', got '{actual}'"
        )

    if gsi_names is not None:
        actual_gsi = {idx["IndexName"] for idx in table.get("GlobalSecondaryIndexes", [])}
        expected = set(gsi_names)
        assert actual_gsi == expected, f"Expected GSIs {expected}, got {actual_gsi}"

    if lsi_names is not None:
        actual_lsi = {idx["IndexName"] for idx in table.get("LocalSecondaryIndexes", [])}
        expected = set(lsi_names)
        assert actual_lsi == expected, f"Expected LSIs {expected}, got {actual_lsi}"


def assert_sqs_queue(  # noqa: PLR0913
    url: str,
    *,
    visibility_timeout: int | None = None,
    delay: int | None = None,
    retention: int | None = None,
    fifo: bool | None = None,
    dlq_arn: str | None = None,
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

    if retention is not None:
        actual = int(attrs["MessageRetentionPeriod"])
        assert actual == retention, f"Expected retention {retention}, got {actual}"

    if fifo is not None:
        actual = attrs.get("FifoQueue", "false") == "true"
        assert actual == fifo, f"Expected fifo={fifo}, got {actual}"

    if dlq_arn is not None:
        redrive = json.loads(attrs.get("RedrivePolicy", "{}"))
        actual = redrive.get("deadLetterTargetArn")
        assert actual == dlq_arn, f"Expected DLQ ARN '{dlq_arn}', got '{actual}'"


def assert_sns_topic(arn: str, *, fifo: bool | None = None) -> None:
    """Assert an SNS topic exists and has expected properties."""
    client = _boto3_session().client("sns")
    resp = client.get_topic_attributes(TopicArn=arn)
    attrs = resp["Attributes"]

    if fifo is not None:
        actual = attrs.get("FifoTopic", "false") == "true"
        assert actual == fifo, f"Expected fifo={fifo}, got {actual}"


def assert_sns_subscription(
    topic_arn: str,
    *,
    protocol: str,
    endpoint: str,
) -> None:
    """Assert an SNS subscription exists with expected protocol and endpoint."""
    client = _boto3_session().client("sns")
    resp = client.list_subscriptions_by_topic(TopicArn=topic_arn)
    subs = resp["Subscriptions"]

    matching = [s for s in subs if s["Protocol"] == protocol and s["Endpoint"] == endpoint]
    assert len(matching) == 1, (
        f"Expected 1 {protocol} subscription to {endpoint}, "
        f"got {len(matching)}. All subs: {[(s['Protocol'], s['Endpoint']) for s in subs]}"
    )


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


def assert_event_source_mapping(
    function_arn: str,
    *,
    event_source_arn: str,
    batch_size: int | None = None,
    state: str = "Enabled",
) -> None:
    """Assert an event source mapping exists connecting a source to a Lambda function."""
    client = _boto3_session().client("lambda")
    resp = client.list_event_source_mappings(FunctionName=function_arn)
    mappings = resp["EventSourceMappings"]

    # Find mapping for the expected event source
    matching = [m for m in mappings if m["EventSourceArn"] == event_source_arn]
    assert len(matching) == 1, (
        f"Expected 1 event source mapping for {event_source_arn}, "
        f"got {len(matching)}. All mappings: {[m['EventSourceArn'] for m in mappings]}"
    )

    mapping = matching[0]

    actual_state = mapping["State"]
    assert actual_state == state, f"Expected mapping state '{state}', got '{actual_state}'"

    if batch_size is not None:
        actual = mapping["BatchSize"]
        assert actual == batch_size, f"Expected batch_size {batch_size}, got {actual}"


def assert_s3_bucket(
    name: str,
    *,
    public_access_blocked: bool | None = None,
    versioning: bool | None = None,
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

    if versioning is not None:
        resp = client.get_bucket_versioning(Bucket=name)
        actual = resp.get("Status") == "Enabled"
        assert actual == versioning, (
            f"Expected versioning={versioning}, got status={resp.get('Status')}"
        )


def assert_s3_bucket_notifications(
    name: str,
    *,
    lambda_count: int | None = None,
    queue_count: int | None = None,
    topic_count: int | None = None,
) -> None:
    """Assert S3 bucket notification configuration."""
    client = _boto3_session().client("s3")
    resp = client.get_bucket_notification_configuration(Bucket=name)

    if lambda_count is not None:
        actual = len(resp.get("LambdaFunctionConfigurations", []))
        assert actual == lambda_count, (
            f"Expected {lambda_count} Lambda notifications, got {actual}"
        )

    if queue_count is not None:
        actual = len(resp.get("QueueConfigurations", []))
        assert actual == queue_count, f"Expected {queue_count} Queue notifications, got {actual}"

    if topic_count is not None:
        actual = len(resp.get("TopicConfigurations", []))
        assert actual == topic_count, f"Expected {topic_count} Topic notifications, got {actual}"
