import pytest

from stelvio.aws.dynamo_db import DynamoTable, GlobalIndex, LocalIndex

from .assert_helpers import (
    assert_dynamo_table,
    assert_dynamo_tags,
    assert_event_source_mapping,
    assert_lambda_function,
    assert_lambda_tags,
)
from .export_helpers import export_dynamo_table, export_function

pytestmark = pytest.mark.integration


# --- Properties ---


def test_dynamo_table_basic(stelvio_env):
    def infra():
        table = DynamoTable(
            "orders", fields={"pk": "S", "sk": "S"}, partition_key="pk", sort_key="sk"
        )
        export_dynamo_table(table)

    outputs = stelvio_env.deploy(infra)

    assert_dynamo_table(
        outputs["dynamotable_orders_arn"],
        hash_key="pk",
        sort_key="sk",
        billing_mode="PAY_PER_REQUEST",
    )


def test_dynamo_table_tags(stelvio_env):
    def infra():
        table = DynamoTable(
            "tagged-orders",
            fields={"pk": "S"},
            partition_key="pk",
            tags={"Team": "platform"},
        )
        export_dynamo_table(table)

    outputs = stelvio_env.deploy(infra)
    assert_dynamo_tags(outputs["dynamotable_tagged-orders_arn"], {"Team": "platform"})


@pytest.mark.parametrize(
    ("stream_type", "expected_view_type"),
    [
        ("new-image", "NEW_IMAGE"),
        ("old-image", "OLD_IMAGE"),
        ("new-and-old-images", "NEW_AND_OLD_IMAGES"),
        ("keys-only", "KEYS_ONLY"),
    ],
)
def test_dynamo_table_stream(stelvio_env, stream_type, expected_view_type):
    def infra():
        table = DynamoTable("events", fields={"pk": "S"}, partition_key="pk", stream=stream_type)
        export_dynamo_table(table)

    outputs = stelvio_env.deploy(infra)

    assert "dynamotable_events_stream_arn" in outputs
    assert_dynamo_table(
        outputs["dynamotable_events_arn"],
        stream_enabled=True,
        stream_view_type=expected_view_type,
    )


def test_dynamo_table_gsi_projections(stelvio_env):
    def infra():
        table = DynamoTable(
            "orders",
            fields={"pk": "S", "sk": "S", "customer": "S", "status": "S"},
            partition_key="pk",
            sort_key="sk",
            global_indexes={
                "customer-status-index": GlobalIndex(
                    partition_key="customer",
                    sort_key="status",
                    projections=["sk"],
                ),
            },
        )
        export_dynamo_table(table)

    outputs = stelvio_env.deploy(infra)

    assert_dynamo_table(
        outputs["dynamotable_orders_arn"],
        gsi_details={
            "customer-status-index": {
                "hash_key": "customer",
                "sort_key": "status",
                "projection_type": "INCLUDE",
                "non_key_attributes": ["sk"],
            },
        },
    )


def test_dynamo_table_multiple_gsis(stelvio_env):
    def infra():
        table = DynamoTable(
            "products",
            fields={"pk": "S", "category": "S", "brand": "S", "status": "S"},
            partition_key="pk",
            global_indexes={
                "category-index": GlobalIndex(partition_key="category", projections="all"),
                "brand-index": GlobalIndex(partition_key="brand", projections="keys-only"),
                "status-index": GlobalIndex(partition_key="status", projections=["category"]),
            },
        )
        export_dynamo_table(table)

    outputs = stelvio_env.deploy(infra)

    assert_dynamo_table(
        outputs["dynamotable_products_arn"],
        gsi_names=["category-index", "brand-index", "status-index"],
        gsi_details={
            "category-index": {"projection_type": "ALL"},
            "brand-index": {"projection_type": "KEYS_ONLY"},
            "status-index": {
                "projection_type": "INCLUDE",
                "non_key_attributes": ["category"],
            },
        },
    )


def test_dynamo_table_lsi(stelvio_env):
    def infra():
        table = DynamoTable(
            "tickets",
            fields={"pk": "S", "sk": "S", "created_at": "S"},
            partition_key="pk",
            sort_key="sk",
            local_indexes={
                "created-at-index": LocalIndex(sort_key="created_at"),
            },
        )
        export_dynamo_table(table)

    outputs = stelvio_env.deploy(infra)

    assert_dynamo_table(
        outputs["dynamotable_tickets_arn"],
        hash_key="pk",
        sort_key="sk",
        lsi_names=["created-at-index"],
    )


# --- Subscribe ---


def test_dynamo_table_subscribe(stelvio_env, project_dir):
    def infra():
        table = DynamoTable("tasks", fields={"pk": "S"}, partition_key="pk", stream="new-image")
        sub = table.subscribe("processor", "handlers/echo.main", batch_size=50)
        export_dynamo_table(table)
        export_function(sub.resources.function)

    outputs = stelvio_env.deploy(infra)

    assert_dynamo_table(
        outputs["dynamotable_tasks_arn"],
        stream_enabled=True,
        stream_view_type="NEW_IMAGE",
    )

    function_arn = outputs["function_tasks-processor_arn"]
    assert_lambda_function(function_arn)

    assert_event_source_mapping(
        function_arn,
        event_source_arn=outputs["dynamotable_tasks_stream_arn"],
        batch_size=50,
    )


def test_dynamo_subscribe_propagates_tags_to_generated_function(stelvio_env, project_dir):
    def infra():
        table = DynamoTable(
            "tagged-events",
            fields={"pk": "S"},
            partition_key="pk",
            stream="new-image",
            tags={"Team": "platform"},
        )
        sub = table.subscribe("processor", "handlers/echo.main")
        export_function(sub.resources.function)

    outputs = stelvio_env.deploy(infra)
    assert_lambda_tags(outputs["function_tagged-events-processor_arn"], {"Team": "platform"})


def test_dynamo_table_subscribe_with_filter(stelvio_env, project_dir):
    def infra():
        table = DynamoTable("events", fields={"pk": "S"}, partition_key="pk", stream="new-image")
        sub = table.subscribe(
            "insert-only",
            "handlers/echo.main",
            filters=[{"pattern": '{"eventName": ["INSERT"]}'}],
        )
        export_dynamo_table(table)
        export_function(sub.resources.function)

    outputs = stelvio_env.deploy(infra)

    assert_event_source_mapping(
        outputs["function_events-insert-only_arn"],
        event_source_arn=outputs["dynamotable_events_stream_arn"],
        has_filter_criteria=True,
    )


def test_dynamo_table_multiple_subscriptions(stelvio_env, project_dir):
    def infra():
        table = DynamoTable("orders", fields={"pk": "S"}, partition_key="pk", stream="new-image")
        sub1 = table.subscribe("processor", "handlers/echo.main", batch_size=10)
        sub2 = table.subscribe("auditor", "handlers/echo.main", batch_size=1)
        export_dynamo_table(table)
        export_function(sub1.resources.function)
        export_function(sub2.resources.function)

    outputs = stelvio_env.deploy(infra)

    stream_arn = outputs["dynamotable_orders_stream_arn"]

    processor_arn = outputs["function_orders-processor_arn"]
    assert_lambda_function(processor_arn)
    assert_event_source_mapping(processor_arn, event_source_arn=stream_arn, batch_size=10)

    auditor_arn = outputs["function_orders-auditor_arn"]
    assert_lambda_function(auditor_arn)
    assert_event_source_mapping(auditor_arn, event_source_arn=stream_arn, batch_size=1)
