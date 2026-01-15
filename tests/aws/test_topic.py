import json

import pulumi
import pytest
from pulumi.runtime import set_mocks

from stelvio.aws.function import FunctionConfig
from stelvio.aws.queue import Queue
from stelvio.aws.topic import Topic

from .pulumi_mocks import ACCOUNT_ID, DEFAULT_REGION, PulumiTestMocks

# Test prefix
TP = "test-test-"

# Filter policy constants
FILTER_POLICY_ORDER_SHIPMENT = {"type": ["order", "shipment"]}
FILTER_POLICY_PRIORITY_HIGH = {"priority": ["high"]}

# ARN templates
TOPIC_ARN_TEMPLATE = f"arn:aws:sns:{DEFAULT_REGION}:{ACCOUNT_ID}:{{name}}"


@pytest.fixture
def pulumi_mocks():
    mocks = PulumiTestMocks()
    set_mocks(mocks)
    return mocks


# Assertion helpers


def assert_field_not_set_or_none(inputs: dict, field: str) -> None:
    """Assert field is either not present in inputs OR is explicitly None."""
    if field in inputs:
        assert inputs[field] is None, f"Field '{field}' should be None but was {inputs[field]}"


def assert_subscription_filter(
    pulumi_mocks, subscription_name: str, expected_filter: dict | None
) -> None:
    """Verify subscription has expected filter policy."""
    subs = pulumi_mocks.created_topic_subscriptions(subscription_name)
    assert len(subs) == 1, f"Expected 1 subscription named '{subscription_name}'"

    actual_filter_policy = subs[0].inputs.get("filterPolicy")

    if expected_filter is None:
        assert actual_filter_policy is None, (
            f"Expected no filter policy, but got: {actual_filter_policy}"
        )
    else:
        assert actual_filter_policy is not None, "Expected filter policy but got None"
        actual_filter = json.loads(actual_filter_policy)
        assert actual_filter == expected_filter


def assert_queue_policy_statement(policy_doc: dict, expected_queue_arn: str) -> None:
    """Verify queue policy statement matches expected structure exactly."""
    assert policy_doc["Version"] == "2012-10-17"
    assert len(policy_doc["Statement"]) == 1

    statement = policy_doc["Statement"][0]

    # Validate ALL required fields explicitly
    assert statement["Effect"] == "Allow"
    assert statement["Principal"] == {"Service": "sns.amazonaws.com"}
    assert statement["Action"] == "sqs:SendMessage"
    assert statement["Resource"] == expected_queue_arn
    assert statement["Condition"] == {"StringEquals": {"aws:SourceAccount": ACCOUNT_ID}}

    # Ensure no extra fields exist
    expected_keys = {"Effect", "Principal", "Action", "Resource", "Condition"}
    assert set(statement.keys()) == expected_keys, (
        f"Statement has unexpected fields. Expected: {expected_keys}, Got: {set(statement.keys())}"
    )


# Topic creation tests


@pulumi.runtime.test
def test_topic_creates_sns_topic(pulumi_mocks, project_cwd):
    topic = Topic("notifications")

    def check_resources(_):
        topic_name = f"{TP}notifications"
        topics = pulumi_mocks.created_topics(topic_name)
        assert len(topics) == 1
        t = topics[0]
        assert t.typ == "aws:sns/topic:Topic"
        assert t.inputs["name"] == topic_name
        assert_field_not_set_or_none(t.inputs, "fifoTopic")

    topic.resources.topic.arn.apply(check_resources)


@pulumi.runtime.test
def test_topic_fifo_creates_fifo_topic(pulumi_mocks, project_cwd):
    topic = Topic("orders", fifo=True)

    def check_resources(_):
        topic_name = f"{TP}orders.fifo"
        topics = pulumi_mocks.created_topics(topic_name)
        assert len(topics) == 1
        t = topics[0]
        assert t.typ == "aws:sns/topic:Topic"
        assert t.inputs["name"] == topic_name
        assert t.inputs["fifoTopic"] is True
        assert t.inputs["contentBasedDeduplication"] is True

    topic.resources.topic.arn.apply(check_resources)


@pulumi.runtime.test
def test_topic_fifo_suffix_not_duplicated(pulumi_mocks, project_cwd):
    topic = Topic("orders.fifo", fifo=True)

    def check_resources(_):
        topic_name = f"{TP}orders.fifo"
        topics = pulumi_mocks.created_topics(topic_name)
        assert len(topics) == 1
        assert topics[0].inputs["name"] == topic_name

    topic.resources.topic.arn.apply(check_resources)


@pulumi.runtime.test
def test_topic_properties(pulumi_mocks, project_cwd):
    topic = Topic("notifications")

    def check_properties(args):
        arn, name = args
        expected_name = f"{TP}notifications-test-name"
        assert arn == TOPIC_ARN_TEMPLATE.format(name=expected_name)
        assert name == expected_name

    pulumi.Output.all(topic.arn, topic.topic_name).apply(check_properties)


@pulumi.runtime.test
def test_topic_fifo_properties(pulumi_mocks, project_cwd):
    topic = Topic("orders", fifo=True)

    def check_properties(args):
        arn, name = args
        expected_name = f"{TP}orders.fifo-test-name"
        assert arn == TOPIC_ARN_TEMPLATE.format(name=expected_name)
        assert name == expected_name

    pulumi.Output.all(topic.arn, topic.topic_name).apply(check_properties)


# Lambda subscription tests


@pulumi.runtime.test
def test_topic_subscribe_creates_lambda_subscription(pulumi_mocks, project_cwd):
    topic = Topic("notifications")
    sub = topic.subscribe("handler", "functions/simple.handler")

    def check_resources(args):
        topic_arn, fn_arn, _ = args
        sub_name = f"{TP}notifications-handler-subscription"
        subs = pulumi_mocks.created_topic_subscriptions(sub_name)
        assert len(subs) == 1
        s = subs[0]
        assert s.typ == "aws:sns/topicSubscription:TopicSubscription"
        assert s.inputs["protocol"] == "lambda"
        assert s.inputs["topic"] == topic_arn
        assert s.inputs["endpoint"] == fn_arn
        assert_field_not_set_or_none(s.inputs, "filterPolicy")
        assert_field_not_set_or_none(s.inputs, "rawMessageDelivery")

    pulumi.Output.all(
        topic.arn,
        sub.resources.function.resources.function.arn,
        sub.resources.subscription.arn,
    ).apply(check_resources)


@pulumi.runtime.test
def test_topic_subscribe_creates_lambda_function(pulumi_mocks, project_cwd):
    topic = Topic("notifications")
    sub = topic.subscribe("handler", "functions/simple.handler")

    def check_resources(_):
        fn_name = f"{TP}notifications-handler"
        functions = pulumi_mocks.created_functions(fn_name)
        assert len(functions) == 1
        fn = functions[0]
        assert fn.typ == "aws:lambda/function:Function"
        assert fn.inputs["handler"] == "simple.handler"

    sub.resources.function.resources.function.arn.apply(check_resources)


@pulumi.runtime.test
def test_topic_subscribe_creates_lambda_permission(pulumi_mocks, project_cwd):
    topic = Topic("notifications")
    sub = topic.subscribe("handler", "functions/simple.handler")

    def check_resources(args):
        topic_arn, fn_name, _ = args
        perm_name = f"{TP}notifications-handler-subscription-perm"
        permissions = pulumi_mocks.created_permissions(perm_name)
        assert len(permissions) == 1
        perm = permissions[0]
        assert perm.typ == "aws:lambda/permission:Permission"
        assert perm.inputs["action"] == "lambda:InvokeFunction"
        assert perm.inputs["principal"] == "sns.amazonaws.com"
        assert perm.inputs["sourceArn"] == topic_arn
        assert perm.inputs["function"] == fn_name

        expected_keys = {"action", "principal", "sourceArn", "function"}
        assert set(perm.inputs.keys()) == expected_keys

    pulumi.Output.all(
        topic.arn,
        sub.resources.function.function_name,
        sub.resources.permission.id,
    ).apply(check_resources)


@pulumi.runtime.test
def test_topic_subscribe_with_function_config(pulumi_mocks, project_cwd):
    topic = Topic("notifications")
    config = FunctionConfig(handler="functions/simple.handler", memory=512, timeout=60)
    sub = topic.subscribe("handler", config)

    def check_resources(_):
        fn_name = f"{TP}notifications-handler"
        functions = pulumi_mocks.created_functions(fn_name)
        assert len(functions) == 1
        fn = functions[0]
        assert fn.typ == "aws:lambda/function:Function"
        assert fn.inputs["memorySize"] == 512
        assert fn.inputs["timeout"] == 60

    sub.resources.function.resources.function.arn.apply(check_resources)


@pulumi.runtime.test
def test_topic_subscribe_with_function_options(pulumi_mocks, project_cwd):
    topic = Topic("notifications")
    sub = topic.subscribe("handler", "functions/simple.handler", memory=256, timeout=30)

    def check_resources(_):
        fn_name = f"{TP}notifications-handler"
        functions = pulumi_mocks.created_functions(fn_name)
        assert len(functions) == 1
        fn = functions[0]
        assert fn.typ == "aws:lambda/function:Function"
        assert fn.inputs["memorySize"] == 256
        assert fn.inputs["timeout"] == 30

    sub.resources.function.resources.function.arn.apply(check_resources)


@pulumi.runtime.test
def test_topic_subscribe_with_function_config_dict(pulumi_mocks, project_cwd):
    topic = Topic("notifications")
    config_dict = {"handler": "functions/simple.handler", "memory": 512, "timeout": 60}
    sub = topic.subscribe("handler", config_dict)

    def check_resources(_):
        fn_name = f"{TP}notifications-handler"
        functions = pulumi_mocks.created_functions(fn_name)
        assert len(functions) == 1
        fn = functions[0]
        assert fn.typ == "aws:lambda/function:Function"
        assert fn.inputs["memorySize"] == 512
        assert fn.inputs["timeout"] == 60

    sub.resources.function.resources.function.arn.apply(check_resources)


@pulumi.runtime.test
def test_topic_subscribe_with_handler_in_opts(pulumi_mocks, project_cwd):
    topic = Topic("notifications")
    sub = topic.subscribe("handler", handler="functions/simple.handler", memory=256)

    def check_resources(_):
        fn_name = f"{TP}notifications-handler"
        functions = pulumi_mocks.created_functions(fn_name)
        assert len(functions) == 1
        fn = functions[0]
        assert fn.typ == "aws:lambda/function:Function"
        assert fn.inputs["memorySize"] == 256

    sub.resources.function.resources.function.arn.apply(check_resources)


@pulumi.runtime.test
def test_topic_subscribe_with_filter(pulumi_mocks, project_cwd):
    topic = Topic("notifications")
    sub = topic.subscribe(
        "handler", "functions/simple.handler", filter_=FILTER_POLICY_ORDER_SHIPMENT
    )

    def check_resources(_):
        sub_name = f"{TP}notifications-handler-subscription"
        assert_subscription_filter(pulumi_mocks, sub_name, FILTER_POLICY_ORDER_SHIPMENT)

    sub.resources.subscription.arn.apply(check_resources)


@pulumi.runtime.test
def test_topic_multiple_lambda_subscriptions(pulumi_mocks, project_cwd):
    topic = Topic("notifications")
    sub1 = topic.subscribe("email-handler", "functions/simple.handler")
    sub2 = topic.subscribe("sms-handler", "functions/simple.handler")

    def check_resources(_):
        all_subs = pulumi_mocks.created_topic_subscriptions()
        lambda_subs = [s for s in all_subs if s.inputs["protocol"] == "lambda"]
        assert len(lambda_subs) == 2

    pulumi.Output.all(
        sub1.resources.subscription.arn,
        sub2.resources.subscription.arn,
    ).apply(check_resources)


# SQS queue subscription tests


@pulumi.runtime.test
def test_topic_subscribe_queue_with_queue_component(pulumi_mocks, project_cwd):
    topic = Topic("notifications")
    queue = Queue("analytics")
    sub = topic.subscribe_queue("analytics", queue)

    def check_resources(args):
        topic_arn, queue_arn, _ = args
        sub_name = f"{TP}notifications-analytics-queue-subscription"
        subs = pulumi_mocks.created_topic_subscriptions(sub_name)
        assert len(subs) == 1
        s = subs[0]
        assert s.typ == "aws:sns/topicSubscription:TopicSubscription"
        assert s.inputs["protocol"] == "sqs"
        assert s.inputs["topic"] == topic_arn
        assert s.inputs["endpoint"] == queue_arn
        assert_field_not_set_or_none(s.inputs, "filterPolicy")
        assert s.inputs["rawMessageDelivery"] is False

    pulumi.Output.all(
        topic.arn,
        queue.arn,
        sub.resources.subscription.arn,
    ).apply(check_resources)


@pulumi.runtime.test
def test_topic_subscribe_queue_creates_queue_policy(pulumi_mocks, project_cwd):
    topic = Topic("notifications")
    queue = Queue("analytics")
    sub = topic.subscribe_queue("analytics", queue)

    def check_resources(args):
        queue_arn, queue_url, _ = args
        policies = pulumi_mocks.created_queue_policies()
        assert len(policies) == 1
        policy = policies[0]
        assert policy.typ == "aws:sqs/queuePolicy:QueuePolicy"
        assert policy.inputs["queueUrl"] == queue_url

        policy_doc = json.loads(policy.inputs["policy"])
        assert_queue_policy_statement(policy_doc, queue_arn)

    pulumi.Output.all(
        queue.arn,
        queue.url,
        sub.resources.subscription.arn,
    ).apply(check_resources)


@pulumi.runtime.test
def test_topic_subscribe_queue_with_arn_string(pulumi_mocks, project_cwd):
    topic = Topic("notifications")
    queue_arn = f"arn:aws:sqs:us-east-1:{ACCOUNT_ID}:external-queue"
    sub = topic.subscribe_queue("external", queue_arn)

    def check_resources(args):
        topic_arn, _ = args
        sub_name = f"{TP}notifications-external-queue-subscription"
        subs = pulumi_mocks.created_topic_subscriptions(sub_name)
        assert len(subs) == 1
        s = subs[0]
        assert s.inputs["endpoint"] == queue_arn
        assert s.inputs["topic"] == topic_arn

        policies = pulumi_mocks.created_queue_policies()
        assert len(policies) == 0

    pulumi.Output.all(
        topic.arn,
        sub.resources.subscription.arn,
    ).apply(check_resources)


@pulumi.runtime.test
def test_topic_subscribe_queue_with_filter(pulumi_mocks, project_cwd):
    topic = Topic("notifications")
    queue = Queue("analytics")
    sub = topic.subscribe_queue("analytics", queue, filter_=FILTER_POLICY_PRIORITY_HIGH)

    def check_resources(_):
        sub_name = f"{TP}notifications-analytics-queue-subscription"
        assert_subscription_filter(pulumi_mocks, sub_name, FILTER_POLICY_PRIORITY_HIGH)

    sub.resources.subscription.arn.apply(check_resources)


@pulumi.runtime.test
def test_topic_subscribe_queue_with_raw_message_delivery(pulumi_mocks, project_cwd):
    topic = Topic("notifications")
    queue = Queue("analytics")
    sub = topic.subscribe_queue("analytics", queue, raw_message_delivery=True)

    def check_resources(_):
        sub_name = f"{TP}notifications-analytics-queue-subscription"
        subs = pulumi_mocks.created_topic_subscriptions(sub_name)
        assert len(subs) == 1
        assert subs[0].inputs["rawMessageDelivery"] is True

    sub.resources.subscription.arn.apply(check_resources)


@pulumi.runtime.test
def test_topic_subscribe_queue_with_filter_and_raw_message_delivery(pulumi_mocks, project_cwd):
    topic = Topic("notifications")
    queue = Queue("analytics")
    sub = topic.subscribe_queue(
        "analytics", queue, filter_=FILTER_POLICY_PRIORITY_HIGH, raw_message_delivery=True
    )

    def check_resources(_):
        sub_name = f"{TP}notifications-analytics-queue-subscription"
        assert_subscription_filter(pulumi_mocks, sub_name, FILTER_POLICY_PRIORITY_HIGH)

        subs = pulumi_mocks.created_topic_subscriptions(sub_name)
        assert subs[0].inputs["rawMessageDelivery"] is True

    sub.resources.subscription.arn.apply(check_resources)


@pulumi.runtime.test
def test_topic_fifo_subscribe_fifo_queue(pulumi_mocks, project_cwd):
    topic = Topic("orders", fifo=True)
    queue = Queue("processing", fifo=True)
    sub = topic.subscribe_queue("processor", queue)

    def check_resources(_):
        subs = pulumi_mocks.created_topic_subscriptions()
        assert len(subs) == 1
        assert subs[0].inputs["protocol"] == "sqs"

    sub.resources.subscription.arn.apply(check_resources)


@pulumi.runtime.test
def test_topic_fifo_subscribe_standard_queue(pulumi_mocks, project_cwd):
    """Verify FIFO topic can subscribe to standard queue (AWS allows this)."""
    topic = Topic("orders", fifo=True)
    queue = Queue("processing")  # Standard queue
    sub = topic.subscribe_queue("processor", queue)

    def check_resources(_):
        subs = pulumi_mocks.created_topic_subscriptions()
        assert len(subs) == 1
        assert subs[0].inputs["protocol"] == "sqs"

    sub.resources.subscription.arn.apply(check_resources)


@pulumi.runtime.test
def test_topic_multiple_queue_subscriptions(pulumi_mocks, project_cwd):
    topic = Topic("notifications")
    queue1 = Queue("analytics")
    queue2 = Queue("archival")
    sub1 = topic.subscribe_queue("analytics", queue1)
    sub2 = topic.subscribe_queue("archival", queue2)

    def check_resources(args):
        queue1_arn, queue2_arn, _, _ = args
        all_subs = pulumi_mocks.created_topic_subscriptions()
        sqs_subs = [s for s in all_subs if s.inputs["protocol"] == "sqs"]
        assert len(sqs_subs) == 2

        policies = pulumi_mocks.created_queue_policies()
        assert len(policies) == 2

        policy_resources = set()
        for policy in policies:
            policy_doc = json.loads(policy.inputs["policy"])
            policy_resources.add(policy_doc["Statement"][0]["Resource"])

        assert queue1_arn in policy_resources
        assert queue2_arn in policy_resources

    pulumi.Output.all(
        queue1.arn,
        queue2.arn,
        sub1.resources.subscription.arn,
        sub2.resources.subscription.arn,
    ).apply(check_resources)


@pulumi.runtime.test
def test_topic_mixed_lambda_and_queue_subscriptions(pulumi_mocks, project_cwd):
    topic = Topic("notifications")
    queue = Queue("analytics")
    lambda_sub = topic.subscribe("processor", "functions/simple.handler")
    queue_sub = topic.subscribe_queue("analytics", queue)

    def check_resources(_):
        all_subs = pulumi_mocks.created_topic_subscriptions()
        assert len(all_subs) == 2

        lambda_subs = [s for s in all_subs if s.inputs["protocol"] == "lambda"]
        sqs_subs = [s for s in all_subs if s.inputs["protocol"] == "sqs"]
        assert len(lambda_subs) == 1
        assert len(sqs_subs) == 1

        permissions = pulumi_mocks.created_permissions()
        assert len(permissions) == 1

        policies = pulumi_mocks.created_queue_policies()
        assert len(policies) == 1

    pulumi.Output.all(
        lambda_sub.resources.subscription.arn,
        queue_sub.resources.subscription.arn,
    ).apply(check_resources)


# Validation error tests


def test_topic_fifo_cannot_subscribe_lambda():
    topic = Topic("orders", fifo=True)
    with pytest.raises(ValueError, match="Cannot subscribe Lambda to FIFO topic"):
        topic.subscribe("handler", "functions/simple.handler")


def test_topic_duplicate_lambda_subscription():
    topic = Topic("notifications")
    topic.subscribe("handler", "functions/simple.handler")
    with pytest.raises(ValueError, match="Subscription 'handler' already exists"):
        topic.subscribe("handler", "functions/other.handler")


def test_topic_duplicate_queue_subscription():
    topic = Topic("notifications")
    queue = Queue("analytics")
    topic.subscribe_queue("analytics", queue)
    with pytest.raises(ValueError, match="Queue subscription 'analytics' already exists"):
        topic.subscribe_queue("analytics", queue)


def test_topic_subscribe_missing_handler():
    topic = Topic("notifications")
    with pytest.raises(ValueError, match="Missing handler configuration"):
        topic.subscribe("handler")


def test_topic_subscribe_ambiguous_handler():
    topic = Topic("notifications")
    with pytest.raises(ValueError, match="Ambiguous handler configuration"):
        topic.subscribe("handler", "functions/simple.handler", handler="other.handler")


def test_topic_subscribe_cannot_combine_config_with_opts():
    topic = Topic("notifications")
    config = FunctionConfig(handler="functions/simple.handler")
    with pytest.raises(ValueError, match="cannot combine complete handler"):
        topic.subscribe("handler", config, memory=256)


# Link tests


@pulumi.runtime.test
def test_topic_link(pulumi_mocks, project_cwd):
    topic = Topic("notifications")

    link = topic.link()

    def verify_link(args):
        properties, permissions, topic_arn, topic_name = args

        assert properties["topic_arn"] == topic_arn
        assert properties["topic_name"] == topic_name

        assert len(permissions) == 1
        permission = permissions[0]
        assert permission.actions == ["sns:Publish"]
        assert len(permission.resources) == 1

        def verify_resource(resource):
            assert resource == topic_arn

        permission.resources[0].apply(verify_resource)

    pulumi.Output.all(
        link.properties,
        link.permissions,
        topic.arn,
        topic.topic_name,
    ).apply(verify_link)
