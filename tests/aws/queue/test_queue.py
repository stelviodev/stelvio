import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pulumi
import pytest
from pulumi.runtime import set_mocks

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

from ...test_utils import assert_config_dict_matches_dataclass
from ..pulumi_mocks import ACCOUNT_ID, DEFAULT_REGION, PulumiTestMocks, tn


def delete_files(directory: Path, filename: str):
    directory_path = directory
    for file_path in directory_path.rglob(filename):
        file_path.unlink()


@pytest.fixture(autouse=True)
def project_cwd(monkeypatch, pytestconfig):
    rootpath = pytestconfig.rootpath
    test_project_dir = rootpath / "tests" / "aws" / "sample_test_project"
    monkeypatch.chdir(test_project_dir)
    yield test_project_dir
    delete_files(test_project_dir, "stlv_resources.py")


QUEUE_ARN_TEMPLATE = f"arn:aws:sqs:{DEFAULT_REGION}:{ACCOUNT_ID}:{{name}}"

# Test prefix
TP = "test-test-"

# Test constants for frequently repeated handlers
SIMPLE_HANDLER = "functions/simple.handler"
USERS_HANDLER = "functions/users.handler"
ORDERS_HANDLER = "functions/orders.handler"


@pytest.fixture
def pulumi_mocks():
    mocks = PulumiTestMocks()
    set_mocks(mocks)
    return mocks


@pytest.fixture
def basic_queue():
    return Queue("test")


def assert_mapping_config(pulumi_mocks, batch_size=10, enabled=True):
    mapping = next(r for r in pulumi_mocks.created_resources if "EventSourceMapping" in r.typ)
    assert mapping.inputs["batchSize"] == batch_size
    assert mapping.inputs["enabled"] == enabled


@dataclass
class QueueTestCase:
    test_id: str
    name: str
    config_input: QueueConfig | QueueConfigDict | None
    expected_fifo: bool | None = None
    expected_delay: int = 0
    expected_visibility_timeout: int = 30


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
            sub.function_name.split(f"{queue.name}-", 1)[1] for sub in queue._subscriptions
        ]
        for name in expected_names:
            assert name in subscription_names

    # Check Pulumi mock resources
    functions = [
        r for r in pulumi_mocks.created_resources if r.typ == "aws:lambda/function:Function"
    ]
    mappings = [r for r in pulumi_mocks.created_resources if "EventSourceMapping" in r.typ]

    assert len(functions) == expected_count
    assert len(mappings) == expected_count

    # Verify each subscription has proper mapping and function with correct relationships
    queue_mock = next(r for r in pulumi_mocks.created_resources if r.typ == "aws:sqs/queue:Queue")
    expected_queue_name = tn(queue_mock.name)
    expected_queue_arn = f"arn:aws:sqs:{DEFAULT_REGION}:{ACCOUNT_ID}:{expected_queue_name}"

    for subscription in queue._subscriptions:
        # Extract subscription name from function_name
        subscription_name = subscription.function_name.split(f"{queue.name}-", 1)[1]

        # Find corresponding function and mapping in mocks by exact name match
        expected_function_name = subscription.function_name
        expected_mapping_name = f"{subscription.name}-mapping"

        function_mock = next((f for f in functions if f.name == TP + expected_function_name), None)
        mapping_mock = next((m for m in mappings if m.name == TP + expected_mapping_name), None)

        assert function_mock is not None, (
            f"Function not found for subscription '{subscription_name}'"
        )
        assert mapping_mock is not None, (
            f"EventSourceMapping not found for subscription '{subscription_name}'"
        )

        # Verify EventSourceMapping configuration
        esm_inputs = mapping_mock.inputs
        assert esm_inputs["batchSize"] == 10
        assert esm_inputs["enabled"] is True

        # Verify the mapping connects THIS specific function to the queue
        expected_function_name_in_mapping = tn(function_mock.name)
        assert esm_inputs["eventSourceArn"] == expected_queue_arn
        assert esm_inputs["functionName"] == expected_function_name_in_mapping

        # Critical: Verify that the mapping actually references the function we found
        # This ensures the mapping-function pairing is correct
        assert esm_inputs["functionName"] == tn(TP + expected_function_name), (
            f"Mapping for subscription '{subscription_name}' should reference function "
            f"'{TP + expected_function_name}' "
            f"but references '{esm_inputs['functionName']}'"
        )

        # Verify Lambda function has the correct SQS permissions
        verify_function_sqs_permissions(pulumi_mocks, function_mock, expected_queue_arn)

        # Verify Stelvio Function object was created correctly for this specific subscription
        expected_handler_input = (
            expected_configs.get(subscription_name) if expected_configs else None
        )
        verify_stelvio_function_for_subscription(queue, subscription_name, expected_handler_input)


def verify_function_sqs_permissions(pulumi_mocks, function_mock, expected_queue_arn):
    # Find the IAM policy for this function
    policies = [r for r in pulumi_mocks.created_resources if r.typ == "aws:iam/policy:Policy"]

    # Function policy name uses safe_name with "-p" suffix
    expected_policy_name = function_mock.name + "-p"
    function_policy = next((p for p in policies if p.name == expected_policy_name), None)

    assert function_policy is not None, f"IAM policy not found for function {function_mock.name}"

    # Parse the policy document and verify it contains the expected SQS permissions
    actual_statements = json.loads(function_policy.inputs["policy"])

    # Expected policy should contain SQS permissions
    expected_sqs_statement = {
        "actions": [
            "sqs:ReceiveMessage",
            "sqs:DeleteMessage",
            "sqs:GetQueueAttributes",
        ],
        "resources": [expected_queue_arn],
    }

    # Find the SQS statement in actual policy
    sqs_statements = [
        stmt for stmt in actual_statements if "sqs:ReceiveMessage" in stmt.get("actions", [])
    ]
    sqs_statement = sqs_statements[0] if sqs_statements else None

    assert sqs_statement is not None, "SQS permissions not found in function policy"
    assert sqs_statement == expected_sqs_statement, (
        f"SQS permissions mismatch.\nExpected: {expected_sqs_statement}\nGot: {sqs_statement}"
    )


def normalize_handler_input_to_function_config(handler_input):
    if isinstance(handler_input, str):
        return FunctionConfig(handler=handler_input)
    if isinstance(handler_input, dict):
        return FunctionConfig(**handler_input)
    if isinstance(handler_input, FunctionConfig):
        return handler_input
    raise TypeError(f"Unsupported handler input type: {type(handler_input)}")


def verify_stelvio_function_for_subscription(
    queue: Queue, subscription_name: str, expected_handler_input=None
):
    # Get all Function instances from the registry
    functions = ComponentRegistry._instances.get(Function, [])
    function_map = {f.name: f for f in functions}

    # Find this specific subscription's function
    expected_fn_name = f"{queue.name}-{subscription_name}"

    assert expected_fn_name in function_map, (
        f"Stelvio Function '{expected_fn_name}' not found in ComponentRegistry. "
        f"Available functions: {list(function_map.keys())}"
    )

    created_function: Function = function_map[expected_fn_name]

    # Verify function has the SQS link with correct name
    expected_sqs_link_name = f"{queue.name}-sqs"
    sqs_links = [
        link
        for link in created_function.config.links
        if hasattr(link, "name") and link.name == expected_sqs_link_name
    ]
    assert len(sqs_links) >= 1, (
        f"Function '{expected_fn_name}' missing SQS link "
        f"'{expected_sqs_link_name}'. "
        f"Links: {[getattr(link, 'name', str(link)) for link in created_function.config.links]}"
    )

    # Verify subscription config was properly applied to Function
    if expected_handler_input is not None:
        expected_config = normalize_handler_input_to_function_config(expected_handler_input)

        # Compare the key configuration fields
        assert created_function.config.handler == expected_config.handler, (
            f"Function handler mismatch: expected {expected_config.handler}, "
            f"got {created_function.config.handler}"
        )

        # Only check memory/timeout if they were explicitly set in expected config
        if expected_config.memory is not None:
            assert created_function.config.memory == expected_config.memory, (
                f"Function memory mismatch: expected {expected_config.memory}, "
                f"got {created_function.config.memory}"
            )
        if expected_config.timeout is not None:
            assert created_function.config.timeout == expected_config.timeout, (
                f"Function timeout mismatch: expected {expected_config.timeout}, "
                f"got {created_function.config.timeout}"
            )


def verify_queue_resources(pulumi_mocks, test_case: QueueTestCase):
    queues = [r for r in pulumi_mocks.created_resources if r.typ == "aws:sqs/queue:Queue"]
    assert len(queues) == 1
    queue_args = queues[0]

    assert queue_args.name == TP + test_case.name
    assert queue_args.inputs.get("delaySeconds") == test_case.expected_delay
    actual_visibility = queue_args.inputs.get("visibilityTimeoutSeconds")
    assert actual_visibility == test_case.expected_visibility_timeout
    assert queue_args.inputs.get("fifoQueue") == test_case.expected_fifo


# Test case definitions
BASIC_QUEUE_TC = QueueTestCase(
    test_id="basic_queue",
    name="basic-queue",
    config_input=None,
    expected_fifo=None,
    expected_delay=0,
    expected_visibility_timeout=30,
)

FIFO_QUEUE_TC = QueueTestCase(
    test_id="fifo_queue",
    name="fifo-queue",
    config_input=QueueConfig(fifo=True),
    expected_fifo=True,
    expected_delay=0,
    expected_visibility_timeout=30,
)

DELAYED_QUEUE_TC = QueueTestCase(
    test_id="delayed_queue",
    name="delayed-queue",
    config_input={"delay": 5},
    expected_fifo=None,
    expected_delay=5,
    expected_visibility_timeout=30,
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
@pulumi.runtime.test
def test_queue_creation(pulumi_mocks, test_case):
    if test_case.config_input is None:
        queue = Queue(test_case.name)
    elif isinstance(test_case.config_input, dict):
        queue = Queue(test_case.name, **test_case.config_input)
    else:
        queue = Queue(test_case.name, config=test_case.config_input)

    def check_resources(_):
        verify_queue_resources(pulumi_mocks, test_case)

    queue.arn.apply(check_resources)


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

        # Permission should include SQS actions
        permission = permissions[0]
        expected_actions = [
            "sqs:SendMessage",
            "sqs:ReceiveMessage",
            "sqs:DeleteMessage",
            "sqs:GetQueueAttributes",
            "sqs:GetQueueUrl",
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


def test_queue_invalid_config_type():
    """Test that invalid config types raise TypeError."""
    with pytest.raises(
        TypeError, match="Invalid config type: expected QueueConfig or QueueConfigDict"
    ):
        Queue("test", config="invalid")


def test_dlq_config_from_string():
    """Test DLQ config normalization from string."""
    queue = Queue("test", dlq="my-dlq")

    assert isinstance(queue._config.dlq, DlqConfig)
    assert queue._config.dlq.queue == "my-dlq"
    assert queue._config.dlq.retry == 3


def test_dlq_config_from_dict():
    """Test DLQ config normalization from dict."""
    queue = Queue("test", dlq={"queue": "my-dlq", "retry": 5})

    assert isinstance(queue._config.dlq, DlqConfig)
    assert queue._config.dlq.queue == "my-dlq"
    assert queue._config.dlq.retry == 5


def test_dlq_config_from_queue_component():
    """Test DLQ config with Queue component reference."""
    dlq = Queue("my-dlq")
    queue = Queue("test", dlq=DlqConfig(queue=dlq, retry=5))

    assert isinstance(queue._config.dlq, DlqConfig)
    assert queue._config.dlq.queue is dlq
    assert queue._config.dlq.retry == 5


@pulumi.runtime.test
def test_dlq_with_queue_reference(pulumi_mocks):
    """Test that DLQ is properly configured when referencing Queue component."""
    dlq = Queue("orders-dlq")
    main_queue = Queue("orders", dlq=DlqConfig(queue=dlq, retry=5))

    def check_dlq_config(_):
        queues = [r for r in pulumi_mocks.created_resources if r.typ == "aws:sqs/queue:Queue"]
        assert len(queues) == 2

        # Find the main queue (not the DLQ)
        main_queue_resource = next((q for q in queues if q.name == TP + "orders"), None)
        assert main_queue_resource is not None

        # Verify redrive policy is set
        redrive_policy = main_queue_resource.inputs.get("redrivePolicy")
        assert redrive_policy is not None

    pulumi.Output.all(main_queue.arn, dlq.arn).apply(check_dlq_config)


@pulumi.runtime.test
def test_dlq_with_string_reference(pulumi_mocks):
    """Test that DLQ is properly configured when referencing by name."""
    dlq = Queue("orders-dlq")
    main_queue = Queue("orders", dlq="orders-dlq")

    def check_dlq_config(_):
        queues = [r for r in pulumi_mocks.created_resources if r.typ == "aws:sqs/queue:Queue"]
        assert len(queues) == 2

        # Find the main queue (not the DLQ)
        main_queue_resource = next((q for q in queues if q.name == TP + "orders"), None)
        assert main_queue_resource is not None

        # Verify redrive policy is set
        redrive_policy = main_queue_resource.inputs.get("redrivePolicy")
        assert redrive_policy is not None

    pulumi.Output.all(main_queue.arn, dlq.arn).apply(check_dlq_config)


def test_dlq_not_found_error():
    """Test that referencing non-existent DLQ by name raises error."""
    queue = Queue("test", dlq="non-existent-dlq")

    with pytest.raises(ValueError, match="Dead-letter queue 'non-existent-dlq' not found"):
        _ = queue.resources


@pulumi.runtime.test
def test_duplicate_subscription_names(pulumi_mocks):
    queue = Queue("test-queue")

    queue.subscribe("processor", SIMPLE_HANDLER)

    with pytest.raises(ValueError, match="Subscription 'processor' already exists"):
        queue.subscribe("processor", USERS_HANDLER)


@pulumi.runtime.test
def test_subscription_basic(pulumi_mocks):
    """Basic subscription functionality test."""
    queue = Queue("basic-sub")

    subscription = queue.subscribe("test", SIMPLE_HANDLER)

    def check_subscription(_):
        verify_subscription_resources(pulumi_mocks, queue, 1, ["test"])

    # Trigger resource creation and then verify
    pulumi.Output.all(queue.arn, subscription.resources.event_source_mapping.arn).apply(
        check_subscription
    )


@pytest.mark.parametrize(
    ("handler_input", "test_name"),
    [
        (SIMPLE_HANDLER, "string"),
        ({"handler": USERS_HANDLER, "memory": 512}, "dict_as_handler"),
        (FunctionConfig(handler=ORDERS_HANDLER, timeout=120), "config"),
    ],
)
@pulumi.runtime.test
def test_subscription_handler_types(pulumi_mocks, handler_input, test_name):
    """Test all supported handler input types."""
    queue = Queue(f"sub-{test_name}")

    subscription = queue.subscribe("test", handler_input)

    def check_handler_type(_):
        verify_subscription_resources(
            pulumi_mocks,
            queue,
            expected_count=1,
            expected_names=["test"],
            expected_configs={"test": handler_input},
        )

    esm = subscription.resources.event_source_mapping
    pulumi.Output.all([queue.arn, esm.arn]).apply(check_handler_type)


@pulumi.runtime.test
def test_subscription_function_config_opts(pulumi_mocks):
    queue = Queue("dict-unpacked")

    subscription = queue.subscribe("test", handler=USERS_HANDLER, memory=512, timeout=30)

    def check_dict_unpacked(_):
        verify_subscription_resources(
            pulumi_mocks,
            queue,
            expected_count=1,
            expected_names=["test"],
            expected_configs={"test": {"handler": USERS_HANDLER, "memory": 512, "timeout": 30}},
        )

    esm = subscription.resources.event_source_mapping
    pulumi.Output.all([queue.arn, esm.arn]).apply(check_dict_unpacked)


@pulumi.runtime.test
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

    def check_link_merging(_):
        # Verify subscription created correctly
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

    esm = subscription.resources.event_source_mapping
    pulumi.Output.all([queue.arn, esm.arn]).apply(check_link_merging)


@pulumi.runtime.test
def test_subscription_with_multiple_handlers(pulumi_mocks):
    queue = Queue("multi-subscription")

    sub1 = queue.subscribe("processor", SIMPLE_HANDLER)
    sub2 = queue.subscribe("audit", {"handler": USERS_HANDLER, "memory": 256})
    sub3 = queue.subscribe("config", FunctionConfig(handler=ORDERS_HANDLER, timeout=60))

    def check_subscription_resources(_):
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

    # Wait for both queue AND all EventSourceMappings to be created
    all_mapping_arns = [sub.resources.event_source_mapping.arn for sub in [sub1, sub2, sub3]]
    pulumi.Output.all([queue.arn, *all_mapping_arns]).apply(check_subscription_resources)


@pulumi.runtime.test
def test_subscription_batch_size(pulumi_mocks, basic_queue):
    subscription = basic_queue.subscribe(
        "batch-test",
        SIMPLE_HANDLER,
        batch_size=5,
    )

    def check_config(_):
        assert_mapping_config(pulumi_mocks, batch_size=5)

    esm = subscription.resources.event_source_mapping
    pulumi.Output.all([basic_queue.arn, esm.arn]).apply(check_config)


@pulumi.runtime.test
def test_fifo_queue_naming(pulumi_mocks):
    """Test that FIFO queues get .fifo suffix."""
    queue = Queue("fifo-test", fifo=True)
    _ = queue.resources

    def check_fifo_naming(_):
        queues = [r for r in pulumi_mocks.created_resources if r.typ == "aws:sqs/queue:Queue"]
        assert len(queues) == 1
        queue_resource = queues[0]
        # FIFO queues should have name set with .fifo suffix
        assert queue_resource.inputs.get("name").endswith(".fifo")
        assert queue_resource.inputs.get("fifoQueue") is True
        assert queue_resource.inputs.get("contentBasedDeduplication") is True

    queue.arn.apply(check_fifo_naming)


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
