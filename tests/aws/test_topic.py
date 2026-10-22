import json
import re

import pulumi
import pytest

from stelvio.aws.function import FunctionConfig
from stelvio.aws.queue import Queue
from stelvio.aws.topic import Topic

from ..conftest import TP
from .conftest import assert_hash_truncated
from .pulumi_mocks import ACCOUNT_ID, DEFAULT_REGION, R, tn

# Filter policy constants
FILTER_POLICY_ORDER_SHIPMENT = {"type": ["order", "shipment"]}
FILTER_POLICY_PRIORITY_HIGH = {"priority": ["high"]}

# ARN templates
TOPIC_ARN_TEMPLATE = f"arn:aws:sns:{DEFAULT_REGION}:{ACCOUNT_ID}:{{name}}"

# Topic creation tests


@pytest.mark.parametrize(
    ("name", "fifo", "fifo_inputs"),
    [
        pytest.param("orders", False, {}, id="standard"),
        pytest.param(
            "orders", True, {"fifoTopic": True, "contentBasedDeduplication": True}, id="fifo"
        ),
        pytest.param(
            "orders.fifo",
            True,
            {"fifoTopic": True, "contentBasedDeduplication": True},
            id="fifo-suffix-stripped",
        ),
    ],
)
def test_topic_creates_sns_topic(pulumi_mocks, name, fifo, fifo_inputs):
    @pulumi.runtime.test
    def deploy():
        return Topic(name, fifo=fifo).resources

    deploy()

    # Full compare: no `name` input, and `.fifo` never reaches the logical name
    pulumi_mocks.assert_res("orders", R.TOPIC, fifo_inputs)


@pytest.mark.parametrize(
    ("name", "fifo"),
    [("orders.fifo", False), ("orders.v2", False), ("orders.v2.fifo", True)],
)
def test_topic_rejects_a_dot_outside_the_fifo_suffix(pulumi_mocks, name, fifo):
    with pytest.raises(ValueError, match=rf"Topic '{name}': .*'\.fifo' suffix .*fifo=True"):
        Topic(name, fifo=fifo)


def test_topic_rejects_a_dotted_name_even_when_customize_sets_the_aws_name(pulumi_mocks):
    # The check reads the component name, so a dotted one must be renamed.
    with pytest.raises(ValueError, match=r"Topic 'orders\.v2': .*'\.fifo' suffix .*fifo=True"):
        Topic("orders.v2", customize={"topic": {"name": "orders-v2"}})


def test_topic_long_name_truncates_logical_name(pulumi_mocks):
    @pulumi.runtime.test
    def deploy():
        return Topic("t" * 100).resources

    deploy()

    [topic] = pulumi_mocks.created(R.TOPIC)
    assert_hash_truncated(topic.name, 72)  # pulumi-aws caps SNS autonames at 80, minus its 8


@pulumi.runtime.test
def test_topic_properties(pulumi_mocks, project_cwd):
    topic = Topic("notifications")

    def check_properties(args):
        arn, name = args
        expected_name = f"{TP}notifications-test-name"
        assert arn == TOPIC_ARN_TEMPLATE.format(name=expected_name)
        assert name == expected_name

    pulumi.Output.all(topic.arn, topic.topic_name).apply(check_properties)


# Lambda subscription tests


def test_topic_subscribe_creates_lambda_subscription(pulumi_mocks, project_cwd):
    topic = Topic("notifications")
    sub = topic.subscribe("handler", "functions/simple.handler")

    @pulumi.runtime.test
    def deploy():
        return sub.resources

    deploy()

    # Full compare: no filterPolicy, no rawMessageDelivery
    pulumi_mocks.assert_res(
        "notifications-handler-subscription",
        R.TOPIC_SUBSCRIPTION,
        {
            "protocol": "lambda",
            "topic": TOPIC_ARN_TEMPLATE.format(name=tn(TP + "notifications")),
            "endpoint": (
                f"arn:aws:lambda:{DEFAULT_REGION}:{ACCOUNT_ID}:function:"
                f"{tn(TP + 'notifications-handler')}"
            ),
        },
    )
    pulumi_mocks.assert_res_counts(
        {
            R.TOPIC: 1,
            R.TOPIC_SUBSCRIPTION: 1,
            R.LAMBDA_PERMISSION: 1,
            R.FUNCTION: 1,
            R.ROLE: 1,
            R.ROLE_POLICY_ATTACHMENT: 1,
        }
    )


def test_topic_subscribe_creates_lambda_function(pulumi_mocks, project_cwd):
    topic = Topic("notifications")
    sub = topic.subscribe("handler", "functions/simple.handler")

    @pulumi.runtime.test
    def deploy():
        return sub.resources

    deploy()

    pulumi_mocks.assert_res(
        "notifications-handler", R.FUNCTION, {"handler": "simple.handler"}, partial=True
    )


def test_topic_subscribe_creates_lambda_permission(pulumi_mocks, project_cwd):
    topic = Topic("notifications")
    sub = topic.subscribe("handler", "functions/simple.handler")

    @pulumi.runtime.test
    def deploy():
        return sub.resources

    deploy()

    pulumi_mocks.assert_res(
        "notifications-handler-subscription-perm",
        R.LAMBDA_PERMISSION,
        {
            "action": "lambda:InvokeFunction",
            "principal": "sns.amazonaws.com",
            "sourceArn": TOPIC_ARN_TEMPLATE.format(name=tn(TP + "notifications")),
            "function": tn(TP + "notifications-handler"),
        },
    )


def test_topic_subscribe_with_function_config(pulumi_mocks, project_cwd):
    topic = Topic("notifications")
    config = FunctionConfig(handler="functions/simple.handler", memory=512, timeout=60)
    sub = topic.subscribe("handler", config)

    @pulumi.runtime.test
    def deploy():
        return sub.resources

    deploy()

    pulumi_mocks.assert_res(
        "notifications-handler", R.FUNCTION, {"memorySize": 512, "timeout": 60}, partial=True
    )


def test_topic_subscribe_with_function_options(pulumi_mocks, project_cwd):
    topic = Topic("notifications")
    sub = topic.subscribe("handler", "functions/simple.handler", memory=256, timeout=30)

    @pulumi.runtime.test
    def deploy():
        return sub.resources

    deploy()

    pulumi_mocks.assert_res(
        "notifications-handler", R.FUNCTION, {"memorySize": 256, "timeout": 30}, partial=True
    )


def test_topic_subscribe_with_function_config_dict(pulumi_mocks, project_cwd):
    topic = Topic("notifications")
    config_dict = {"handler": "functions/simple.handler", "memory": 512, "timeout": 60}
    sub = topic.subscribe("handler", config_dict)

    @pulumi.runtime.test
    def deploy():
        return sub.resources

    deploy()

    pulumi_mocks.assert_res(
        "notifications-handler", R.FUNCTION, {"memorySize": 512, "timeout": 60}, partial=True
    )


def test_topic_subscribe_with_handler_in_opts(pulumi_mocks, project_cwd):
    topic = Topic("notifications")
    sub = topic.subscribe("handler", handler="functions/simple.handler", memory=256)

    @pulumi.runtime.test
    def deploy():
        return sub.resources

    deploy()

    pulumi_mocks.assert_res("notifications-handler", R.FUNCTION, {"memorySize": 256}, partial=True)


def test_topic_subscribe_with_filter(pulumi_mocks, project_cwd):
    topic = Topic("notifications")
    sub = topic.subscribe(
        "handler", "functions/simple.handler", filter_=FILTER_POLICY_ORDER_SHIPMENT
    )

    @pulumi.runtime.test
    def deploy():
        return sub.resources

    deploy()

    recorded = pulumi_mocks.assert_res("notifications-handler-subscription", R.TOPIC_SUBSCRIPTION)
    assert json.loads(recorded.inputs["filterPolicy"]) == FILTER_POLICY_ORDER_SHIPMENT


def test_topic_multiple_lambda_subscriptions(pulumi_mocks, project_cwd):
    topic = Topic("notifications")
    sub1 = topic.subscribe("email-handler", "functions/simple.handler")
    sub2 = topic.subscribe("sms-handler", "functions/simple.handler")

    @pulumi.runtime.test
    def deploy():
        return [sub1.resources, sub2.resources]

    deploy()

    subs = pulumi_mocks.created(R.TOPIC_SUBSCRIPTION)
    assert [s.inputs["protocol"] for s in subs] == ["lambda", "lambda"]


# SQS queue subscription tests


def test_topic_subscribe_queue_with_queue_component(pulumi_mocks, project_cwd):
    topic = Topic("notifications")
    queue = Queue("analytics")
    sub = topic.subscribe_queue("analytics", queue)

    @pulumi.runtime.test
    def deploy():
        return sub.resources

    deploy()

    # Full compare: no filterPolicy
    pulumi_mocks.assert_res(
        "notifications-analytics-queue-subscription",
        R.TOPIC_SUBSCRIPTION,
        {
            "protocol": "sqs",
            "topic": TOPIC_ARN_TEMPLATE.format(name=tn(TP + "notifications")),
            "endpoint": f"arn:aws:sqs:{DEFAULT_REGION}:{ACCOUNT_ID}:{tn(TP + 'analytics')}",
            "rawMessageDelivery": False,
        },
    )
    pulumi_mocks.assert_res_counts(
        {R.TOPIC: 1, R.TOPIC_SUBSCRIPTION: 1, R.QUEUE: 1, R.QUEUE_POLICY: 1}
    )


def test_topic_subscribe_queue_creates_queue_policy(pulumi_mocks, project_cwd):
    topic = Topic("notifications")
    queue = Queue("analytics")
    sub = topic.subscribe_queue("analytics", queue)

    @pulumi.runtime.test
    def deploy():
        return sub.resources

    deploy()

    assert len(pulumi_mocks.created(R.QUEUE_POLICY)) == 1
    queue_url = f"https://sqs.{DEFAULT_REGION}.amazonaws.com/{ACCOUNT_ID}/{tn(TP + 'analytics')}"
    pulumi_mocks.assert_res(
        "analytics-notifications-sns-policy",
        R.QUEUE_POLICY,
        {"queueUrl": queue_url},
        partial=True,
    )


def test_topic_subscribe_queue_with_arn_string(pulumi_mocks, project_cwd):
    topic = Topic("notifications")
    queue_arn = f"arn:aws:sqs:us-east-1:{ACCOUNT_ID}:external-queue"
    sub = topic.subscribe_queue("external", queue_arn)

    @pulumi.runtime.test
    def deploy():
        return sub.resources

    deploy()

    pulumi_mocks.assert_res(
        "notifications-external-queue-subscription",
        R.TOPIC_SUBSCRIPTION,
        {"endpoint": queue_arn, "topic": TOPIC_ARN_TEMPLATE.format(name=tn(TP + "notifications"))},
        partial=True,
    )
    pulumi_mocks.assert_no_res(R.QUEUE_POLICY)


def test_topic_subscribe_queue_with_filter(pulumi_mocks, project_cwd):
    topic = Topic("notifications")
    queue = Queue("analytics")
    sub = topic.subscribe_queue("analytics", queue, filter_=FILTER_POLICY_PRIORITY_HIGH)

    @pulumi.runtime.test
    def deploy():
        return sub.resources

    deploy()

    recorded = pulumi_mocks.assert_res(
        "notifications-analytics-queue-subscription", R.TOPIC_SUBSCRIPTION
    )
    assert json.loads(recorded.inputs["filterPolicy"]) == FILTER_POLICY_PRIORITY_HIGH


def test_topic_subscribe_queue_with_raw_message_delivery(pulumi_mocks, project_cwd):
    topic = Topic("notifications")
    queue = Queue("analytics")
    sub = topic.subscribe_queue("analytics", queue, raw_message_delivery=True)

    @pulumi.runtime.test
    def deploy():
        return sub.resources

    deploy()

    pulumi_mocks.assert_res(
        "notifications-analytics-queue-subscription",
        R.TOPIC_SUBSCRIPTION,
        {"rawMessageDelivery": True},
        partial=True,
    )


def test_topic_subscribe_queue_with_filter_and_raw_message_delivery(pulumi_mocks, project_cwd):
    topic = Topic("notifications")
    queue = Queue("analytics")
    sub = topic.subscribe_queue(
        "analytics", queue, filter_=FILTER_POLICY_PRIORITY_HIGH, raw_message_delivery=True
    )

    @pulumi.runtime.test
    def deploy():
        return sub.resources

    deploy()

    recorded = pulumi_mocks.assert_res(
        "notifications-analytics-queue-subscription",
        R.TOPIC_SUBSCRIPTION,
        {"rawMessageDelivery": True},
        partial=True,
    )
    assert json.loads(recorded.inputs["filterPolicy"]) == FILTER_POLICY_PRIORITY_HIGH


def test_topic_fifo_subscribe_fifo_queue(pulumi_mocks, project_cwd):
    topic = Topic("orders", fifo=True)
    queue = Queue("processing", fifo=True)
    sub = topic.subscribe_queue("processor", queue)

    @pulumi.runtime.test
    def deploy():
        return sub.resources

    deploy()

    assert len(pulumi_mocks.created(R.TOPIC_SUBSCRIPTION)) == 1
    pulumi_mocks.assert_res(
        "orders-processor-queue-subscription",
        R.TOPIC_SUBSCRIPTION,
        {"protocol": "sqs"},
        partial=True,
    )


def test_topic_fifo_subscribe_standard_queue(pulumi_mocks, project_cwd):
    """Verify FIFO topic can subscribe to standard queue (AWS allows this)."""
    topic = Topic("orders", fifo=True)
    queue = Queue("processing")  # Standard queue
    sub = topic.subscribe_queue("processor", queue)

    @pulumi.runtime.test
    def deploy():
        return sub.resources

    deploy()

    assert len(pulumi_mocks.created(R.TOPIC_SUBSCRIPTION)) == 1
    pulumi_mocks.assert_res(
        "orders-processor-queue-subscription",
        R.TOPIC_SUBSCRIPTION,
        {"protocol": "sqs"},
        partial=True,
    )


def test_topic_multiple_queue_subscriptions(pulumi_mocks, project_cwd):
    topic = Topic("notifications")
    queue1 = Queue("analytics")
    queue2 = Queue("archival")
    sub1 = topic.subscribe_queue("analytics", queue1)
    sub2 = topic.subscribe_queue("archival", queue2)

    @pulumi.runtime.test
    def deploy():
        return [sub1.resources, sub2.resources]

    deploy()

    subs = pulumi_mocks.created(R.TOPIC_SUBSCRIPTION)
    assert [s.inputs["protocol"] for s in subs] == ["sqs", "sqs"]
    policy_resources = sorted(
        json.loads(p.inputs["policy"])["Statement"][0]["Resource"]
        for p in pulumi_mocks.created(R.QUEUE_POLICY)
    )
    assert policy_resources == [
        f"arn:aws:sqs:{DEFAULT_REGION}:{ACCOUNT_ID}:{tn(TP + 'analytics')}",
        f"arn:aws:sqs:{DEFAULT_REGION}:{ACCOUNT_ID}:{tn(TP + 'archival')}",
    ]


def test_topic_mixed_lambda_and_queue_subscriptions(pulumi_mocks, project_cwd):
    topic = Topic("notifications")
    queue = Queue("analytics")
    lambda_sub = topic.subscribe("processor", "functions/simple.handler")
    queue_sub = topic.subscribe_queue("analytics", queue)

    @pulumi.runtime.test
    def deploy():
        return [lambda_sub.resources, queue_sub.resources]

    deploy()

    subs = pulumi_mocks.created(R.TOPIC_SUBSCRIPTION)
    assert sorted(s.inputs["protocol"] for s in subs) == ["lambda", "sqs"]
    assert len(pulumi_mocks.created(R.LAMBDA_PERMISSION)) == 1
    assert len(pulumi_mocks.created(R.QUEUE_POLICY)) == 1


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


def test_topic_subscribe_queue_customize_rejects_queue_policy_key():
    topic = Topic("notifications")
    with pytest.raises(ValueError, match=r"Unknown customization key\(s\) \['queue_policy'\]"):
        topic.subscribe_queue("analytics", Queue("analytics"), customize={"queue_policy": {}})


def test_topic_subscribe_queue_from_topic_without_app_prefix_raises(pulumi_mocks):
    topic = Topic("notifications", customize={"topic": {"name": "my-notifications"}})
    queue = Queue("analytics")
    sub = topic.subscribe_queue("analytics", queue)

    @pulumi.runtime.test
    def deploy():
        return sub.resources

    topic_arn = f"arn:aws:sns:{DEFAULT_REGION}:{ACCOUNT_ID}:my-notifications"
    queue_arn = f"arn:aws:sqs:{DEFAULT_REGION}:{ACCOUNT_ID}:{tn(TP + 'analytics')}"
    with pytest.raises(
        ValueError,
        match=re.escape(
            f"{topic_arn} cannot send to {queue_arn}: Stelvio's policy on it only lets this "
            f"app's buckets and topics send (names starting with '{TP}'). Keep that prefix in "
            "the custom name, or pass the target's ARN as a string and write its policy "
            "yourself."
        ),
    ):
        deploy()


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


# `parent=self` lives in ResourceOptions (invisible to input mocks), but it surfaces
# in the subscription component's URN as the `stelvio:aws:Topic$` parent segment.


@pulumi.runtime.test
def test_topic_subscription_parented_to_topic(pulumi_mocks, project_cwd):
    topic = Topic("parented")
    sub = topic.subscribe("handler", "functions/simple.handler")
    _ = sub.resources

    def check(urn):
        assert urn == (
            "urn:pulumi:stack::project::stelvio:aws:Topic$stelvio:aws:TopicSubscription"
            "::parented-handler-subscription"
        )

    return sub.urn.apply(check)


@pulumi.runtime.test
def test_topic_queue_subscription_parented_to_topic(pulumi_mocks):
    topic = Topic("parented-q")
    queue = Queue("parented-analytics")
    sub = topic.subscribe_queue("analytics", queue)
    _ = sub.resources

    def check(urn):
        assert urn == (
            "urn:pulumi:stack::project::stelvio:aws:Topic$stelvio:aws:TopicQueueSubscription"
            "::parented-q-analytics-queue-subscription"
        )

    return sub.urn.apply(check)
