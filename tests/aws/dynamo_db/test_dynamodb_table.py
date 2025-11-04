import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pulumi
import pytest
from pulumi.runtime import set_mocks

from stelvio.aws.dynamo_db import (
    DynamoTable,
    DynamoTableConfig,
    DynamoTableConfigDict,
    FieldType,
    GlobalIndex,
    LocalIndex,
    StreamView,
    _convert_projection,
)
from stelvio.aws.function import Function, FunctionConfig
from stelvio.aws.permission import AwsPermission
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


TABLE_ARN_TEMPLATE = f"arn:aws:dynamodb:{DEFAULT_REGION}:{ACCOUNT_ID}:table/{{name}}"

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
def basic_table():
    return DynamoTable("test", fields={"id": "string"}, partition_key="id", stream="keys-only")


def assert_mapping_config(pulumi_mocks, batch_size=100, starting_position="LATEST", filters=None):
    mapping = next(r for r in pulumi_mocks.created_resources if "EventSourceMapping" in r.typ)
    assert mapping.inputs["batchSize"] == batch_size
    assert mapping.inputs["startingPosition"] == starting_position

    if filters:
        assert mapping.inputs["filterCriteria"]["filters"] == filters
    else:
        assert mapping.inputs.get("filterCriteria") is None


@dataclass
class DynamoTableTestCase:
    test_id: str
    name: str
    config_input: DynamoTableConfig | DynamoTableConfigDict
    expected_fields: dict[str, str]
    expected_partition_key: str
    expected_sort_key: str | None = None
    expected_local_indexes: list[dict] = field(default_factory=list)
    expected_global_indexes: list[dict] = field(default_factory=list)
    expected_stream_enabled: bool = False
    expected_stream_view_type: str | None = None


def verify_subscription_resources(
    pulumi_mocks,
    table: DynamoTable,
    expected_count: int,
    expected_names: list[str] | None = None,
    expected_configs: dict[str, Any] | None = None,
):
    # Check subscriptions in table
    assert len(table._subscriptions) == expected_count

    if expected_names:
        subscription_names = [
            sub.function_name.split(f"{table.name}-", 1)[1] for sub in table._subscriptions
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
    table_mock = pulumi_mocks.created_dynamo_tables(TP + table.name)[0]
    expected_table_name = tn(table_mock.name)
    expected_stream_arn = (
        f"arn:aws:dynamodb:{DEFAULT_REGION}:{ACCOUNT_ID}:table/{expected_table_name}"
        f"/stream/2025-01-01T00:00:00.000"
    )

    for subscription in table._subscriptions:
        # Extract subscription name from function_name
        subscription_name = subscription.function_name.split(f"{table.name}-", 1)[1]

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
        assert esm_inputs["startingPosition"] == "LATEST"
        assert esm_inputs["batchSize"] == 100
        assert esm_inputs["maximumBatchingWindowInSeconds"] == 0

        # Verify the mapping connects THIS specific function to the table stream
        expected_function_name_in_mapping = tn(function_mock.name)
        assert esm_inputs["eventSourceArn"] == expected_stream_arn
        assert esm_inputs["functionName"] == expected_function_name_in_mapping

        # Critical: Verify that the mapping actually references the function we found
        # This ensures the mapping-function pairing is correct
        assert esm_inputs["functionName"] == tn(TP + expected_function_name), (
            f"Mapping for subscription '{subscription_name}' should reference function "
            f"'{TP + expected_function_name}' "
            f"but references '{esm_inputs['functionName']}'"
        )

        # Verify Lambda function has the correct DynamoDB stream permissions
        verify_function_stream_permissions(pulumi_mocks, function_mock, expected_stream_arn)

        # Verify Stelvio Function object was created correctly for this specific subscription
        expected_handler_input = (
            expected_configs.get(subscription_name) if expected_configs else None
        )
        verify_stelvio_function_for_subscription(table, subscription_name, expected_handler_input)


def verify_function_stream_permissions(pulumi_mocks, function_mock, expected_stream_arn):
    # Find the IAM policy for this function
    policies = [r for r in pulumi_mocks.created_resources if r.typ == "aws:iam/policy:Policy"]

    # Function policy name uses safe_name with "-p" suffix
    expected_policy_name = function_mock.name + "-p"
    function_policy = next((p for p in policies if p.name == expected_policy_name), None)

    assert function_policy is not None, f"IAM policy not found for function {function_mock.name}"

    # Parse the policy document and verify it contains the expected stream permissions

    actual_statements = json.loads(function_policy.inputs["policy"])

    # Expected policy should contain basic Lambda execution + DynamoDB stream permissions
    expected_stream_statement = {
        "actions": [
            "dynamodb:DescribeStream",
            "dynamodb:GetRecords",
            "dynamodb:GetShardIterator",
            "dynamodb:ListStreams",
        ],
        "resources": [expected_stream_arn],
    }

    # Find the stream statement in actual policy
    stream_statements = [
        stmt for stmt in actual_statements if "dynamodb:DescribeStream" in stmt.get("actions", [])
    ]
    stream_statement = stream_statements[0] if stream_statements else None

    assert stream_statement is not None, "DynamoDB stream permissions not found in function policy"
    assert stream_statement == expected_stream_statement, (
        f"Stream permissions mismatch.\n"
        f"Expected: {expected_stream_statement}\n"
        f"Got: {stream_statement}"
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
    table: DynamoTable, subscription_name: str, expected_handler_input=None
):
    # Get all Function instances from the registry
    functions = ComponentRegistry._instances.get(Function, [])
    function_map = {f.name: f for f in functions}

    # Find this specific subscription's function
    expected_fn_name = f"{table.name}-{subscription_name}"

    assert expected_fn_name in function_map, (
        f"Stelvio Function '{expected_fn_name}' not found in ComponentRegistry. "
        f"Available functions: {list(function_map.keys())}"
    )

    created_function: Function = function_map[expected_fn_name]

    # Verify function has the DynamoDB stream link with correct name
    expected_stream_link_name = f"{table.name}-stream"
    stream_links = [
        link
        for link in created_function.config.links
        if hasattr(link, "name") and link.name == expected_stream_link_name
    ]
    assert len(stream_links) >= 1, (
        f"Function '{expected_fn_name}' missing DynamoDB stream link "
        f"'{expected_stream_link_name}'. "
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


def verify_table_resources(pulumi_mocks, test_case: DynamoTableTestCase):
    tables = pulumi_mocks.created_dynamo_tables(TP + test_case.name)
    assert len(tables) == 1
    table_args = tables[0]

    assert table_args.name == TP + test_case.name
    assert table_args.inputs["billingMode"] == "PAY_PER_REQUEST"
    assert table_args.inputs["hashKey"] == test_case.expected_partition_key
    assert table_args.inputs.get("rangeKey") == test_case.expected_sort_key
    # Check attributes - normalize to dict for comparison
    actual_attributes = {attr["name"]: attr["type"] for attr in table_args.inputs["attributes"]}
    assert actual_attributes == test_case.expected_fields
    # Check indexes
    actual_local_indexes = table_args.inputs.get("localSecondaryIndexes") or []
    actual_global_indexes = table_args.inputs.get("globalSecondaryIndexes") or []
    assert actual_local_indexes == test_case.expected_local_indexes
    assert actual_global_indexes == test_case.expected_global_indexes
    # Check stream configuration
    assert table_args.inputs.get("streamEnabled") == test_case.expected_stream_enabled
    assert table_args.inputs.get("streamViewType") == test_case.expected_stream_view_type


# Test case definitions
BASIC_TABLE_TC = DynamoTableTestCase(
    test_id="basic_table",
    name="basic-table",
    config_input=DynamoTableConfig(fields={"id": FieldType.STRING}, partition_key="id"),
    expected_fields={"id": "S"},
    expected_partition_key="id",
)

TABLE_WITH_SORT_KEY_TC = DynamoTableTestCase(
    test_id="table_with_sort_key",
    name="table-with-sort-key",
    config_input={
        "fields": {"pk": "string", "sk": "string"},
        "partition_key": "pk",
        "sort_key": "sk",
    },
    expected_fields={"pk": "S", "sk": "S"},
    expected_partition_key="pk",
    expected_sort_key="sk",
)

STRING_LITERALS_TC = DynamoTableTestCase(
    test_id="string_literals",
    name="string-literals",
    config_input=DynamoTableConfig(
        fields={"id": "string", "count": "number", "data": "B"}, partition_key="id"
    ),
    expected_fields={"id": "S", "count": "N", "data": "B"},
    expected_partition_key="id",
)

LOCAL_INDEX_TC = DynamoTableTestCase(
    test_id="local_index",
    name="local-index",
    config_input=DynamoTableConfig(
        fields={"id": FieldType.STRING, "timestamp": FieldType.NUMBER, "status": FieldType.STRING},
        partition_key="id",
        sort_key="timestamp",
        local_indexes={"status-index": LocalIndex(sort_key="status", projections=["timestamp"])},
    ),
    expected_fields={"id": "S", "timestamp": "N", "status": "S"},
    expected_partition_key="id",
    expected_sort_key="timestamp",
    expected_local_indexes=[
        {
            "name": "status-index",
            "rangeKey": "status",
            "projectionType": "INCLUDE",
            "nonKeyAttributes": ["timestamp"],
        }
    ],
)

GLOBAL_INDEX_TC = DynamoTableTestCase(
    test_id="global_index",
    name="global-index",
    config_input={
        "fields": {"id": "string", "status": "string", "created": "number"},
        "partition_key": "id",
        "global_indexes": {
            "status-index": {
                "partition_key": "status",
                "sort_key": "created",
                "projections": "all",
            }
        },
    },
    expected_fields={"id": "S", "status": "S", "created": "N"},
    expected_partition_key="id",
    expected_global_indexes=[
        {
            "name": "status-index",
            "hashKey": "status",
            "rangeKey": "created",
            "projectionType": "ALL",
        }
    ],
)

GLOBAL_INDEX_NO_SORT_TC = replace(
    GLOBAL_INDEX_TC,
    test_id="global_index_no_sort",
    name="global-no-sort",
    config_input=DynamoTableConfig(
        fields={"id": FieldType.STRING, "status": FieldType.STRING},
        partition_key="id",
        global_indexes={"status-index": GlobalIndex(partition_key="status")},
    ),
    expected_fields={"id": "S", "status": "S"},
    expected_global_indexes=[
        {"name": "status-index", "hashKey": "status", "projectionType": "KEYS_ONLY"}
    ],
)

KEYS_ONLY_PROJECTION_TC = DynamoTableTestCase(
    test_id="keys_only_projection",
    name="keys-only",
    config_input=DynamoTableConfig(
        fields={"id": FieldType.STRING, "status": FieldType.STRING},
        partition_key="id",
        local_indexes={
            "status-index": LocalIndex(sort_key="status")  # Default is "keys-only"
        },
    ),
    expected_fields={"id": "S", "status": "S"},
    expected_partition_key="id",
    expected_local_indexes=[
        {"name": "status-index", "rangeKey": "status", "projectionType": "KEYS_ONLY"}
    ],
)

MULTIPLE_INDEXES_TC = DynamoTableTestCase(
    test_id="multiple_indexes",
    name="multi-index",
    config_input=DynamoTableConfig(
        fields={
            "id": FieldType.STRING,
            "status": FieldType.STRING,
            "created": FieldType.NUMBER,
            "updated": FieldType.NUMBER,
            "category": FieldType.STRING,
        },
        partition_key="id",
        sort_key="created",
        local_indexes={
            "status-index": LocalIndex(sort_key="status"),
            "updated-index": LocalIndex(sort_key="updated", projections="all"),
        },
        global_indexes={
            "status-created": GlobalIndex(partition_key="status", sort_key="created"),
            "category-only": GlobalIndex(partition_key="category", projections=["id", "status"]),
        },
    ),
    expected_fields={"id": "S", "status": "S", "created": "N", "updated": "N", "category": "S"},
    expected_partition_key="id",
    expected_sort_key="created",
    expected_local_indexes=[
        {"name": "status-index", "rangeKey": "status", "projectionType": "KEYS_ONLY"},
        {"name": "updated-index", "rangeKey": "updated", "projectionType": "ALL"},
    ],
    expected_global_indexes=[
        {
            "name": "status-created",
            "hashKey": "status",
            "rangeKey": "created",
            "projectionType": "KEYS_ONLY",
        },
        {
            "name": "category-only",
            "hashKey": "category",
            "projectionType": "INCLUDE",
            "nonKeyAttributes": ["id", "status"],
        },
    ],
)

NO_INDEXES_TC = DynamoTableTestCase(
    test_id="no_indexes",
    name="no-indexes",
    config_input=DynamoTableConfig(fields={"id": FieldType.STRING}, partition_key="id"),
    expected_fields={"id": "S"},
    expected_partition_key="id",
)

# Stream test cases
STREAM_KEYS_ONLY_TC = DynamoTableTestCase(
    test_id="stream_keys_only",
    name="stream-keys-only",
    config_input=DynamoTableConfig(
        fields={"id": FieldType.STRING}, partition_key="id", stream="keys-only"
    ),
    expected_fields={"id": "S"},
    expected_partition_key="id",
    expected_stream_enabled=True,
    expected_stream_view_type="KEYS_ONLY",
)

STREAM_NEW_IMAGE_TC = DynamoTableTestCase(
    test_id="stream_new_image",
    name="stream-new-image",
    config_input={"fields": {"id": "string"}, "partition_key": "id", "stream": "new-image"},
    expected_fields={"id": "S"},
    expected_partition_key="id",
    expected_stream_enabled=True,
    expected_stream_view_type="NEW_IMAGE",
)

STREAM_ENUM_TC = DynamoTableTestCase(
    test_id="stream_enum",
    name="stream-enum",
    config_input=DynamoTableConfig(
        fields={"id": FieldType.STRING}, partition_key="id", stream=StreamView.NEW_AND_OLD_IMAGES
    ),
    expected_fields={"id": "S"},
    expected_partition_key="id",
    expected_stream_enabled=True,
    expected_stream_view_type="NEW_AND_OLD_IMAGES",
)

NO_STREAM_TC = DynamoTableTestCase(
    test_id="no_stream",
    name="no-stream",
    config_input=DynamoTableConfig(fields={"id": FieldType.STRING}, partition_key="id"),
    expected_fields={"id": "S"},
    expected_partition_key="id",
    expected_stream_enabled=False,
    expected_stream_view_type=None,
)


def test_config_dict_matches_dataclass():
    """Test that DynamoTableConfigDict matches DynamoTableConfig."""
    assert_config_dict_matches_dataclass(DynamoTableConfig, DynamoTableConfigDict)


@pytest.mark.parametrize(
    ("projections", "expected"),
    [
        ("keys-only", {"projection_type": "KEYS_ONLY"}),
        ("all", {"projection_type": "ALL"}),
        (
            ["attr1", "attr2"],
            {"projection_type": "INCLUDE", "non_key_attributes": ["attr1", "attr2"]},
        ),
    ],
)
def test_convert_projection(projections, expected):
    """Test projection conversion helper function."""

    assert _convert_projection(projections) == expected


@pytest.mark.parametrize(
    "test_case",
    [
        BASIC_TABLE_TC,
        TABLE_WITH_SORT_KEY_TC,
        STRING_LITERALS_TC,
        LOCAL_INDEX_TC,
        GLOBAL_INDEX_TC,
        GLOBAL_INDEX_NO_SORT_TC,
        KEYS_ONLY_PROJECTION_TC,
        MULTIPLE_INDEXES_TC,
        NO_INDEXES_TC,
        STREAM_KEYS_ONLY_TC,
        STREAM_NEW_IMAGE_TC,
        STREAM_ENUM_TC,
        NO_STREAM_TC,
    ],
    ids=lambda tc: tc.test_id,
)
@pulumi.runtime.test
def test_dynamo_table_creation(pulumi_mocks, test_case):
    if isinstance(test_case.config_input, dict):
        table = DynamoTable(test_case.name, **test_case.config_input)
    else:
        table = DynamoTable(test_case.name, config=test_case.config_input)

    def check_resources(_):
        verify_table_resources(pulumi_mocks, test_case)

    table.arn.apply(check_resources)


@pulumi.runtime.test
def test_table_properties(pulumi_mocks):
    # Arrange
    table = DynamoTable("my-table", fields={"id": FieldType.STRING}, partition_key="id")
    # Act
    _ = table.resources

    # Assert
    def check_resources(args):
        table_id, arn = args
        assert table_id == TP + "my-table-test-id"
        assert arn == TABLE_ARN_TEMPLATE.format(name=tn(TP + "my-table"))

    pulumi.Output.all(table.resources.table.id, table.arn).apply(check_resources)


@pulumi.runtime.test
def test_dynamo_table_link(pulumi_mocks):
    # Arrange
    table_name = "my-table"
    table = DynamoTable(table_name, fields={"id": FieldType.STRING}, partition_key="id")

    # Act
    link = table.link()

    # Assert
    def verify_permissions(args):
        properties, permissions = args

        # Properties should include table name and ARN
        expected_properties = {
            "table_name": TP + table_name + "-test-name",
            "table_arn": TABLE_ARN_TEMPLATE.format(name=tn(TP + table_name)),
        }
        assert properties == expected_properties

        # Should have 2 permissions: table + index
        assert len(permissions) == 2

        # First permission: table CRUD operations
        table_permission = permissions[0]
        expected_table_actions = [
            "dynamodb:Scan",
            "dynamodb:Query",
            "dynamodb:GetItem",
            "dynamodb:PutItem",
            "dynamodb:UpdateItem",
            "dynamodb:DeleteItem",
        ]
        assert sorted(table_permission.actions) == sorted(expected_table_actions)
        assert len(table_permission.resources) == 1

        def verify_table_resource(resource):
            table_arn = TABLE_ARN_TEMPLATE.format(name=tn(TP + table_name))
            assert resource == table_arn

        table_permission.resources[0].apply(verify_table_resource)

        # Second permission: index read-only operations
        index_permission = permissions[1]
        assert sorted(index_permission.actions) == sorted(["dynamodb:Query", "dynamodb:Scan"])
        assert len(index_permission.resources) == 1

        def verify_index_resource(resource):
            table_arn = TABLE_ARN_TEMPLATE.format(name=tn(TP + table_name))
            expected_index_arn = f"{table_arn}/index/*"
            assert resource == expected_index_arn

        index_permission.resources[0].apply(verify_index_resource)

    pulumi.Output.all(link.properties, link.permissions).apply(verify_permissions)


@pytest.mark.parametrize(
    ("config_args", "expected_error"),
    [
        (
            {"fields": {"id": FieldType.STRING}, "partition_key": "invalid_key"},
            "partition_key 'invalid_key' not in fields list",
        ),
        (
            {
                "fields": {"id": FieldType.STRING},
                "partition_key": "id",
                "sort_key": "invalid_sort",
            },
            "sort_key 'invalid_sort' not in fields list",
        ),
        (
            {
                "fields": {"id": FieldType.STRING},
                "partition_key": "id",
                "local_indexes": {"test-index": LocalIndex(sort_key="invalid_key")},
            },
            "Local index 'test-index' sort_key 'invalid_key' not in fields list",
        ),
        (
            {
                "fields": {"id": FieldType.STRING},
                "partition_key": "id",
                "local_indexes": {"test-index": {"sort_key": "invalid_key"}},
            },
            "Local index 'test-index' sort_key 'invalid_key' not in fields list",
        ),
        (
            {
                "fields": {"id": FieldType.STRING},
                "partition_key": "id",
                "global_indexes": {"test-index": GlobalIndex(partition_key="invalid_key")},
            },
            "Global index 'test-index' partition_key 'invalid_key' not in fields list",
        ),
        (
            {
                "fields": {"id": FieldType.STRING, "status": FieldType.STRING},
                "partition_key": "id",
                "global_indexes": {
                    "test-index": GlobalIndex(partition_key="status", sort_key="invalid_key")
                },
            },
            "Global index 'test-index' sort_key 'invalid_key' not in fields list",
        ),
        (
            {
                "fields": {"id": FieldType.STRING},
                "partition_key": "id",
                "global_indexes": {"test-index": {"partition_key": "invalid_key"}},
            },
            "Global index 'test-index' partition_key 'invalid_key' not in fields list",
        ),
    ],
)
def test_dynamo_table_config_validation(config_args, expected_error):
    """Test validation of DynamoTableConfig."""
    with pytest.raises(ValueError, match=expected_error):
        DynamoTableConfig(**config_args)


def test_dynamo_table_invalid_config_combination():
    """Test that combining config parameter with options raises ValueError."""
    config = DynamoTableConfig(fields={"id": FieldType.STRING}, partition_key="id")

    with pytest.raises(
        ValueError, match="cannot combine 'config' parameter with additional options"
    ):
        DynamoTable("test", config=config, stream="keys-only")


def test_dynamo_table_config_dict_support():
    config_dict = {
        "fields": {"id": FieldType.STRING},
        "partition_key": "id",
        "stream": "keys-only",
    }

    table = DynamoTable("test", config=config_dict)

    assert table.partition_key == "id"
    assert table._config.stream_enabled is True


def test_dynamo_table_invalid_config_type():
    """Test that invalid config types raise TypeError."""
    with pytest.raises(
        TypeError, match="Invalid config type: expected DynamoTableConfig or DynamoTableConfigDict"
    ):
        DynamoTable("test", config="invalid")


@pulumi.runtime.test
def test_stream_arn_property(pulumi_mocks):
    """Test stream_arn property behavior."""
    # Test table with stream
    stream_table = DynamoTable(
        "stream-table",
        fields={"id": FieldType.STRING},
        partition_key="id",
        stream="new-and-old-images",
    )

    # Test table without stream
    no_stream_table = DynamoTable(
        "no-stream-table", fields={"id": FieldType.STRING}, partition_key="id"
    )

    # Trigger resource creation
    _ = stream_table.resources
    _ = no_stream_table.resources

    # Check stream table properties
    def check_stream_table(stream_arn):
        assert stream_arn is not None
        # Stream ARN should be in expected format
        expected_pattern = (
            f"arn:aws:dynamodb:{DEFAULT_REGION}:{ACCOUNT_ID}:table/"
            f"{tn(TP + 'stream-table')}/stream/"
        )
        assert stream_arn.startswith(expected_pattern)

    if stream_table.stream_arn:
        stream_table.stream_arn.apply(check_stream_table)

    # Check no-stream table properties
    assert no_stream_table.stream_arn is None


@pulumi.runtime.test
def test_subscription_validation(pulumi_mocks):
    no_stream_table = DynamoTable("no-stream", fields={"id": FieldType.STRING}, partition_key="id")

    with pytest.raises(ValueError, match="streams are not enabled"):
        no_stream_table.subscribe("test", "functions/handler.py")


@pulumi.runtime.test
def test_duplicate_subscription_names(pulumi_mocks):
    table = DynamoTable(
        "stream-table", fields={"id": FieldType.STRING}, partition_key="id", stream="new-image"
    )

    table.subscribe("processor", SIMPLE_HANDLER)

    with pytest.raises(ValueError, match="Subscription 'processor' already exists"):
        table.subscribe("processor", USERS_HANDLER)


@pulumi.runtime.test
def test_subscription_basic(pulumi_mocks):
    """Basic subscription functionality test."""
    table = DynamoTable(
        "basic-sub", fields={"id": FieldType.STRING}, partition_key="id", stream="keys-only"
    )

    subscription = table.subscribe("test", SIMPLE_HANDLER)

    def check_subscription(_):
        verify_subscription_resources(pulumi_mocks, table, 1, ["test"])

    # Trigger resource creation and then verify
    pulumi.Output.all(table.arn, subscription.resources.event_source_mapping.arn).apply(
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
    table = DynamoTable(
        f"sub-{test_name}", fields={"id": FieldType.STRING}, partition_key="id", stream="keys-only"
    )

    subscription = table.subscribe("test", handler_input)

    def check_handler_type(_):
        verify_subscription_resources(
            pulumi_mocks,
            table,
            expected_count=1,
            expected_names=["test"],
            expected_configs={"test": handler_input},
        )

    esm = subscription.resources.event_source_mapping
    pulumi.Output.all([table.arn, esm.arn]).apply(check_handler_type)


@pulumi.runtime.test
def test_subscription_function_config_opts(pulumi_mocks):
    table = DynamoTable(
        "dict-unpacked", fields={"id": FieldType.STRING}, partition_key="id", stream="keys-only"
    )

    subscription = table.subscribe("test", handler=USERS_HANDLER, memory=512, timeout=30)

    def check_dict_unpacked(_):
        verify_subscription_resources(
            pulumi_mocks,
            table,
            expected_count=1,
            expected_names=["test"],
            expected_configs={"test": {"handler": USERS_HANDLER, "memory": 512, "timeout": 30}},
        )

    esm = subscription.resources.event_source_mapping
    pulumi.Output.all([table.arn, esm.arn]).apply(check_dict_unpacked)


@pulumi.runtime.test
def test_subscription_link_merging(pulumi_mocks):
    """Test that user-provided links are properly merged with mandatory stream permissions."""

    table = DynamoTable(
        "link-merge-test", fields={"id": FieldType.STRING}, partition_key="id", stream="keys-only"
    )

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
    subscription = table.subscribe("processor", function_config)

    def check_link_merging(_):
        # Verify subscription created correctly
        verify_subscription_resources(
            pulumi_mocks,
            table,
            expected_count=1,
            expected_names=["processor"],
            expected_configs={"processor": function_config},
        )

        # Additional verification: check that the created Function has both links
        from stelvio.aws.function import Function
        from stelvio.component import ComponentRegistry

        functions = ComponentRegistry._instances.get(Function, [])
        function_map = {f.name: f for f in functions}

        created_function = function_map[f"{table.name}-processor"]

        # Should have 2 links: stream link + user's custom link
        assert len(created_function.config.links) == 2, (
            f"Expected 2 links (stream + custom), got {len(created_function.config.links)}"
        )

        # Verify stream link is present
        stream_links = [
            link
            for link in created_function.config.links
            if hasattr(link, "name") and link.name == f"{table.name}-stream"
        ]
        assert len(stream_links) == 1, "Stream link not found in merged links"

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
    pulumi.Output.all([table.arn, esm.arn]).apply(check_link_merging)


@pulumi.runtime.test
def test_subscription_with_multiple_handlers(pulumi_mocks):
    table = DynamoTable(
        "stream-with-subscription",
        fields={"id": FieldType.STRING},
        partition_key="id",
        stream="new-and-old-images",
    )

    sub1 = table.subscribe("processor", SIMPLE_HANDLER)
    sub2 = table.subscribe("audit", {"handler": USERS_HANDLER, "memory": 256})
    sub3 = table.subscribe("config", FunctionConfig(handler=ORDERS_HANDLER, timeout=60))

    def check_subscription_resources(_):
        verify_subscription_resources(
            pulumi_mocks,
            table,
            expected_count=3,
            expected_names=["processor", "audit", "config"],
            expected_configs={
                "processor": SIMPLE_HANDLER,
                "audit": {"handler": USERS_HANDLER, "memory": 256},
                "config": FunctionConfig(handler=ORDERS_HANDLER, timeout=60),
            },
        )

    # Wait for both table AND all EventSourceMappings to be created
    all_mapping_arns = [sub.resources.event_source_mapping.arn for sub in [sub1, sub2, sub3]]
    pulumi.Output.all([table.arn, *all_mapping_arns]).apply(check_subscription_resources)


@pulumi.runtime.test
def test_subscription_filters_and_batch_size(pulumi_mocks, basic_table):
    subscription = basic_table.subscribe(
        "filtered",
        SIMPLE_HANDLER,
        filters=[{"pattern": '{"eventName":["INSERT"]}'}],
        batch_size=50,
    )

    def check_config(_):
        assert_mapping_config(
            pulumi_mocks, batch_size=50, filters=[{"pattern": '{"eventName":["INSERT"]}'}]
        )

    esm = subscription.resources.event_source_mapping
    pulumi.Output.all([basic_table.arn, esm.arn]).apply(check_config)


@pulumi.runtime.test
def test_subscription_batch_size_only(pulumi_mocks, basic_table):
    subscription = basic_table.subscribe("dict", SIMPLE_HANDLER, batch_size=25)

    def check_dict(_):
        assert_mapping_config(pulumi_mocks, batch_size=25)

    esm = subscription.resources.event_source_mapping
    pulumi.Output.all([basic_table.arn, esm.arn]).apply(check_dict)
