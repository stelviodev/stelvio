import pytest

from stelvio.aws.dynamo_db import DynamoTable

from .assert_helpers import assert_dynamo_table


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
