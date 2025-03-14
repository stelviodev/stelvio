import pulumi
import pytest
from pulumi.runtime import set_mocks

from stelvio.aws.dynamo_db import AttributeType, DynamoTable
from stelvio.aws.permission import AwsPermission

from ..pulumi_mocks import ACCOUNT_ID, DEFAULT_REGION, PulumiTestMocks, tn

TABLE_ARN_TEMPLATE = f"arn:aws:dynamodb:{DEFAULT_REGION}:{ACCOUNT_ID}:table/{{name}}"


@pytest.fixture
def pulumi_mocks():
    mocks = PulumiTestMocks()
    set_mocks(mocks)
    return mocks


@pulumi.runtime.test
def test_table_properties(pulumi_mocks):
    # Arrange
    table = DynamoTable("test-table", fields={"id": AttributeType.STRING}, partition_key="id")
    # Act
    table._create_resource()

    # Assert
    def check_resources(args):
        table_id, arn = args
        assert table_id == "test-table-test-id"
        assert arn == TABLE_ARN_TEMPLATE.format(name=tn("test-table"))

    pulumi.Output.all(table.resources.table.id, table.arn).apply(check_resources)


@pulumi.runtime.test
def test_dynamo_table_basic(pulumi_mocks):
    # Arrange
    table = DynamoTable("test-table", fields={"id": AttributeType.STRING}, partition_key="id")

    # Act
    table._create_resource()

    # Assert
    def check_resources(_):
        tables = pulumi_mocks.created_dynamo_tables("test-table")
        assert len(tables) == 1
        create_table = tables[0]
        assert create_table.inputs["billingMode"] == "PAY_PER_REQUEST"
        assert create_table.inputs["attributes"] == [{"type": "S", "name": "id"}]
        assert create_table.inputs["hashKey"] == "id"

    table.resources.table.id.apply(check_resources)


@pulumi.runtime.test
def test_dynamo_table_partition_key_and_sort_key(pulumi_mocks):
    # Arrange
    table = DynamoTable(
        "test-table",
        fields={"category": AttributeType.STRING, "order": AttributeType.NUMBER},
        partition_key="category",
        sort_key="order",
    )

    # Act
    table._create_resource()

    # Assert
    def check_resources(_):
        tables = pulumi_mocks.created_dynamo_tables("test-table")
        assert len(tables) == 1
        create_table = tables[0]
        assert create_table.inputs["billingMode"] == "PAY_PER_REQUEST"
        assert create_table.inputs["attributes"] == [
            {"type": "S", "name": "category"},
            {"type": "N", "name": "order"},
        ]
        assert create_table.inputs["hashKey"] == "category"
        assert create_table.inputs["rangeKey"] == "order"

    table.resources.table.id.apply(check_resources)


def test_partition_key_not_in_fields(pulumi_mocks):
    with pytest.raises(ValueError, match="partition_key 'non_existent_key' not in fields list"):
        DynamoTable(
            "test-table", fields={"id": AttributeType.STRING}, partition_key="non_existent_key"
        )


def test_sort_key_not_in_fields(pulumi_mocks):
    with pytest.raises(ValueError, match="sort_key 'non_existent_key' not in fields list"):
        DynamoTable(
            "test-table",
            fields={"id": AttributeType.STRING},
            partition_key="id",
            sort_key="non_existent_key",
        )


def test_partition_sort_key_properties(pulumi_mocks):
    # Arrange
    table = DynamoTable(
        "test-table",
        fields={"id": AttributeType.STRING, "timestamp": AttributeType.NUMBER},
        partition_key="id",
        sort_key="timestamp",
    )

    # Assert
    assert table.partition_key == "id"
    assert table.sort_key == "timestamp"


def test_fields_property_immutability(pulumi_mocks):
    # Arrange
    original_fields = {"id": AttributeType.STRING, "count": AttributeType.NUMBER}
    table = DynamoTable("test-table", fields=original_fields, partition_key="id")

    # Act - Attempt to modify the fields after table creation
    fields_copy = table.fields
    fields_copy["new_field"] = AttributeType.BINARY

    # Assert - Original fields in the table should remain unchanged
    assert "new_field" not in table.fields
    assert len(table.fields) == 2
    assert table.fields == {"id": AttributeType.STRING, "count": AttributeType.NUMBER}

    # Also ensure original dictionary hasn't been modified
    assert original_fields == {"id": AttributeType.STRING, "count": AttributeType.NUMBER}


@pulumi.runtime.test
def test_dynamo_table_link(pulumi_mocks):
    # Arrange
    table = DynamoTable("test-table", fields={"id": AttributeType.STRING}, partition_key="id")

    # Create the resource so we have the table output
    table._create_resource()

    # Act - Get the link from the table
    link = table.link()

    # Assert - Check link properties and permissions
    def check_link(args):
        properties, permissions = args

        expected_properties = {
            "table_name": "test-table-test-name",
            "table_arn": TABLE_ARN_TEMPLATE.format(name=tn("test-table")),
        }
        assert properties == expected_properties

        assert len(permissions) == 1
        assert isinstance(permissions[0], AwsPermission)

        expected_actions = [
            "dynamodb:Scan",
            "dynamodb:Query",
            "dynamodb:GetItem",
            "dynamodb:PutItem",
            "dynamodb:UpdateItem",
            "dynamodb:DeleteItem",
        ]
        assert sorted(permissions[0].actions) == sorted(expected_actions)

        # For resources which are Pulumi Outputs, we need to use .apply()
        def check_resource(resource):
            assert resource == TABLE_ARN_TEMPLATE.format(name=tn("test-table"))

        # Check we have exactly 1 resource with the expected ARN
        assert len(permissions[0].resources) == 1
        permissions[0].resources[0].apply(check_resource)

    # We use Output.all and .apply because Link properties and permissions contain
    # Pulumi Output objects (like table.arn)
    pulumi.Output.all(link.properties, link.permissions).apply(check_link)
