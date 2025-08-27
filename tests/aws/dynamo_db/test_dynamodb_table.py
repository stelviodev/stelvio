from dataclasses import dataclass, field, replace

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
)
from stelvio.aws.permission import AwsPermission
from tests.test_utils import assert_config_dict_matches_dataclass

from ..pulumi_mocks import ACCOUNT_ID, DEFAULT_REGION, PulumiTestMocks, tn

TABLE_ARN_TEMPLATE = f"arn:aws:dynamodb:{DEFAULT_REGION}:{ACCOUNT_ID}:table/{{name}}"

# Test prefix
TP = "test-test-"


@pytest.fixture
def pulumi_mocks():
    mocks = PulumiTestMocks()
    set_mocks(mocks)
    return mocks


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


def assert_table_configuration(table_args, test_case: DynamoTableTestCase):
    """Assert table configuration matches expectations."""
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


def verify_table_resources(pulumi_mocks, table: DynamoTable, test_case: DynamoTableTestCase):
    """Verify all table resources are created correctly."""
    tables = pulumi_mocks.created_dynamo_tables(TP + test_case.name)
    assert len(tables) == 1
    table_args = tables[0]

    assert_table_configuration(table_args, test_case)


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
    from stelvio.aws.dynamo_db import _convert_projection

    assert _convert_projection(projections) == expected


def test_field_type_literals_normalized():
    """Test that field type literals are normalized correctly."""
    config = DynamoTableConfig(
        fields={"id": "string", "score": "number", "data": "binary"}, partition_key="id"
    )
    assert config.normalized_fields == {"id": "S", "score": "N", "data": "B"}

    # Test with mixed types
    config2 = DynamoTableConfig(
        fields={"id": FieldType.STRING, "score": "number", "data": "B"}, partition_key="id"
    )
    assert config2.normalized_fields == {"id": "S", "score": "N", "data": "B"}


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
    ],
    ids=lambda tc: tc.test_id,
)
@pulumi.runtime.test
def test_dynamo_table_creation(pulumi_mocks, test_case):
    """Test creating DynamoDB tables with various configurations."""
    if isinstance(test_case.config_input, dict):
        table = DynamoTable(test_case.name, **test_case.config_input)
    else:
        table = DynamoTable(test_case.name, config=test_case.config_input)

    # Trigger resource creation and verify
    def check_resources(_):
        verify_table_resources(pulumi_mocks, table, test_case)

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
    table = DynamoTable("my-table", fields={"id": FieldType.STRING}, partition_key="id")

    # Create the resource so we have the table output
    _ = table.resources

    # Act - Get the link from the table
    link = table.link()

    # Assert - Check link properties and permissions
    def check_link(args):
        properties, permissions = args

        expected_properties = {
            "table_name": TP + "my-table-test-name",
            "table_arn": TABLE_ARN_TEMPLATE.format(name=tn(TP + "my-table")),
        }
        assert properties == expected_properties

        assert len(permissions) == 2
        
        # Check table permissions (first permission)
        table_permission = permissions[0]
        assert isinstance(table_permission, AwsPermission)
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
        
        def check_table_resource(resource):
            assert resource == TABLE_ARN_TEMPLATE.format(name=tn(TP + "my-table"))
        table_permission.resources[0].apply(check_table_resource)
        
        # Check index permissions (second permission)
        index_permission = permissions[1]
        assert isinstance(index_permission, AwsPermission)
        expected_index_actions = ["dynamodb:Query", "dynamodb:Scan"]
        assert sorted(index_permission.actions) == sorted(expected_index_actions)
        assert len(index_permission.resources) == 1
        
        def check_index_resource(resource):
            expected_index_arn = TABLE_ARN_TEMPLATE.format(name=tn(TP + "my-table")) + "/index/*"
            assert resource == expected_index_arn
        index_permission.resources[0].apply(check_index_resource)

    # We use Output.all and .apply because Link properties and permissions contain
    # Pulumi Output objects (like table.arn)
    pulumi.Output.all(link.properties, link.permissions).apply(check_link)


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
