import json
import os
import urllib.request

import boto3


def _boto3_session() -> boto3.Session:
    return boto3.Session(
        profile_name=os.environ.get("STLV_TEST_AWS_PROFILE"),
        region_name=os.environ.get("STLV_TEST_AWS_REGION", "us-east-1"),
    )


def _assert_gsi_details(table: dict, gsi_details: dict[str, dict]) -> None:
    """Assert GSI index properties (keys, projection type, non-key attributes)."""
    gsi_by_name = {idx["IndexName"]: idx for idx in table.get("GlobalSecondaryIndexes", [])}
    for idx_name, expected_props in gsi_details.items():
        assert idx_name in gsi_by_name, (
            f"GSI '{idx_name}' not found. Available: {list(gsi_by_name.keys())}"
        )
        idx = gsi_by_name[idx_name]
        idx_keys = {k["AttributeName"]: k["KeyType"] for k in idx["KeySchema"]}

        if "hash_key" in expected_props:
            assert idx_keys.get(expected_props["hash_key"]) == "HASH", (
                f"GSI '{idx_name}': expected hash key '{expected_props['hash_key']}', "
                f"got schema: {idx_keys}"
            )
        if "sort_key" in expected_props:
            assert idx_keys.get(expected_props["sort_key"]) == "RANGE", (
                f"GSI '{idx_name}': expected sort key '{expected_props['sort_key']}', "
                f"got schema: {idx_keys}"
            )
        if "projection_type" in expected_props:
            actual_proj = idx["Projection"]["ProjectionType"]
            expected_proj = expected_props["projection_type"]
            assert actual_proj == expected_proj, (
                f"GSI '{idx_name}': expected projection '{expected_proj}', got '{actual_proj}'"
            )
        if "non_key_attributes" in expected_props:
            actual_attrs = set(idx["Projection"].get("NonKeyAttributes", []))
            expected_attrs = set(expected_props["non_key_attributes"])
            assert actual_attrs == expected_attrs, (
                f"GSI '{idx_name}': expected non-key attributes {expected_attrs}, "
                f"got {actual_attrs}"
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
    gsi_details: dict[str, dict] | None = None,
    lsi_names: list[str] | None = None,
) -> None:
    """Assert a DynamoDB table exists and has expected properties.

    gsi_details accepts a dict of index_name → expected properties:
        {"index-name": {"hash_key": "field", "sort_key": "field",
                        "projection_type": "INCLUDE",
                        "non_key_attributes": ["attr1"]}}
    """
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

    if gsi_details is not None:
        _assert_gsi_details(table, gsi_details)

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
    dlq_retry: int | None = None,
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

    if dlq_retry is not None:
        redrive = json.loads(attrs.get("RedrivePolicy", "{}"))
        actual = redrive.get("maxReceiveCount")
        assert actual == dlq_retry, f"Expected DLQ retry {dlq_retry}, got {actual}"


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


def assert_lambda_function(  # noqa: PLR0913
    arn: str,
    *,
    runtime: str | None = None,
    timeout: int | None = None,
    memory: int | None = None,
    environment: dict[str, str] | None = None,
    layers_count: int | None = None,
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

    if environment is not None:
        actual_env = config.get("Environment", {}).get("Variables", {})
        for key, value in environment.items():
            assert key in actual_env, (
                f"Expected env var '{key}' not found. Actual vars: {list(actual_env.keys())}"
            )
            assert actual_env[key] == value, (
                f"Expected env var '{key}'='{value}', got '{actual_env[key]}'"
            )

    if layers_count is not None:
        actual = len(config.get("Layers", []))
        assert actual == layers_count, f"Expected {layers_count} layers, got {actual}"


def assert_lambda_function_url(
    arn: str,
    *,
    auth_type: str | None = None,
    cors: bool | None = None,
) -> None:
    """Assert a Lambda function URL exists and has expected properties."""
    client = _boto3_session().client("lambda")
    resp = client.get_function_url_config(FunctionName=arn)

    if auth_type is not None:
        actual = resp["AuthType"]
        assert actual == auth_type, f"Expected auth type '{auth_type}', got '{actual}'"

    if cors is not None:
        has_cors = "Cors" in resp and bool(resp["Cors"])
        assert has_cors == cors, f"Expected cors={cors}, got config: {resp.get('Cors')}"


def assert_lambda_layer(
    version_arn: str,
    *,
    compatible_runtimes: list[str] | None = None,
) -> None:
    """Assert a Lambda layer version exists and has expected properties."""
    client = _boto3_session().client("lambda")
    resp = client.get_layer_version_by_arn(Arn=version_arn)

    if compatible_runtimes is not None:
        actual = set(resp.get("CompatibleRuntimes", []))
        expected = set(compatible_runtimes)
        assert expected.issubset(actual), f"Expected runtimes {expected} to be in {actual}"


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


def assert_eventbridge_target(
    rule_arn: str,
    *,
    input_payload: dict | None = None,
) -> None:
    """Assert an EventBridge target exists and has expected properties."""
    rule_name = rule_arn.split("/", 1)[1]

    client = _boto3_session().client("events")
    resp = client.list_targets_by_rule(Rule=rule_name)
    targets = resp["Targets"]

    assert len(targets) >= 1, f"Expected at least 1 target, got {len(targets)}"

    if input_payload is not None:
        target = targets[0]
        actual = json.loads(target.get("Input", "null"))
        assert actual == input_payload, f"Expected target input {input_payload}, got {actual}"


def assert_event_source_mapping(
    function_arn: str,
    *,
    event_source_arn: str,
    batch_size: int | None = None,
    state: str = "Enabled",
    has_filter_criteria: bool | None = None,
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

    if has_filter_criteria is not None:
        filters = mapping.get("FilterCriteria", {}).get("Filters", [])
        has_filters = len(filters) > 0
        assert has_filters == has_filter_criteria, (
            f"Expected has_filter_criteria={has_filter_criteria}, got filters: {filters}"
        )


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


def assert_lambda_role_permissions(
    role_name: str,
    *,
    expected_actions: list[str],
) -> None:
    """Assert that a Lambda role's custom policy contains the expected IAM actions.

    Checks only Stelvio-created policies (skips AWS managed policies like
    AWSLambdaBasicExecutionRole).
    """
    iam = _boto3_session().client("iam")

    resp = iam.list_attached_role_policies(RoleName=role_name)
    policies = resp["AttachedPolicies"]

    # Skip AWS managed policies (account field is "aws" not a number)
    custom_policies = [p for p in policies if ":aws:policy/" not in p["PolicyArn"]]
    assert custom_policies, (
        f"No custom policies found on role '{role_name}'. "
        f"Attached: {[p['PolicyArn'] for p in policies]}"
    )

    all_actions: set[str] = set()
    for policy in custom_policies:
        policy_resp = iam.get_policy(PolicyArn=policy["PolicyArn"])
        version_id = policy_resp["Policy"]["DefaultVersionId"]

        version_resp = iam.get_policy_version(
            PolicyArn=policy["PolicyArn"],
            VersionId=version_id,
        )
        document = version_resp["PolicyVersion"]["Document"]

        for statement in document.get("Statement", []):
            actions = statement.get("Action", [])
            if isinstance(actions, str):
                actions = [actions]
            all_actions.update(actions)

    missing = set(expected_actions) - all_actions
    assert not missing, (
        f"Missing IAM actions: {sorted(missing)}. Actual actions: {sorted(all_actions)}"
    )


def assert_api_routes(
    api_id: str,
    *,
    expected_routes: dict[str, list[str]],
) -> None:
    """Assert an API Gateway has expected paths and methods.

    Args:
        api_id: The REST API ID.
        expected_routes: Dict of path → list of HTTP methods, e.g.
            {"/hello": ["GET"], "/items": ["GET", "POST"]}
    """
    client = _boto3_session().client("apigateway")
    resp = client.get_resources(restApiId=api_id)

    # Build actual routes map: path → set of methods (excluding OPTIONS)
    actual_routes: dict[str, set[str]] = {}
    for resource in resp["items"]:
        path = resource["path"]
        methods = set(resource.get("resourceMethods", {}).keys()) - {"OPTIONS"}
        if methods:
            actual_routes[path] = methods

    expected_paths = set(expected_routes.keys())
    assert actual_routes.keys() == expected_paths, (
        f"Route mismatch. Expected paths: {expected_paths}, "
        f"actual paths: {set(actual_routes.keys())}"
    )

    for path, expected_methods in expected_routes.items():
        assert actual_routes[path] == set(expected_methods), (
            f"Method mismatch on '{path}'. Expected: {set(expected_methods)}, "
            f"got: {actual_routes[path]}"
        )


def assert_api_cors_headers(invoke_url: str, path: str = "/") -> None:
    """Assert an API Gateway returns CORS headers on OPTIONS request."""
    url = invoke_url.rstrip("/") + path
    if not url.startswith("https://"):
        raise ValueError(f"Expected HTTPS URL, got: {url}")
    req = urllib.request.Request(url, method="OPTIONS")  # noqa: S310
    req.add_header("Origin", "https://example.com")
    req.add_header("Access-Control-Request-Method", "GET")

    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
        headers = dict(resp.headers)
        assert "Access-Control-Allow-Origin" in headers, (
            f"Missing Access-Control-Allow-Origin header. Headers: {headers}"
        )
        assert "Access-Control-Allow-Methods" in headers, (
            f"Missing Access-Control-Allow-Methods header. Headers: {headers}"
        )


def assert_api_authorizers(
    api_id: str,
    *,
    expected_types: list[str],
) -> None:
    """Assert an API Gateway has authorizers with expected types.

    Args:
        api_id: The REST API ID.
        expected_types: List of authorizer types, e.g. ["TOKEN", "REQUEST"].
    """
    client = _boto3_session().client("apigateway")
    resp = client.get_authorizers(restApiId=api_id)
    actual_types = sorted(a["type"] for a in resp["items"])
    expected_sorted = sorted(expected_types)
    assert actual_types == expected_sorted, (
        f"Expected authorizer types {expected_sorted}, got {actual_types}"
    )


def assert_api_method_auth(
    api_id: str,
    *,
    path: str,
    method: str,
    auth_type: str,
) -> None:
    """Assert a specific API Gateway method has the expected authorization type.

    Args:
        api_id: The REST API ID.
        path: The resource path, e.g. "/protected".
        method: The HTTP method, e.g. "GET".
        auth_type: Expected authorization type: "NONE", "AWS_IAM", or "CUSTOM".
    """
    client = _boto3_session().client("apigateway")
    resources = client.get_resources(restApiId=api_id)

    resource_id = None
    for resource in resources["items"]:
        if resource["path"] == path:
            resource_id = resource["id"]
            break
    assert resource_id is not None, (
        f"Path '{path}' not found. Available: {[r['path'] for r in resources['items']]}"
    )

    resp = client.get_method(restApiId=api_id, resourceId=resource_id, httpMethod=method)
    actual = resp["authorizationType"]
    assert actual == auth_type, (
        f"Expected auth type '{auth_type}' on {method} {path}, got '{actual}'"
    )
