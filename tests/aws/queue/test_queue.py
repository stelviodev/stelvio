import json
from dataclasses import dataclass
from typing import Any

import pulumi
import pytest

from stelvio.aws.function import Function, FunctionConfig
from stelvio.aws.permission import AwsPermission
from stelvio.aws.queue import (
    DlqConfig,
    Queue,
    QueueConfig,
    QueueConfigDict,
)
from stelvio.component import ComponentRegistry
from stelvio.link import Link

from ...conftest import TP
from ...test_utils import assert_config_dict_matches_dataclass
from ..conftest import assert_hash_truncated
from ..pulumi_mocks import ACCOUNT_ID, DEFAULT_REGION, R, tn
from ..subscription_test_helpers import verify_stelvio_function_for_subscription

QUEUE_ARN_TEMPLATE = f"arn:aws:sqs:{DEFAULT_REGION}:{ACCOUNT_ID}:{{name}}"

# Test constants for frequently repeated handlers
SIMPLE_HANDLER = "functions/simple.handler"
USERS_HANDLER = "functions/users.handler"
ORDERS_HANDLER = "functions/orders.handler"


pytestmark = pytest.mark.usefixtures("project_cwd")


@pytest.fixture
def basic_queue():
    return Queue("test")


def assert_mapping_config(pulumi_mocks, batch_size=10, enabled=True):
    [mapping] = pulumi_mocks.created(R.EVENT_SOURCE_MAPPING)
    assert mapping.inputs["batchSize"] == batch_size
    assert mapping.inputs["enabled"] == enabled


def get_single_event_source_mapping(pulumi_mocks):
    """Get the single EventSourceMapping from mocks, asserting exactly one exists."""
    [mapping] = pulumi_mocks.created(R.EVENT_SOURCE_MAPPING)
    return mapping


@dataclass
class QueueTestCase:
    test_id: str
    name: str
    config_input: QueueConfig | QueueConfigDict | None
    expected_fifo: bool | None = None
    expected_delay: int = 0
    expected_visibility_timeout: int = 60
    expected_retention: int = 345600  # 4 days default


def verify_subscription_resources(
    pulumi_mocks,
    queue: Queue,
    expected_count: int,
    expected_names: list[str] | None = None,
    expected_configs: dict[str, Any] | None = None,
):
    # Check subscriptions in queue
    assert len(queue._subscriptions) == expected_count

    if expected_names:
        subscription_names = [
            sub._function_name.split(f"{queue.name}-", 1)[1] for sub in queue._subscriptions
        ]
        for name in expected_names:
            assert name in subscription_names

    # Check Pulumi mock resources
    assert len(pulumi_mocks.created(R.FUNCTION)) == expected_count
    assert len(pulumi_mocks.created(R.EVENT_SOURCE_MAPPING)) == expected_count

    [queue_mock] = pulumi_mocks.created(R.QUEUE)
    expected_queue_arn = QUEUE_ARN_TEMPLATE.format(name=tn(queue_mock.name))

    for subscription in queue._subscriptions:
        subscription_name = subscription._function_name.split(f"{queue.name}-", 1)[1]
        function_mock = pulumi_mocks.assert_res(subscription._function_name, R.FUNCTION)
        # The mapping connects THIS subscription's function to the queue
        pulumi_mocks.assert_res(
            f"{subscription.name}-mapping",
            R.EVENT_SOURCE_MAPPING,
            {
                "batchSize": 10,
                "enabled": True,
                "eventSourceArn": expected_queue_arn,
                "functionName": tn(function_mock.name),
            },
            partial=True,
        )

        # Verify Lambda function has the correct SQS permissions
        verify_function_sqs_permissions(pulumi_mocks, function_mock, expected_queue_arn)

        # Verify Stelvio Function object was created correctly for this specific subscription
        expected_handler_input = (
            expected_configs.get(subscription_name) if expected_configs else None
        )
        verify_stelvio_function_for_subscription(
            source_name=queue.name,
            subscription_name=subscription_name,
            expected_link_name=f"{queue.name}-sqs",
            expected_handler_input=expected_handler_input,
            expected_link_count=1,
        )


def verify_function_sqs_permissions(pulumi_mocks, function_mock, expected_queue_arn):
    function_policy = pulumi_mocks.assert_res(function_mock.name + "-p", R.POLICY, prefixed=False)
    actual_statements = json.loads(function_policy.inputs["policy"])

    expected_sqs_statement = {
        "actions": [
            "sqs:ReceiveMessage",
            "sqs:DeleteMessage",
            "sqs:GetQueueAttributes",
        ],
        "resources": [expected_queue_arn],
    }

    [statement] = [s for s in actual_statements if "sqs:ReceiveMessage" in s["actions"]]
    assert statement == expected_sqs_statement


def verify_queue_resources(pulumi_mocks, test_case: QueueTestCase):
    assert len(pulumi_mocks.created(R.QUEUE)) == 1
    pulumi_mocks.assert_res(
        test_case.name,
        R.QUEUE,
        {
            "delaySeconds": test_case.expected_delay,
            "visibilityTimeoutSeconds": test_case.expected_visibility_timeout,
            "messageRetentionSeconds": test_case.expected_retention,
            "fifoQueue": test_case.expected_fifo,
        },
        partial=True,
    )


# Test case definitions
BASIC_QUEUE_TC = QueueTestCase(
    test_id="basic_queue",
    name="basic-queue",
    config_input=None,
    expected_fifo=None,
    expected_delay=0,
    expected_visibility_timeout=60,
)

FIFO_QUEUE_TC = QueueTestCase(
    test_id="fifo_queue",
    name="fifo-queue",
    config_input=QueueConfig(fifo=True),
    expected_fifo=True,
    expected_delay=0,
    expected_visibility_timeout=60,
)

DELAYED_QUEUE_TC = QueueTestCase(
    test_id="delayed_queue",
    name="delayed-queue",
    config_input={"delay": 5},
    expected_fifo=None,
    expected_delay=5,
    expected_visibility_timeout=60,
)

CUSTOM_VISIBILITY_TC = QueueTestCase(
    test_id="custom_visibility",
    name="custom-visibility",
    config_input=QueueConfig(visibility_timeout=60),
    expected_fifo=None,
    expected_delay=0,
    expected_visibility_timeout=60,
)

FULL_CONFIG_TC = QueueTestCase(
    test_id="full_config",
    name="full-config",
    config_input=QueueConfig(fifo=True, delay=10, visibility_timeout=120),
    expected_fifo=True,
    expected_delay=10,
    expected_visibility_timeout=120,
)


def test_config_dict_matches_dataclass():
    """Test that QueueConfigDict matches QueueConfig."""
    assert_config_dict_matches_dataclass(QueueConfig, QueueConfigDict)


def test_dlq_config_dict_matches_dataclass():
    """Test that DlqConfigDict has the same fields as DlqConfig.

    Note: We can't use assert_config_dict_matches_dataclass because DlqConfig uses
    forward references for 'Queue | str' which resolve differently in dataclass vs TypedDict.
    """
    from dataclasses import fields
    from typing import get_type_hints

    from stelvio.aws.queue import DlqConfigDict

    dataclass_fields = {f.name for f in fields(DlqConfig)}
    typeddict_fields = set(get_type_hints(DlqConfigDict).keys())

    assert dataclass_fields == typeddict_fields, (
        f"DlqConfigDict and DlqConfig have different fields: "
        f"dataclass={dataclass_fields}, typeddict={typeddict_fields}"
    )


@pytest.mark.parametrize(
    "test_case",
    [
        BASIC_QUEUE_TC,
        FIFO_QUEUE_TC,
        DELAYED_QUEUE_TC,
        CUSTOM_VISIBILITY_TC,
        FULL_CONFIG_TC,
    ],
    ids=lambda tc: tc.test_id,
)
def test_queue_creation(pulumi_mocks, test_case):
    if test_case.config_input is None:
        queue = Queue(test_case.name)
    elif isinstance(test_case.config_input, dict):
        queue = Queue(test_case.name, **test_case.config_input)
    else:
        queue = Queue(test_case.name, config=test_case.config_input)

    @pulumi.runtime.test
    def deploy():
        return queue.resources

    deploy()

    verify_queue_resources(pulumi_mocks, test_case)


@pulumi.runtime.test
def test_queue_properties(pulumi_mocks):
    # Arrange
    queue = Queue("my-queue")
    # Act
    _ = queue.resources

    # Assert
    def check_resources(args):
        queue_id, arn = args
        assert queue_id == TP + "my-queue-test-id"
        assert arn == QUEUE_ARN_TEMPLATE.format(name=tn(TP + "my-queue"))

    pulumi.Output.all(queue.resources.queue.id, queue.arn).apply(check_resources)


@pulumi.runtime.test
def test_queue_link(pulumi_mocks):
    # Arrange
    queue_name = "my-queue"
    queue = Queue(queue_name)

    # Act
    link = queue.link()

    # Assert
    def verify_permissions(args):
        properties, permissions = args

        # Properties should include queue name, ARN, and URL
        assert "queue_name" in properties
        assert "queue_arn" in properties
        assert "queue_url" in properties

        # Should have 1 permission block
        assert len(permissions) == 1

        # Permission should include SQS actions for sending messages
        permission = permissions[0]
        expected_actions = [
            "sqs:SendMessage",
            "sqs:GetQueueAttributes",
        ]
        assert sorted(permission.actions) == sorted(expected_actions)
        assert len(permission.resources) == 1

        def verify_resource(resource):
            queue_arn = QUEUE_ARN_TEMPLATE.format(name=tn(TP + queue_name))
            assert resource == queue_arn

        permission.resources[0].apply(verify_resource)

    pulumi.Output.all(link.properties, link.permissions).apply(verify_permissions)


def test_queue_invalid_config_combination():
    """Test that combining config parameter with options raises ValueError."""
    config = QueueConfig(fifo=True)

    with pytest.raises(
        ValueError, match="cannot combine 'config' parameter with additional options"
    ):
        Queue("test", config=config, delay=5)


def test_queue_config_dict_support():
    config_dict = {
        "fifo": True,
        "delay": 5,
    }

    queue = Queue("test", config=config_dict)

    assert queue._config.fifo is True
    assert queue._config.delay == 5


def test_dlq_config_from_dict_simple():
    """Test DLQ config normalization from dict with Queue."""
    dlq = Queue("my-dlq")
    queue = Queue("test", dlq={"queue": dlq})

    assert isinstance(queue._config.dlq, DlqConfig)
    assert queue._config.dlq.queue is dlq
    assert queue._config.dlq.retry == 3  # default


def test_dlq_config_from_dict():
    """Test DLQ config normalization from dict."""
    dlq = Queue("my-dlq")
    queue = Queue("test", dlq={"queue": dlq, "retry": 5})

    assert isinstance(queue._config.dlq, DlqConfig)
    assert queue._config.dlq.queue is dlq
    assert queue._config.dlq.retry == 5


def test_dlq_config_from_queue_component():
    """Test DLQ config with Queue component reference."""
    dlq = Queue("my-dlq")
    queue = Queue("test", dlq=DlqConfig(queue=dlq, retry=5))

    assert isinstance(queue._config.dlq, DlqConfig)
    assert queue._config.dlq.queue is dlq
    assert queue._config.dlq.retry == 5


def test_dlq_with_queue_reference(pulumi_mocks):
    """Test that DLQ is properly configured when referencing Queue component."""

    @pulumi.runtime.test
    def deploy():
        dlq = Queue("orders-dlq")
        return Queue("orders", dlq=DlqConfig(queue=dlq, retry=5)).resources

    deploy()

    assert len(pulumi_mocks.created(R.QUEUE)) == 2
    main_queue = pulumi_mocks.assert_res("orders", R.QUEUE)
    assert json.loads(main_queue.inputs["redrivePolicy"]) == {
        "deadLetterTargetArn": QUEUE_ARN_TEMPLATE.format(name=tn(TP + "orders-dlq")),
        "maxReceiveCount": 5,
    }


def test_dlq_with_dict_reference(pulumi_mocks):
    """Test that DLQ is properly configured when using dict syntax."""

    @pulumi.runtime.test
    def deploy():
        dlq = Queue("orders-dlq")
        return Queue("orders", dlq={"queue": dlq}).resources  # Uses default retry=3

    deploy()

    assert len(pulumi_mocks.created(R.QUEUE)) == 2
    main_queue = pulumi_mocks.assert_res("orders", R.QUEUE)
    assert json.loads(main_queue.inputs["redrivePolicy"]) == {
        "deadLetterTargetArn": QUEUE_ARN_TEMPLATE.format(name=tn(TP + "orders-dlq")),
        "maxReceiveCount": 3,
    }


def test_dlq_with_queue_directly(pulumi_mocks):
    """Test that DLQ is properly configured when passing Queue component directly."""
    dlq = Queue("orders-dlq")
    main_queue = Queue("orders", dlq=dlq)  # Pass Queue directly, uses default retry=3

    @pulumi.runtime.test
    def deploy():
        return main_queue.resources

    deploy()

    assert len(pulumi_mocks.created(R.QUEUE)) == 2
    main_queue_resource = pulumi_mocks.assert_res("orders", R.QUEUE)
    assert json.loads(main_queue_resource.inputs["redrivePolicy"]) == {
        "deadLetterTargetArn": QUEUE_ARN_TEMPLATE.format(name=tn(TP + "orders-dlq")),
        "maxReceiveCount": 3,
    }

    # Verify the internal config was normalized to DlqConfig
    assert isinstance(main_queue._config.dlq, DlqConfig)
    assert main_queue._config.dlq.queue is dlq
    assert main_queue._config.dlq.retry == 3


def test_dlq_with_string_arn(pulumi_mocks):
    """Test that DLQ is properly configured when passing ARN as string.

    Note: This test uses a static ARN string. In real-world scenarios, you might
    reference an existing DLQ from another stack or external resource.
    """
    # Simulate an external DLQ ARN (e.g., from another stack or existing resource)
    external_dlq_arn = "arn:aws:sqs:us-east-1:123456789012:external-dlq"
    main_queue = Queue("orders", dlq=external_dlq_arn)  # Pass ARN string, uses default retry=3

    @pulumi.runtime.test
    def deploy():
        return main_queue.resources

    deploy()

    # Only main queue should be created (DLQ is external)
    assert len(pulumi_mocks.created(R.QUEUE)) == 1
    main_queue_resource = pulumi_mocks.assert_res("orders", R.QUEUE)
    assert json.loads(main_queue_resource.inputs["redrivePolicy"]) == {
        "deadLetterTargetArn": external_dlq_arn,
        "maxReceiveCount": 3,
    }

    # Verify the internal config was normalized to DlqConfig
    assert isinstance(main_queue._config.dlq, DlqConfig)
    assert main_queue._config.dlq.queue == external_dlq_arn
    assert main_queue._config.dlq.retry == 3


@pulumi.runtime.test
def test_duplicate_subscription_names(pulumi_mocks):
    queue = Queue("test-queue")

    queue.subscribe("processor", SIMPLE_HANDLER)

    with pytest.raises(ValueError, match="Subscription 'processor' already exists"):
        queue.subscribe("processor", USERS_HANDLER)


def test_subscription_basic(pulumi_mocks):
    """Basic subscription functionality test."""
    queue = Queue("basic-sub")

    subscription = queue.subscribe("test", SIMPLE_HANDLER)

    @pulumi.runtime.test
    def deploy():
        return [queue.resources, subscription.resources]

    deploy()

    verify_subscription_resources(pulumi_mocks, queue, 1, ["test"])


@pytest.mark.parametrize(
    ("handler_input", "test_name"),
    [
        (SIMPLE_HANDLER, "string"),
        ({"handler": USERS_HANDLER, "memory": 512}, "dict_as_handler"),
        (FunctionConfig(handler=ORDERS_HANDLER, timeout=120), "config"),
    ],
)
def test_subscription_handler_types(pulumi_mocks, handler_input, test_name):
    """Test all supported handler input types."""
    queue = Queue(f"sub-{test_name}")

    subscription = queue.subscribe("test", handler_input)

    @pulumi.runtime.test
    def deploy():
        return [queue.resources, subscription.resources]

    deploy()

    verify_subscription_resources(
        pulumi_mocks,
        queue,
        expected_count=1,
        expected_names=["test"],
        expected_configs={"test": handler_input},
    )


def test_subscription_function_config_opts(pulumi_mocks):
    queue = Queue("dict-unpacked")

    subscription = queue.subscribe("test", handler=USERS_HANDLER, memory=512, timeout=60)

    @pulumi.runtime.test
    def deploy():
        return [queue.resources, subscription.resources]

    deploy()

    verify_subscription_resources(
        pulumi_mocks,
        queue,
        expected_count=1,
        expected_names=["test"],
        expected_configs={"test": {"handler": USERS_HANDLER, "memory": 512, "timeout": 60}},
    )


def test_subscription_link_merging(pulumi_mocks):
    """Test that user-provided links are properly merged with mandatory SQS permissions."""
    queue = Queue("link-merge-test")

    # Create FunctionConfig with custom links
    custom_link = Link(
        "s3-access",
        properties={"bucket_name": "my-bucket"},
        permissions=[
            AwsPermission(
                actions=["s3:GetObject", "s3:PutObject"], resources=["arn:aws:s3:::my-bucket/*"]
            )
        ],
    )

    function_config = FunctionConfig(handler=SIMPLE_HANDLER, memory=256, links=[custom_link])

    # Subscribe with custom function config
    subscription = queue.subscribe("processor", function_config)

    @pulumi.runtime.test
    def deploy():
        return [queue.resources, subscription.resources]

    deploy()

    verify_subscription_resources(
        pulumi_mocks,
        queue,
        expected_count=1,
        expected_names=["processor"],
        expected_configs={"processor": function_config},
    )

    # Additional verification: check that the created Function has both links
    functions = ComponentRegistry._instances.get(Function, [])
    function_map = {f.name: f for f in functions}

    created_function = function_map[f"{queue.name}-processor"]

    # Should have 2 links: sqs link + user's custom link
    assert len(created_function.config.links) == 2, (
        f"Expected 2 links (sqs + custom), got {len(created_function.config.links)}"
    )

    # Verify SQS link is present
    sqs_links = [
        link
        for link in created_function.config.links
        if hasattr(link, "name") and link.name == f"{queue.name}-sqs"
    ]
    assert len(sqs_links) == 1, "SQS link not found in merged links"

    # Verify custom link is present with correct permissions
    custom_links = [
        link
        for link in created_function.config.links
        if hasattr(link, "name") and link.name == "s3-access"
    ]
    assert len(custom_links) == 1, "Custom link not found in merged links"

    # Verify the custom link has the exact same permission as originally created
    expected_permission = AwsPermission(
        actions=["s3:GetObject", "s3:PutObject"], resources=["arn:aws:s3:::my-bucket/*"]
    )
    assert custom_links[0].permissions == [expected_permission], (
        "Custom link permissions not preserved correctly"
    )


@pytest.mark.parametrize("other_name", ["orders-sqs", "orders_sqs"])
def test_subscription_links_a_queue_named_like_its_own_sqs_link(pulumi_mocks, other_name):
    # The subscription's own `orders-sqs` link has no properties, so it shadows nothing.
    orders = Queue("orders")

    @pulumi.runtime.test
    def deploy():
        sub = orders.subscribe("proc", SIMPLE_HANDLER, links=[Queue(other_name)])
        return sub.resources.function.resources

    deploy()

    fn = pulumi_mocks.assert_res("orders-proc", R.FUNCTION)
    assert "STLV_ORDERS_SQS_QUEUE_URL" in fn.inputs["environment"]["variables"]


def test_subscription_with_multiple_handlers(pulumi_mocks):
    queue = Queue("multi-subscription")

    sub1 = queue.subscribe("processor", SIMPLE_HANDLER)
    sub2 = queue.subscribe("audit", {"handler": USERS_HANDLER, "memory": 256})
    sub3 = queue.subscribe("config", FunctionConfig(handler=ORDERS_HANDLER, timeout=60))

    @pulumi.runtime.test
    def deploy():
        return [queue.resources, sub1.resources, sub2.resources, sub3.resources]

    deploy()

    verify_subscription_resources(
        pulumi_mocks,
        queue,
        expected_count=3,
        expected_names=["processor", "audit", "config"],
        expected_configs={
            "processor": SIMPLE_HANDLER,
            "audit": {"handler": USERS_HANDLER, "memory": 256},
            "config": FunctionConfig(handler=ORDERS_HANDLER, timeout=60),
        },
    )


def test_subscription_batch_size(pulumi_mocks, basic_queue):
    subscription = basic_queue.subscribe(
        "batch-test",
        SIMPLE_HANDLER,
        batch_size=5,
    )

    @pulumi.runtime.test
    def deploy():
        return subscription.resources

    deploy()

    assert_mapping_config(pulumi_mocks, batch_size=5)


def test_subscription_customize_reaches_mapping_and_function(pulumi_mocks):
    subscription = Queue("orders").subscribe(
        "proc",
        SIMPLE_HANDLER,
        customize={
            "event_source_mapping": {"function_response_types": ["ReportBatchItemFailures"]},
            "function": {"function": {"memory_size": 1024}},
        },
    )

    @pulumi.runtime.test
    def deploy():
        return subscription.resources

    deploy()

    pulumi_mocks.assert_res(
        "orders-proc-subscription-mapping",
        R.EVENT_SOURCE_MAPPING,
        {"functionResponseTypes": ["ReportBatchItemFailures"], "batchSize": 10},
        partial=True,
    )
    pulumi_mocks.assert_res("orders-proc", R.FUNCTION, {"memorySize": 1024}, partial=True)
    pulumi_mocks.assert_res_counts(
        {
            R.QUEUE: 1,
            R.EVENT_SOURCE_MAPPING: 1,
            R.FUNCTION: 1,
            R.ROLE: 1,
            R.POLICY: 1,
            R.ROLE_POLICY_ATTACHMENT: 2,
        }
    )


@pytest.mark.parametrize(
    ("name", "fifo", "fifo_inputs"),
    [
        pytest.param("orders", False, {}, id="standard"),
        pytest.param(
            "orders", True, {"fifoQueue": True, "contentBasedDeduplication": True}, id="fifo"
        ),
        pytest.param(
            "orders.fifo",
            True,
            {"fifoQueue": True, "contentBasedDeduplication": True},
            id="fifo-suffix-stripped",
        ),
    ],
)
def test_queue_lets_pulumi_name_it(pulumi_mocks, name, fifo, fifo_inputs):
    @pulumi.runtime.test
    def deploy():
        return Queue(name, fifo=fifo).resources

    deploy()

    # Full compare: no `name` input, and `.fifo` never reaches the logical name
    pulumi_mocks.assert_res(
        "orders",
        R.QUEUE,
        {
            "delaySeconds": 0,
            "visibilityTimeoutSeconds": 60,
            "messageRetentionSeconds": 345600,
            **fifo_inputs,
        },
    )


@pytest.mark.parametrize(
    ("name", "fifo"),
    [("orders.fifo", False), ("orders.v2", False), ("orders.v2.fifo", True)],
)
def test_queue_rejects_a_dot_outside_the_fifo_suffix(pulumi_mocks, name, fifo):
    with pytest.raises(ValueError, match=rf"Queue '{name}': .*'\.fifo' suffix .*fifo=True"):
        Queue(name, fifo=fifo)


def test_queue_rejects_a_dotted_name_even_when_customize_sets_the_aws_name(pulumi_mocks):
    # The check reads the component name, so a dotted one must be renamed.
    with pytest.raises(ValueError, match=r"Queue 'orders\.v2': .*'\.fifo' suffix .*fifo=True"):
        Queue("orders.v2", customize={"queue": {"name": "orders-v2"}})


@pytest.mark.parametrize(
    ("fifo", "length"),
    [
        pytest.param(False, 72, id="standard"),  # SQS 80 minus the 8-char Pulumi suffix
        pytest.param(True, 67, id="fifo"),  # 5 fewer: the provider appends .fifo last
    ],
)
def test_queue_long_name_truncates_logical_name(pulumi_mocks, fifo, length):
    @pulumi.runtime.test
    def deploy():
        return Queue("q" * 100, fifo=fifo).resources

    deploy()

    [queue] = pulumi_mocks.created(R.QUEUE)
    assert_hash_truncated(queue.name, length)


# Handler validation tests for QueueSubscription
@pytest.mark.parametrize(
    ("handler", "opts", "expected_error"),
    [
        # Missing handler in both places
        (
            None,
            {},
            "Missing handler configuration: when handler argument is None, "
            "'handler' option must be provided",
        ),
        # Handler in both places (ambiguous)
        (
            "functions/handler.process",
            {"handler": "functions/other.process"},
            "Ambiguous handler configuration: handler is specified both as positional argument "
            "and in options",
        ),
        # Complete config dict with additional options
        (
            {"handler": "functions/handler.process"},
            {"memory": 256},
            "Invalid configuration: cannot combine complete handler configuration "
            "with additional options",
        ),
        # Complete FunctionConfig with additional options
        (
            FunctionConfig(handler="functions/handler.process"),
            {"memory": 256},
            "Invalid configuration: cannot combine complete handler configuration "
            "with additional options",
        ),
    ],
)
def test_subscription_handler_validation(handler, opts, expected_error):
    """Test validation errors for invalid handler configurations in subscribe()."""
    queue = Queue("validation-test")

    with pytest.raises(ValueError, match=expected_error):
        queue.subscribe("test", handler, **opts)


def test_subscription_invalid_handler_type():
    """Test that invalid handler types raise TypeError."""
    queue = Queue("invalid-handler-test")

    with pytest.raises(TypeError, match="Invalid handler type: int"):
        queue.subscribe("test", 123)  # type: ignore[arg-type]


# Batch size validation tests
def test_subscription_batch_size_validation_standard_queue():
    """Test that batch_size validation allows 1-10000 for standard queues."""
    queue = Queue("standard-batch-test")

    # Valid batch sizes for standard queue
    queue.subscribe("small", SIMPLE_HANDLER, batch_size=1)
    queue.subscribe("medium", SIMPLE_HANDLER, batch_size=100)
    queue.subscribe("large", SIMPLE_HANDLER, batch_size=10000)


def test_subscription_batch_size_validation_standard_queue_invalid():
    """Test that invalid batch_size raises error for standard queues."""
    queue = Queue("standard-batch-invalid")

    with pytest.raises(
        ValueError, match="batch_size must be between 1 and 10000 for standard queues"
    ):
        queue.subscribe("invalid", SIMPLE_HANDLER, batch_size=0)

    queue2 = Queue("standard-batch-invalid2")
    with pytest.raises(
        ValueError, match="batch_size must be between 1 and 10000 for standard queues"
    ):
        queue2.subscribe("invalid", SIMPLE_HANDLER, batch_size=10001)


def test_subscription_batch_size_validation_fifo_queue():
    """Test that batch_size validation allows 1-10 for FIFO queues."""
    queue = Queue("fifo-batch-test", fifo=True)

    # Valid batch sizes for FIFO queue
    queue.subscribe("small", SIMPLE_HANDLER, batch_size=1)
    queue.subscribe("medium", SIMPLE_HANDLER, batch_size=5)
    queue.subscribe("large", SIMPLE_HANDLER, batch_size=10)


def test_subscription_batch_size_validation_fifo_queue_invalid():
    """Test that invalid batch_size raises error for FIFO queues."""
    queue = Queue("fifo-batch-invalid", fifo=True)

    with pytest.raises(ValueError, match="batch_size must be between 1 and 10 for FIFO queues"):
        queue.subscribe("invalid", SIMPLE_HANDLER, batch_size=11)


# Filter validation tests
def test_subscription_with_single_filter(pulumi_mocks, basic_queue):
    """Test subscription with a single message filter."""
    subscription = basic_queue.subscribe(
        "filtered",
        SIMPLE_HANDLER,
        filters=[{"body": {"orderType": ["refund"]}}],
    )

    @pulumi.runtime.test
    def deploy():
        return subscription.resources

    deploy()

    mapping = get_single_event_source_mapping(pulumi_mocks)

    expected_filter_criteria = {"filters": [{"pattern": '{"body": {"orderType": ["refund"]}}'}]}
    assert mapping.inputs["filterCriteria"] == expected_filter_criteria


def test_subscription_filters_and_batch_size(pulumi_mocks, basic_queue):
    """Test subscription with both filters and custom batch size."""
    subscription = basic_queue.subscribe(
        "filtered-batch",
        SIMPLE_HANDLER,
        filters=[{"body": {"priority": ["high"]}}],
        batch_size=5,
    )

    @pulumi.runtime.test
    def deploy():
        return subscription.resources

    deploy()

    mapping = get_single_event_source_mapping(pulumi_mocks)

    assert mapping.inputs["batchSize"] == 5
    expected_filter_criteria = {"filters": [{"pattern": '{"body": {"priority": ["high"]}}'}]}
    assert mapping.inputs["filterCriteria"] == expected_filter_criteria


def test_subscription_multiple_filters(pulumi_mocks, basic_queue):
    """Test subscription with multiple filters (OR logic)."""
    filters = [
        {"body": {"orderType": ["return"]}},
        {"body": {"orderType": ["cancellation"]}},
        {"body": {"orderType": ["refund"]}},
    ]
    subscription = basic_queue.subscribe(
        "multi-filtered",
        SIMPLE_HANDLER,
        filters=filters,
    )

    @pulumi.runtime.test
    def deploy():
        return subscription.resources

    deploy()

    mapping = get_single_event_source_mapping(pulumi_mocks)

    expected_filter_criteria = {"filters": [{"pattern": json.dumps(f)} for f in filters]}
    assert mapping.inputs["filterCriteria"] == expected_filter_criteria


def test_subscription_empty_filters(pulumi_mocks, basic_queue):
    """Test that empty filter list results in no filter_criteria."""
    subscription = basic_queue.subscribe(
        "empty-filters",
        SIMPLE_HANDLER,
        filters=[],
    )

    @pulumi.runtime.test
    def deploy():
        return subscription.resources

    deploy()

    mapping = get_single_event_source_mapping(pulumi_mocks)

    # Empty filters should result in None (no filtering)
    assert mapping.inputs.get("filterCriteria") is None


def test_subscription_too_many_filters():
    """Test that more than 5 filters raises ValueError."""
    queue = Queue("too-many-filters-test")

    filters = [
        {"body": {"type": ["a"]}},
        {"body": {"type": ["b"]}},
        {"body": {"type": ["c"]}},
        {"body": {"type": ["d"]}},
        {"body": {"type": ["e"]}},
        {"body": {"type": ["f"]}},  # 6th filter - exceeds limit
    ]

    with pytest.raises(
        ValueError,
        match="SQS message filters cannot exceed 5 filters, got 6 filters",
    ):
        queue.subscribe("invalid", SIMPLE_HANDLER, filters=filters)


def test_subscription_invalid_filter_type():
    """Test that non-dict filter raises TypeError."""
    queue = Queue("invalid-filter-type-test")

    # Filter must be a dict, not a string
    with pytest.raises(
        TypeError,
        match="Each SQS message filter must be a dict, but filter at index 0 is str",
    ):
        queue.subscribe(
            "invalid",
            SIMPLE_HANDLER,
            filters=["invalid-string-filter"],  # type: ignore[list-item]
        )


def test_subscription_filters_not_list():
    """Test that filters must be a list."""
    queue = Queue("filters-not-list-test")

    with pytest.raises(
        TypeError,
        match="SQS message filters must be a list, got dict",
    ):
        queue.subscribe(
            "invalid",
            SIMPLE_HANDLER,
            filters={"body": {"type": ["test"]}},  # type: ignore[arg-type]
        )


def test_subscription_complex_filter(pulumi_mocks, basic_queue):
    """Test subscription with complex filter using multiple fields (AND logic)."""
    subscription = basic_queue.subscribe(
        "complex-filter",
        SIMPLE_HANDLER,
        filters=[
            {
                "body": {
                    "customerTier": ["gold", "platinum"],
                    "priority": ["high"],
                },
                "messageAttributes": {
                    "region": ["us-west-2"],
                },
            }
        ],
    )

    @pulumi.runtime.test
    def deploy():
        return subscription.resources

    deploy()

    mapping = get_single_event_source_mapping(pulumi_mocks)

    complex_filter = {
        "body": {
            "customerTier": ["gold", "platinum"],
            "priority": ["high"],
        },
        "messageAttributes": {
            "region": ["us-west-2"],
        },
    }
    expected_filter_criteria = {"filters": [{"pattern": json.dumps(complex_filter)}]}
    assert mapping.inputs["filterCriteria"] == expected_filter_criteria


# Retention configuration tests
def test_queue_default_retention():
    """Test that default retention is None (as it is now set in customizer)."""
    queue = Queue("default-retention")
    assert queue._config.retention is None


def test_queue_default_retention_after_customize(pulumi_mocks):
    """Test that default retention is applied to the SQS resource by the customizer."""
    queue = Queue("default-retention")

    @pulumi.runtime.test
    def deploy():
        return queue.resources

    deploy()

    [queue_resource] = pulumi_mocks.created(R.QUEUE)
    assert queue_resource.inputs["messageRetentionSeconds"] == 345600


def test_queue_custom_retention():
    """Test that custom retention can be set."""
    # 7 days in seconds
    queue = Queue("custom-retention", retention=604800)
    assert queue._config.retention == 604800


def test_queue_retention_in_resources(pulumi_mocks):
    """Test that retention is properly passed to SQS resource."""
    queue = Queue("retention-test", retention=86400)  # 1 day

    @pulumi.runtime.test
    def deploy():
        return queue.resources

    deploy()

    [queue_resource] = pulumi_mocks.created(R.QUEUE)
    assert queue_resource.inputs["messageRetentionSeconds"] == 86400


def test_multiple_subscriptions_same_handler_different_batch_sizes(pulumi_mocks):
    """Test creating multiple subscriptions with same handler but different batch sizes.

    Each subscription creates a separate Lambda function, so this is a valid use case
    for scenarios like processing the same queue at different throughput levels.
    """
    queue = Queue("multi-batch-queue")

    # Create subscriptions with same handler but different batch sizes
    sub1 = queue.subscribe("fast-processor", SIMPLE_HANDLER, batch_size=1)
    sub2 = queue.subscribe("batch-processor", SIMPLE_HANDLER, batch_size=100)

    @pulumi.runtime.test
    def deploy():
        return [queue.resources, sub1.resources, sub2.resources]

    deploy()

    assert len(queue._subscriptions) == 2

    assert len(pulumi_mocks.created(R.EVENT_SOURCE_MAPPING)) == 2
    pulumi_mocks.assert_res(
        "multi-batch-queue-fast-processor-subscription-mapping",
        R.EVENT_SOURCE_MAPPING,
        {"batchSize": 1},
        partial=True,
    )
    pulumi_mocks.assert_res(
        "multi-batch-queue-batch-processor-subscription-mapping",
        R.EVENT_SOURCE_MAPPING,
        {"batchSize": 100},
        partial=True,
    )


def test_fifo_queue_with_subscription(pulumi_mocks):
    """Test that FIFO queue subscription creates EventSourceMapping with correct FIFO queue ARN."""
    queue = Queue("fifo-sub-test", fifo=True)

    subscription = queue.subscribe("processor", SIMPLE_HANDLER, batch_size=5)

    @pulumi.runtime.test
    def deploy():
        return [queue.resources, subscription.resources]

    deploy()

    assert len(pulumi_mocks.created(R.QUEUE)) == 1
    pulumi_mocks.assert_res("fifo-sub-test", R.QUEUE, {"fifoQueue": True}, partial=True)

    # Verify EventSourceMapping references the FIFO queue
    mapping = get_single_event_source_mapping(pulumi_mocks)

    expected_queue_arn = QUEUE_ARN_TEMPLATE.format(name=tn(TP + "fifo-sub-test"))
    assert mapping.inputs["eventSourceArn"] == expected_queue_arn
    assert mapping.inputs["batchSize"] == 5  # FIFO max is 10, we set 5
    assert mapping.inputs["enabled"] is True


@pulumi.runtime.test
def test_queue_subscription_parented_to_queue(pulumi_mocks):
    queue = Queue("parented-queue")
    sub = queue.subscribe("processor", SIMPLE_HANDLER)
    _ = sub.resources

    def check(urn):
        assert urn == (
            "urn:pulumi:stack::project::stelvio:aws:Queue$stelvio:aws:QueueSubscription"
            "::parented-queue-processor-subscription"
        )

    return sub.urn.apply(check)
