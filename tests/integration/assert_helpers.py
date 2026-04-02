import json
import os
import time
import urllib.request

import boto3


def _boto3_session(region: str | None = None) -> boto3.Session:
    return boto3.Session(
        profile_name=os.environ.get("STLV_TEST_AWS_PROFILE"),
        region_name=region or os.environ.get("STLV_TEST_AWS_REGION", "us-east-1"),
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


def assert_dynamo_tags(arn: str, expected_tags: dict[str, str]) -> None:
    """Assert a DynamoDB table has the expected tags."""
    client = _boto3_session().client("dynamodb")
    response = client.list_tags_of_resource(ResourceArn=arn)
    tags = {t["Key"]: t["Value"] for t in response["Tags"]}
    _assert_expected_tags(tags, expected_tags, resource_label="DynamoDB table")


def _assert_expected_tags(
    actual_tags: dict[str, str],
    expected_tags: dict[str, str],
    *,
    resource_label: str,
) -> None:
    for key, value in expected_tags.items():
        assert key in actual_tags, (
            f"Tag '{key}' not found on {resource_label}. Tags: {actual_tags}"
        )
        assert actual_tags[key] == value, (
            f"Tag '{key}' on {resource_label}: expected '{value}', got '{actual_tags[key]}'"
        )


def find_acm_certificate(resources: list[dict]) -> dict:
    """Find the ACM Certificate resource in exported Pulumi resources."""
    matches = [
        resource for resource in resources if "aws:acm/certificate:Certificate" in resource["type"]
    ]
    matches = [resource for resource in matches if "Validation" not in resource["type"]]
    urns = [resource["urn"] for resource in matches]
    assert len(matches) == 1, f"Expected 1 ACM certificate, got {len(matches)}: {urns}"
    return matches[0]


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

    if dlq_arn is not None or dlq_retry is not None:
        redrive = json.loads(attrs.get("RedrivePolicy", "{}"))

        if dlq_arn is not None:
            actual = redrive.get("deadLetterTargetArn")
            assert actual == dlq_arn, f"Expected DLQ ARN '{dlq_arn}', got '{actual}'"

        if dlq_retry is not None:
            actual = redrive.get("maxReceiveCount")
            assert actual == dlq_retry, f"Expected DLQ retry {dlq_retry}, got {actual}"


def assert_sqs_tags(url: str, expected_tags: dict[str, str], *, region: str | None = None) -> None:
    """Assert an SQS queue has the expected tags."""
    client = _boto3_session(region).client("sqs")
    tags = client.list_queue_tags(QueueUrl=url).get("Tags", {})
    _assert_expected_tags(tags, expected_tags, resource_label="SQS queue")


def assert_lambda_tags(arn: str, expected_tags: dict[str, str]) -> None:
    """Assert a Lambda function has the expected tags."""
    client = _boto3_session().client("lambda")
    tags = client.list_tags(Resource=arn).get("Tags", {})
    _assert_expected_tags(tags, expected_tags, resource_label="Lambda function")


def assert_sns_tags(arn: str, expected_tags: dict[str, str]) -> None:
    """Assert an SNS topic has the expected tags."""
    client = _boto3_session().client("sns")
    response = client.list_tags_for_resource(ResourceArn=arn)
    tags = {t["Key"]: t["Value"] for t in response.get("Tags", [])}
    _assert_expected_tags(tags, expected_tags, resource_label="SNS topic")


def assert_s3_bucket_tags(name: str, expected_tags: dict[str, str]) -> None:
    """Assert an S3 bucket has the expected tags."""
    client = _boto3_session().client("s3")
    response = client.get_bucket_tagging(Bucket=name)
    tags = {t["Key"]: t["Value"] for t in response.get("TagSet", [])}
    _assert_expected_tags(tags, expected_tags, resource_label="S3 bucket")


def assert_eventbridge_tags(arn: str, expected_tags: dict[str, str]) -> None:
    """Assert an EventBridge rule has the expected tags."""
    client = _boto3_session().client("events")
    response = client.list_tags_for_resource(ResourceARN=arn)
    tags = {t["Key"]: t["Value"] for t in response.get("Tags", [])}
    _assert_expected_tags(tags, expected_tags, resource_label="EventBridge rule")


def assert_apigateway_tags(arn: str, expected_tags: dict[str, str]) -> None:
    """Assert an API Gateway REST API has the expected tags."""
    client = _boto3_session().client("apigateway")
    tags = client.get_tags(resourceArn=arn).get("tags", {})
    _assert_expected_tags(tags, expected_tags, resource_label="API Gateway REST API")


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
    has_filter_policy: bool | None = None,
    raw_message_delivery: bool | None = None,
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

    sub_arn = matching[0]["SubscriptionArn"]
    if has_filter_policy is not None or raw_message_delivery is not None:
        attrs = client.get_subscription_attributes(SubscriptionArn=sub_arn)["Attributes"]

        if has_filter_policy is not None:
            policy = attrs.get("FilterPolicy")
            has_policy = policy is not None and policy != "{}"
            assert has_policy == has_filter_policy, (
                f"Expected has_filter_policy={has_filter_policy}, got policy: {policy}"
            )

        if raw_message_delivery is not None:
            actual = attrs.get("RawMessageDelivery", "false") == "true"
            assert actual == raw_message_delivery, (
                f"Expected raw_message_delivery={raw_message_delivery}, got {actual}"
            )


def assert_lambda_function(  # noqa: PLR0913
    arn: str,
    *,
    runtime: str | None = None,
    timeout: int | None = None,
    memory: int | None = None,
    environment: dict[str, str] | None = None,
    layers_count: int | None = None,
    architecture: str | None = None,
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

    if architecture is not None:
        actual = config.get("Architectures", ["x86_64"])[0]
        assert actual == architecture, f"Expected architecture '{architecture}', got '{actual}'"


def assert_lambda_function_url(
    arn: str,
    *,
    auth_type: str | None = None,
    cors: bool | None = None,
    cors_origins: list[str] | None = None,
    invoke_mode: str | None = None,
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

    if cors_origins is not None:
        actual = resp.get("Cors", {}).get("AllowOrigins", [])
        assert set(actual) == set(cors_origins), (
            f"Expected CORS origins {cors_origins}, got {actual}"
        )

    if invoke_mode is not None:
        actual = resp.get("InvokeMode", "BUFFERED")
        assert actual == invoke_mode, f"Expected invoke mode '{invoke_mode}', got '{actual}'"


def assert_lambda_layer(
    version_arn: str,
    *,
    compatible_runtimes: list[str] | None = None,
    compatible_architectures: list[str] | None = None,
) -> None:
    """Assert a Lambda layer version exists and has expected properties."""
    client = _boto3_session().client("lambda")
    resp = client.get_layer_version_by_arn(Arn=version_arn)

    if compatible_runtimes is not None:
        actual = set(resp.get("CompatibleRuntimes", []))
        expected = set(compatible_runtimes)
        assert actual == expected, f"Expected runtimes {expected}, got {actual}"

    if compatible_architectures is not None:
        actual = set(resp.get("CompatibleArchitectures", []))
        expected = set(compatible_architectures)
        assert actual == expected, f"Expected architectures {expected}, got {actual}"


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
    has_filter: bool | None = None,
) -> None:
    """Assert S3 bucket notification configuration.

    has_filter checks whether any notification configuration has key filter rules
    (prefix/suffix).
    """
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

    if has_filter is not None:
        all_configs = [
            *resp.get("LambdaFunctionConfigurations", []),
            *resp.get("QueueConfigurations", []),
            *resp.get("TopicConfigurations", []),
        ]
        filter_rules = [
            rule
            for cfg in all_configs
            for rule in cfg.get("Filter", {}).get("Key", {}).get("FilterRules", [])
        ]
        actual_has_filter = len(filter_rules) > 0
        assert actual_has_filter == has_filter, (
            f"Expected has_filter={has_filter}, got filter rules: {filter_rules}"
        )


def assert_lambda_role_permissions(
    role_name: str,
    *,
    expected_actions: list[str] | None = None,
    forbidden_actions: list[str] | None = None,
) -> None:
    """Assert that a Lambda role's custom policy contains/excludes expected IAM actions.

    Checks only Stelvio-created policies (skips AWS managed policies like
    AWSLambdaBasicExecutionRole).

    Args:
        role_name: The IAM role name to check.
        expected_actions: Actions that must be present in the policy.
        forbidden_actions: Actions that must NOT be present in the policy.
    """
    iam = _boto3_session().client("iam")

    resp = iam.list_attached_role_policies(RoleName=role_name)
    policies = resp["AttachedPolicies"]

    # Skip AWS managed policies (ARN contains ":aws:policy/" vs numeric account ID)
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

    if expected_actions is not None:
        missing = set(expected_actions) - all_actions
        assert not missing, (
            f"Missing IAM actions: {sorted(missing)}. Actual actions: {sorted(all_actions)}"
        )

    if forbidden_actions is not None:
        unexpected = set(forbidden_actions) & all_actions
        assert not unexpected, (
            f"Forbidden IAM actions found: {sorted(unexpected)}. "
            f"Actual actions: {sorted(all_actions)}"
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


def invoke_lambda(arn: str, payload: dict | None = None) -> dict:
    """Invoke a Lambda function and return the parsed response payload."""
    client = _boto3_session().client("lambda")
    kwargs: dict = {"FunctionName": arn}
    if payload is not None:
        kwargs["Payload"] = json.dumps(payload)
    resp = client.invoke(**kwargs)

    result = json.loads(resp["Payload"].read())

    if "FunctionError" in resp:
        raise AssertionError(f"Lambda invocation failed: {result}")

    return result


# --- Action helpers (trigger events) ---


def drain_sqs(queue_url: str) -> None:
    """Drain all messages from an SQS queue (e.g., S3 test events)."""
    client = _boto3_session().client("sqs")
    while True:
        resp = client.receive_message(
            QueueUrl=queue_url, MaxNumberOfMessages=10, WaitTimeSeconds=1
        )
        messages = resp.get("Messages", [])
        if not messages:
            break
        for msg in messages:
            client.delete_message(QueueUrl=queue_url, ReceiptHandle=msg["ReceiptHandle"])


def send_sqs_message(queue_url: str, body: dict) -> str:
    client = _boto3_session().client("sqs")
    resp = client.send_message(QueueUrl=queue_url, MessageBody=json.dumps(body))
    return resp["MessageId"]


def publish_sns_message(topic_arn: str, message: dict) -> str:
    client = _boto3_session().client("sns")
    resp = client.publish(TopicArn=topic_arn, Message=json.dumps(message))
    return resp["MessageId"]


def upload_s3_object(bucket_name: str, key: str, body: str | bytes) -> None:
    client = _boto3_session().client("s3")
    if isinstance(body, str):
        body = body.encode()
    client.put_object(Bucket=bucket_name, Key=key, Body=body)


def put_dynamo_item(table_name: str, item: dict) -> None:
    table = _boto3_session().resource("dynamodb").Table(table_name)
    table.put_item(Item=item)


def http_request(
    url: str,
    method: str = "GET",
    *,
    body: dict | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 30,
) -> tuple[int, str]:
    """Returns (status_code, response_body). Handles HTTP errors without raising."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)  # noqa: S310
    req.add_header("Content-Type", "application/json")
    if headers:
        for key, value in headers.items():
            req.add_header(key, value)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode() if e.fp else ""


# --- Polling helpers (wait for async results) ---


def poll_dynamo_items(
    table_name: str,
    *,
    timeout: int = 60,
    min_items: int = 1,
) -> list[dict]:
    """Poll a DynamoDB table until at least min_items exist or timeout."""
    table = _boto3_session().resource("dynamodb").Table(table_name)
    deadline = time.monotonic() + timeout
    items = []

    while time.monotonic() < deadline:
        items = table.scan().get("Items", [])
        if len(items) >= min_items:
            return items
        time.sleep(2)

    raise AssertionError(
        f"Timed out after {timeout}s waiting for {min_items} item(s) in '{table_name}'. "
        f"Found {len(items)}."
    )


def poll_sqs_messages(
    queue_url: str,
    *,
    timeout: int = 60,
    min_messages: int = 1,
) -> list[dict]:
    """Poll an SQS queue, deleting messages as they're read to avoid re-processing."""
    client = _boto3_session().client("sqs")
    deadline = time.monotonic() + timeout
    collected: list[dict] = []

    while time.monotonic() < deadline:
        resp = client.receive_message(
            QueueUrl=queue_url,
            MaxNumberOfMessages=10,
            WaitTimeSeconds=5,
        )
        for msg in resp.get("Messages", []):
            collected.append(json.loads(msg["Body"]))
            client.delete_message(QueueUrl=queue_url, ReceiptHandle=msg["ReceiptHandle"])
        if len(collected) >= min_messages:
            return collected

    raise AssertionError(
        f"Timed out after {timeout}s waiting for {min_messages} message(s) in queue. "
        f"Received {len(collected)}."
    )


# --- CloudFront / SES assertion helpers ---


def assert_acm_certificate(
    domain_name: str,
    *,
    status: str | None = None,
    validation_method: str | None = None,
    key_algorithm: str | None = None,
    region: str | None = None,
) -> None:
    """Assert an ACM certificate exists for the domain and has expected properties.

    Args:
        domain_name: The domain name to find the certificate for.
        status: Expected status: "ISSUED", "PENDING_VALIDATION", etc.
        validation_method: Expected method: "DNS" or "EMAIL".
        key_algorithm: Expected key algorithm: "RSA-2048", "EC_prime256v1", etc.
        region: AWS region to query. Defaults to STLV_TEST_AWS_REGION.
    """
    client = _boto3_session(region).client("acm")

    cert_arn = None
    paginator = client.get_paginator("list_certificates")
    for page in paginator.paginate():
        for cert_summary in page["CertificateSummaryList"]:
            if cert_summary["DomainName"] == domain_name:
                cert_arn = cert_summary["CertificateArn"]
                break
        if cert_arn:
            break

    assert cert_arn is not None, f"No ACM certificate found for domain '{domain_name}'"

    resp = client.describe_certificate(CertificateArn=cert_arn)
    cert = resp["Certificate"]

    if status is not None:
        actual = cert["Status"]
        assert actual == status, f"Expected certificate status '{status}', got '{actual}'"

    if validation_method is not None:
        actual = cert.get("DomainValidationOptions", [{}])[0].get("ValidationMethod")
        assert actual == validation_method, (
            f"Expected validation method '{validation_method}', got '{actual}'"
        )

    if key_algorithm is not None:
        actual = cert.get("KeyAlgorithm")
        assert actual == key_algorithm, f"Expected key algorithm '{key_algorithm}', got '{actual}'"


def assert_acm_tags(
    cert_arn: str,
    expected_tags: dict[str, str],
    *,
    region: str | None = None,
) -> None:
    """Assert an ACM certificate has the expected tags.

    Args:
        cert_arn: The certificate ARN.
        expected_tags: Expected tag key-value pairs.
        region: AWS region to query. Needed when the cert is in a different
            region than the default test region (e.g. us-east-1 for CloudFront certs).
    """
    client = _boto3_session(region).client("acm")
    response = client.list_tags_for_certificate(CertificateArn=cert_arn)
    tags = {t["Key"]: t["Value"] for t in response["Tags"]}
    _assert_expected_tags(tags, expected_tags, resource_label="ACM certificate")


def assert_cloudfront_tags(distribution_arn: str, expected_tags: dict[str, str]) -> None:
    """Assert a CloudFront distribution has the expected tags."""
    client = _boto3_session().client("cloudfront")
    response = client.list_tags_for_resource(Resource=distribution_arn)
    tags = {t["Key"]: t["Value"] for t in response["Tags"]["Items"]}
    _assert_expected_tags(tags, expected_tags, resource_label="CloudFront distribution")


def assert_cloudfront_tags_by_distribution_id(
    distribution_id: str, expected_tags: dict[str, str]
) -> None:
    """Assert CloudFront tags when only distribution id is available."""
    client = _boto3_session().client("cloudfront")
    distribution = client.get_distribution(Id=distribution_id)["Distribution"]
    assert_cloudfront_tags(distribution["ARN"], expected_tags)


def assert_cloudfront_distribution(  # noqa: PLR0913
    distribution_id: str,
    *,
    enabled: bool | None = None,
    aliases: list[str] | None = None,
    price_class: str | None = None,
    origins_count: int | None = None,
    default_certificate: bool | None = None,
    ssl_support_method: str | None = None,
    minimum_protocol_version: str | None = None,
    acm_certificate_domain: str | None = None,
) -> None:
    """Assert a CloudFront distribution exists and has expected properties."""
    client = _boto3_session().client("cloudfront")
    resp = client.get_distribution(Id=distribution_id)
    config = resp["Distribution"]["DistributionConfig"]

    if enabled is not None:
        actual = config["Enabled"]
        assert actual == enabled, f"Expected enabled={enabled}, got {actual}"

    if aliases is not None:
        actual_aliases = config.get("Aliases", {}).get("Items", [])
        assert set(actual_aliases) == set(aliases), (
            f"Expected aliases {aliases}, got {actual_aliases}"
        )

    if price_class is not None:
        actual = config["PriceClass"]
        assert actual == price_class, f"Expected price_class '{price_class}', got '{actual}'"

    if origins_count is not None:
        actual = config["Origins"]["Quantity"]
        assert actual == origins_count, f"Expected {origins_count} origins, got {actual}"

    viewer_cert = config["ViewerCertificate"]

    if default_certificate is not None:
        actual = viewer_cert.get("CloudFrontDefaultCertificate", False)
        assert actual == default_certificate, (
            f"Expected default_certificate={default_certificate}, got {actual}"
        )

    if ssl_support_method is not None:
        actual = viewer_cert.get("SSLSupportMethod")
        assert actual == ssl_support_method, (
            f"Expected ssl_support_method '{ssl_support_method}', got '{actual}'"
        )

    if minimum_protocol_version is not None:
        actual = viewer_cert.get("MinimumProtocolVersion")
        assert actual == minimum_protocol_version, (
            f"Expected minimum_protocol_version '{minimum_protocol_version}', got '{actual}'"
        )

    if acm_certificate_domain is not None:
        cert_arn = viewer_cert.get("ACMCertificateArn")
        assert cert_arn is not None, "Expected ACM certificate ARN in ViewerCertificate, got None"
        acm_client = _boto3_session().client("acm")
        cert_resp = acm_client.describe_certificate(CertificateArn=cert_arn)
        actual_domain = cert_resp["Certificate"]["DomainName"]
        assert actual_domain == acm_certificate_domain, (
            f"Expected ACM certificate for domain '{acm_certificate_domain}', "
            f"got '{actual_domain}'"
        )


def assert_ses_identity(
    identity: str,
    *,
    identity_type: str | None = None,
    configuration_set_name: str | None = None,
    dkim_status: str | None = None,
    verified_for_sending: bool | None = None,
) -> None:
    """Assert an SES email identity exists and has expected properties.

    Args:
        identity: The email address or domain.
        identity_type: Expected type: "EMAIL_ADDRESS" or "DOMAIN".
        configuration_set_name: Expected associated configuration set name.
        dkim_status: Expected DKIM status: "SUCCESS", "PENDING", "FAILED", etc.
        verified_for_sending: Whether the identity is verified for sending.
    """
    client = _boto3_session().client("sesv2")
    resp = client.get_email_identity(EmailIdentity=identity)

    if identity_type is not None:
        actual_type = resp["IdentityType"]
        assert actual_type == identity_type, (
            f"Expected identity type '{identity_type}', got '{actual_type}'"
        )

    if configuration_set_name is not None:
        actual = resp.get("ConfigurationSetName")
        assert actual == configuration_set_name, (
            f"Expected configuration set '{configuration_set_name}', got '{actual}'"
        )

    if dkim_status is not None:
        actual = resp.get("DkimAttributes", {}).get("Status")
        assert actual == dkim_status, f"Expected DKIM status '{dkim_status}', got '{actual}'"

    if verified_for_sending is not None:
        actual = resp.get("VerifiedForSendingStatus", False)
        assert actual == verified_for_sending, (
            f"Expected verified_for_sending={verified_for_sending}, got {actual}"
        )


def assert_ses_configuration_set(name: str) -> None:
    """Assert an SES configuration set exists."""
    client = _boto3_session().client("sesv2")
    client.get_configuration_set(ConfigurationSetName=name)


def assert_ses_tags(resource_arn: str, expected_tags: dict[str, str]) -> None:
    """Assert an SES v2 resource has the expected tags."""
    client = _boto3_session().client("sesv2")
    response = client.list_tags_for_resource(ResourceArn=resource_arn)
    tags = {t["Key"]: t["Value"] for t in response.get("Tags", [])}
    _assert_expected_tags(tags, expected_tags, resource_label="SES resource")


def wait_for_event_source_mapping(
    function_name_or_arn: str,
    *,
    timeout: int = 120,
) -> None:
    """Wait until all event source mappings for a function are in 'Enabled' state.

    DynamoDB Stream and SQS event source mappings go through Creating → Enabling →
    Enabled states. With starting_position=LATEST, items written before the ESM is
    active are silently missed.
    """
    client = _boto3_session().client("lambda")
    deadline = time.monotonic() + timeout
    mappings = []

    while time.monotonic() < deadline:
        resp = client.list_event_source_mappings(FunctionName=function_name_or_arn)
        mappings = resp["EventSourceMappings"]
        if mappings and all(m["State"] == "Enabled" for m in mappings):
            return
        time.sleep(5)

    states = [(m.get("EventSourceArn", "?"), m["State"]) for m in mappings]
    raise AssertionError(f"Event source mapping not active after {timeout}s. States: {states}")


# --- Cognito assertion helpers ---


def assert_cognito_user_pool(  # noqa: PLR0913, C901
    pool_id: str,
    *,
    username_attributes: list[str] | None = None,
    alias_attributes: list[str] | None = None,
    auto_verified_attributes: list[str] | None = None,
    mfa_configuration: str | None = None,
    password_policy: dict | None = None,
    deletion_protection: str | None = None,
    tier: str | None = None,
    lambda_config_triggers: list[str] | None = None,
    email_sending_account: str | None = None,
) -> None:
    """Assert a Cognito User Pool exists and has expected properties."""
    client = _boto3_session().client("cognito-idp")
    resp = client.describe_user_pool(UserPoolId=pool_id)
    pool = resp["UserPool"]

    if username_attributes is not None:
        actual = sorted(pool.get("UsernameAttributes", []))
        expected = sorted(username_attributes)
        assert actual == expected, f"Expected username_attributes {expected}, got {actual}"

    if alias_attributes is not None:
        actual = sorted(pool.get("AliasAttributes", []))
        expected = sorted(alias_attributes)
        assert actual == expected, f"Expected alias_attributes {expected}, got {actual}"

    if auto_verified_attributes is not None:
        actual = sorted(pool.get("AutoVerifiedAttributes", []))
        expected = sorted(auto_verified_attributes)
        assert actual == expected, f"Expected auto_verified_attributes {expected}, got {actual}"

    if mfa_configuration is not None:
        actual_mfa = pool.get("MfaConfiguration", "OFF")
        assert actual_mfa == mfa_configuration, (
            f"Expected mfa_configuration '{mfa_configuration}', got '{actual_mfa}'"
        )

    if password_policy is not None:
        actual_pw = pool.get("Policies", {}).get("PasswordPolicy", {})
        for key, expected_val in password_policy.items():
            actual_val = actual_pw.get(key)
            assert actual_val == expected_val, (
                f"Password policy '{key}': expected {expected_val}, got {actual_val}"
            )

    if deletion_protection is not None:
        actual_dp = pool.get("DeletionProtection", "INACTIVE")
        assert actual_dp == deletion_protection, (
            f"Expected DeletionProtection '{deletion_protection}', got '{actual_dp}'"
        )

    if tier is not None:
        actual_tier = pool.get("UserPoolTier", "ESSENTIALS")
        assert actual_tier == tier, f"Expected UserPoolTier '{tier}', got '{actual_tier}'"

    if lambda_config_triggers is not None:
        lambda_cfg = pool.get("LambdaConfig", {})
        for trigger_name in lambda_config_triggers:
            # Convert snake_case trigger name to PascalCase key
            pascal_key = "".join(w.capitalize() for w in trigger_name.split("_"))
            assert lambda_cfg.get(pascal_key), (
                f"Expected trigger '{trigger_name}' (key '{pascal_key}') in LambdaConfig, "
                f"got keys: {list(lambda_cfg.keys())}"
            )

    if email_sending_account is not None:
        actual_email = pool.get("EmailConfiguration", {}).get("EmailSendingAccount")
        assert actual_email == email_sending_account, (
            f"Expected EmailSendingAccount '{email_sending_account}', got '{actual_email}'"
        )


def assert_cognito_user_pool_client(  # noqa: PLR0913
    pool_id: str,
    client_id: str,
    *,
    callback_urls: list[str] | None = None,
    logout_urls: list[str] | None = None,
    generate_secret: bool | None = None,
    supported_identity_providers: list[str] | None = None,
    allowed_oauth_flows: list[str] | None = None,
    allowed_oauth_scopes: list[str] | None = None,
) -> None:
    """Assert a Cognito User Pool Client exists and has expected properties."""
    client = _boto3_session().client("cognito-idp")
    resp = client.describe_user_pool_client(
        UserPoolId=pool_id,
        ClientId=client_id,
    )
    upc = resp["UserPoolClient"]

    if callback_urls is not None:
        actual = sorted(upc.get("CallbackURLs", []))
        expected = sorted(callback_urls)
        assert actual == expected, f"Expected callback_urls {expected}, got {actual}"

    if logout_urls is not None:
        actual = sorted(upc.get("LogoutURLs", []))
        expected = sorted(logout_urls)
        assert actual == expected, f"Expected logout_urls {expected}, got {actual}"

    if generate_secret is not None:
        has_secret = bool(upc.get("ClientSecret"))
        assert has_secret == generate_secret, (
            f"Expected generate_secret={generate_secret}, has secret={has_secret}"
        )

    if supported_identity_providers is not None:
        actual = sorted(upc.get("SupportedIdentityProviders", []))
        expected = sorted(supported_identity_providers)
        assert actual == expected, f"Expected providers {expected}, got {actual}"

    if allowed_oauth_flows is not None:
        actual = sorted(upc.get("AllowedOAuthFlows", []))
        expected = sorted(allowed_oauth_flows)
        assert actual == expected, f"Expected OAuth flows {expected}, got {actual}"

    if allowed_oauth_scopes is not None:
        actual = sorted(upc.get("AllowedOAuthScopes", []))
        expected = sorted(allowed_oauth_scopes)
        assert actual == expected, f"Expected OAuth scopes {expected}, got {actual}"


def assert_cognito_identity_provider(
    pool_id: str,
    provider_name: str,
    *,
    provider_type: str | None = None,
    provider_details: dict[str, str] | None = None,
    attribute_mapping: dict[str, str] | None = None,
) -> None:
    """Assert a Cognito Identity Provider exists and has expected properties."""
    client = _boto3_session().client("cognito-idp")
    resp = client.describe_identity_provider(
        UserPoolId=pool_id,
        ProviderName=provider_name,
    )
    idp = resp["IdentityProvider"]

    if provider_type is not None:
        actual = idp.get("ProviderType")
        assert actual == provider_type, f"Expected provider_type '{provider_type}', got '{actual}'"

    if provider_details is not None:
        actual = idp.get("ProviderDetails", {})
        for key, expected_val in provider_details.items():
            actual_val = actual.get(key)
            assert actual_val == expected_val, (
                f"Provider detail '{key}': expected '{expected_val}', got '{actual_val}'"
            )

    if attribute_mapping is not None:
        actual = idp.get("AttributeMapping", {})
        for key, expected_val in attribute_mapping.items():
            actual_val = actual.get(key)
            assert actual_val == expected_val, (
                f"Attribute mapping '{key}': expected '{expected_val}', got '{actual_val}'"
            )


def assert_cognito_tags(pool_arn: str, expected_tags: dict[str, str]) -> None:
    """Assert a Cognito User Pool has the expected tags."""
    client = _boto3_session().client("cognito-idp")
    response = client.list_tags_for_resource(ResourceArn=pool_arn)
    tags = response.get("Tags", {})
    _assert_expected_tags(tags, expected_tags, resource_label="Cognito User Pool")


def assert_cognito_user_pool_domain(
    pool_id: str,
    *,
    domain: str | None = None,
    custom_domain: str | None = None,
) -> None:
    """Assert a Cognito User Pool has the expected domain configuration.

    Args:
        pool_id: The user pool ID.
        domain: Expected prefix domain (e.g. "myapp-prod").
        custom_domain: Expected custom domain (e.g. "auth.example.com").
    """
    client = _boto3_session().client("cognito-idp")
    resp = client.describe_user_pool(UserPoolId=pool_id)
    pool = resp["UserPool"]

    if domain is not None:
        actual = pool.get("Domain", "")
        assert actual == domain, f"Expected Domain '{domain}', got '{actual}'"

    if custom_domain is not None:
        actual = pool.get("CustomDomain", "")
        assert actual == custom_domain, f"Expected CustomDomain '{custom_domain}', got '{actual}'"


def sign_up_cognito_user(
    pool_id: str,
    client_id: str,
    email: str,
    password: str,
) -> dict:
    """Sign up a user via Cognito — returns the SignUp API response."""
    client = _boto3_session().client("cognito-idp")
    return client.sign_up(
        ClientId=client_id,
        Username=email,
        Password=password,
        UserAttributes=[{"Name": "email", "Value": email}],
    )


def admin_delete_cognito_user(pool_id: str, email: str) -> None:
    """Delete a Cognito user (cleanup helper)."""
    client = _boto3_session().client("cognito-idp")
    client.admin_delete_user(UserPoolId=pool_id, Username=email)


def disable_cognito_deletion_protection(pool_id: str) -> None:
    """Disable deletion protection so the pool can be destroyed."""
    client = _boto3_session().client("cognito-idp")
    client.update_user_pool(UserPoolId=pool_id, DeletionProtection="INACTIVE")


def assert_cognito_identity_pool(
    identity_pool_id: str,
    *,
    allow_unauthenticated: bool | None = None,
) -> None:
    """Assert a Cognito Identity Pool exists and has expected properties."""
    client = _boto3_session().client("cognito-identity")
    resp = client.describe_identity_pool(IdentityPoolId=identity_pool_id)

    if allow_unauthenticated is not None:
        actual = resp.get("AllowUnauthenticatedIdentities", False)
        assert actual == allow_unauthenticated, (
            f"Expected AllowUnauthenticatedIdentities={allow_unauthenticated}, got {actual}"
        )


# --- AppSync assertion helpers ---


def assert_appsync_api(
    api_id: str,
    *,
    authentication_type: str | None = None,
    additional_auth_count: int | None = None,
    additional_auth_types: list[str] | None = None,
) -> None:
    """Assert an AppSync GraphQL API exists and has expected properties.

    Args:
        api_id: The AppSync API ID.
        authentication_type: Expected default auth type, e.g. "API_KEY", "AWS_IAM".
        additional_auth_count: Expected number of additional auth providers.
        additional_auth_types: Expected list of additional auth provider types.
    """
    client = _boto3_session().client("appsync")
    resp = client.get_graphql_api(apiId=api_id)
    api = resp["graphqlApi"]

    if authentication_type is not None:
        actual = api["authenticationType"]
        assert actual == authentication_type, (
            f"Expected authentication type '{authentication_type}', got '{actual}'"
        )

    additional = api.get("additionalAuthenticationProviders", [])

    if additional_auth_count is not None:
        actual = len(additional)
        assert actual == additional_auth_count, (
            f"Expected {additional_auth_count} additional auth providers, got {actual}"
        )

    if additional_auth_types is not None:
        actual_types = sorted(p["authenticationType"] for p in additional)
        expected_sorted = sorted(additional_auth_types)
        assert actual_types == expected_sorted, (
            f"Expected additional auth types {expected_sorted}, got {actual_types}"
        )


def assert_appsync_data_source(
    api_id: str,
    name: str,
    *,
    ds_type: str | None = None,
    has_service_role: bool | None = None,
) -> None:
    """Assert an AppSync data source exists and has expected properties.

    Args:
        api_id: The AppSync API ID.
        name: Data source name.
        ds_type: Expected type, e.g. "AWS_LAMBDA", "AMAZON_DYNAMODB", "HTTP", "NONE".
        has_service_role: Whether a service role ARN should be present.
    """
    client = _boto3_session().client("appsync")
    resp = client.get_data_source(apiId=api_id, name=name)
    ds = resp["dataSource"]

    if ds_type is not None:
        actual = ds["type"]
        assert actual == ds_type, f"Expected data source type '{ds_type}', got '{actual}'"

    if has_service_role is not None:
        has_role = bool(ds.get("serviceRoleArn"))
        assert has_role == has_service_role, (
            f"Expected has_service_role={has_service_role}, "
            f"got serviceRoleArn={ds.get('serviceRoleArn')}"
        )


def assert_appsync_resolver(  # noqa: PLR0913
    api_id: str,
    type_name: str,
    field_name: str,
    *,
    kind: str | None = None,
    data_source_name: str | None = None,
    pipeline_functions_count: int | None = None,
) -> None:
    """Assert an AppSync resolver exists and has expected properties.

    Args:
        api_id: The AppSync API ID.
        type_name: GraphQL type name, e.g. "Query", "Mutation".
        field_name: Field name, e.g. "getUser".
        kind: Expected resolver kind: "UNIT" or "PIPELINE".
        data_source_name: Expected data source name (for UNIT resolvers).
        pipeline_functions_count: Expected number of pipeline functions.
    """
    client = _boto3_session().client("appsync")
    resp = client.get_resolver(apiId=api_id, typeName=type_name, fieldName=field_name)
    resolver = resp["resolver"]

    if kind is not None:
        actual = resolver["kind"]
        assert actual == kind, f"Expected resolver kind '{kind}', got '{actual}'"

    if data_source_name is not None:
        actual = resolver.get("dataSourceName")
        assert actual == data_source_name, (
            f"Expected data source name '{data_source_name}', got '{actual}'"
        )

    if pipeline_functions_count is not None:
        functions = resolver.get("pipelineConfig", {}).get("functions", [])
        actual = len(functions)
        assert actual == pipeline_functions_count, (
            f"Expected {pipeline_functions_count} pipeline functions, got {actual}"
        )


# --- AppSync action helpers ---


def graphql_query(
    url: str,
    query: str,
    *,
    api_key: str | None = None,
    variables: dict | None = None,
    timeout: int = 30,
) -> dict:
    """Execute a GraphQL query against an AppSync API.

    Args:
        url: AppSync GraphQL endpoint URL.
        query: GraphQL query/mutation string.
        api_key: API key for authentication (x-api-key header).
        variables: Optional GraphQL variables dict.
        timeout: Request timeout in seconds.

    Returns:
        Parsed JSON response dict (contains "data" and/or "errors").
    """
    body: dict = {"query": query}
    if variables is not None:
        body["variables"] = variables

    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST")  # noqa: S310
    req.add_header("Content-Type", "application/json")
    if api_key is not None:
        req.add_header("x-api-key", api_key)

    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read())
