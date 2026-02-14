import pytest

from stelvio.aws.dynamo_db import DynamoTable, GlobalIndex, LocalIndex

from .assert_helpers import (
    assert_dynamo_table,
    assert_event_source_mapping,
    assert_lambda_function,
)


@pytest.mark.integration
def test_dynamo_table(stelvio_env):
    def infra():
        DynamoTable("orders", fields={"pk": "S", "sk": "S"}, partition_key="pk", sort_key="sk")

    outputs = stelvio_env.deploy(infra)

    assert_dynamo_table(
        outputs["dynamotable_orders_arn"],
        hash_key="pk",
        sort_key="sk",
        billing_mode="PAY_PER_REQUEST",
    )


@pytest.mark.integration
def test_dynamo_table_stream(stelvio_env):
    def infra():
        DynamoTable("events", fields={"pk": "S"}, partition_key="pk", stream="new-image")

    outputs = stelvio_env.deploy(infra)

    assert "dynamotable_events_stream_arn" in outputs
    assert_dynamo_table(
        outputs["dynamotable_events_arn"],
        hash_key="pk",
        stream_enabled=True,
        stream_view_type="NEW_IMAGE",
    )


@pytest.mark.integration
def test_dynamo_table_gsi(stelvio_env):
    def infra():
        DynamoTable(
            "products",
            fields={"pk": "S", "sk": "S", "category": "S"},
            partition_key="pk",
            sort_key="sk",
            global_indexes={
                "category-index": GlobalIndex(partition_key="category", projections="all"),
            },
        )

    outputs = stelvio_env.deploy(infra)

    assert_dynamo_table(
        outputs["dynamotable_products_arn"],
        hash_key="pk",
        sort_key="sk",
        gsi_names=["category-index"],
    )


@pytest.mark.integration
def test_dynamo_table_lsi(stelvio_env):
    def infra():
        DynamoTable(
            "tickets",
            fields={"pk": "S", "sk": "S", "created_at": "S"},
            partition_key="pk",
            sort_key="sk",
            local_indexes={
                "created-at-index": LocalIndex(sort_key="created_at"),
            },
        )

    outputs = stelvio_env.deploy(infra)

    assert_dynamo_table(
        outputs["dynamotable_tickets_arn"],
        hash_key="pk",
        sort_key="sk",
        lsi_names=["created-at-index"],
    )


# Future test ideas:
# - GSI with sort key and specific projections (INCLUDE with attribute list)
# - Multiple GSIs on same table
# - Subscribe with filter patterns (e.g. INSERT-only filter)
# - Multiple subscriptions on same table
# - Different stream view types (old-image, keys-only, new-and-old-images)


@pytest.mark.integration
def test_dynamo_table_subscribe(stelvio_env, project_dir):
    def infra():
        table = DynamoTable("tasks", fields={"pk": "S"}, partition_key="pk", stream="new-image")
        table.subscribe("processor", "handlers/echo.main", batch_size=50)

    outputs = stelvio_env.deploy(infra)

    # Table has stream enabled
    assert_dynamo_table(
        outputs["dynamotable_tasks_arn"],
        hash_key="pk",
        stream_enabled=True,
        stream_view_type="NEW_IMAGE",
    )

    # Lambda function was created
    function_arn = outputs["function_tasks-processor_arn"]
    assert_lambda_function(function_arn)

    # EventSourceMapping wires stream → Lambda with correct config
    stream_arn = outputs["dynamotable_tasks_stream_arn"]
    assert_event_source_mapping(
        function_arn,
        event_source_arn=stream_arn,
        batch_size=50,
    )
