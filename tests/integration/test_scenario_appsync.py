"""Scenario tests: AppSync end-to-end.

Verifies that AppSync APIs actually handle GraphQL queries —
Lambda data source invocation, DynamoDB CRUD via codegen resolvers,
and linking AppSync to a Function injects correct STLV_ env vars.
"""

import pytest

from stelvio.aws.appsync import ApiKeyAuth, AppSync, dynamo_get, dynamo_put, dynamo_scan
from stelvio.aws.dynamo_db import DynamoTable
from stelvio.aws.function import Function

from .assert_helpers import assert_lambda_function, graphql_query

pytestmark = pytest.mark.integration

LAMBDA_SCHEMA = """\
type Query {
    echo(msg: String): EchoResult
}

type EchoResult {
    msg: String
}
"""

DYNAMO_SCHEMA = """\
type Query {
    getItem(pk: String!): Item
    listItems: ItemConnection
}

type Mutation {
    putItem(pk: String!, name: String!): Item
}

type Item {
    pk: String!
    name: String
}

type ItemConnection {
    items: [Item]
    nextToken: String
    scannedCount: Int
}
"""


def test_scenario_appsync_lambda_query(stelvio_env, project_dir):
    """Query AppSync → Lambda data source returns expected data."""

    def infra():
        api = AppSync("lam-q", LAMBDA_SCHEMA, auth=ApiKeyAuth())
        ds = api.data_source_lambda("echo", handler="handlers/appsync_echo.main")
        api.query("echo", ds)

    outputs = stelvio_env.deploy(infra)

    result = graphql_query(
        outputs["appsync_lam-q_url"],
        'query { echo(msg: "hello") { msg } }',
        api_key=outputs["appsync_lam-q_api_key"],
    )

    assert "errors" not in result, f"GraphQL errors: {result.get('errors')}"
    assert result["data"]["echo"]["msg"] == "hello"


def test_scenario_appsync_dynamo_crud(stelvio_env, project_dir):
    """Mutation and Query via DynamoDB codegen resolvers — put, get, scan."""

    def infra():
        table = DynamoTable("items", fields={"pk": "S"}, partition_key="pk")
        api = AppSync("dyn-crud", DYNAMO_SCHEMA, auth=ApiKeyAuth())
        ds = api.data_source_dynamo("items", table=table)
        api.mutation("putItem", ds, code=dynamo_put(key_fields=["pk"]))
        api.query("getItem", ds, code=dynamo_get(pk="pk"))
        api.query("listItems", ds, code=dynamo_scan())

    outputs = stelvio_env.deploy(infra)
    url = outputs["appsync_dyn-crud_url"]
    key = outputs["appsync_dyn-crud_api_key"]

    # Create item
    put_result = graphql_query(
        url,
        'mutation { putItem(pk: "item-1", name: "Widget") { pk name } }',
        api_key=key,
    )
    assert "errors" not in put_result, f"GraphQL errors: {put_result.get('errors')}"
    assert put_result["data"]["putItem"]["pk"] == "item-1"
    assert put_result["data"]["putItem"]["name"] == "Widget"

    # Read item back
    get_result = graphql_query(
        url,
        'query { getItem(pk: "item-1") { pk name } }',
        api_key=key,
    )
    assert "errors" not in get_result, f"GraphQL errors: {get_result.get('errors')}"
    assert get_result["data"]["getItem"]["pk"] == "item-1"
    assert get_result["data"]["getItem"]["name"] == "Widget"

    # Scan all items
    scan_result = graphql_query(
        url,
        "query { listItems { items { pk name } } }",
        api_key=key,
    )
    assert "errors" not in scan_result, f"GraphQL errors: {scan_result.get('errors')}"
    items = scan_result["data"]["listItems"]["items"]
    assert len(items) >= 1
    assert any(i["pk"] == "item-1" for i in items)


def test_scenario_appsync_link_env_vars(stelvio_env, project_dir):
    """Linking a Function to AppSync injects STLV_ env vars and IAM permissions."""

    def infra():
        api = AppSync("linked", LAMBDA_SCHEMA, auth=ApiKeyAuth())
        ds = api.data_source_lambda("echo", handler="handlers/appsync_echo.main")
        api.query("echo", ds)
        Function("consumer", handler="handlers/echo.main", links=[api])

    outputs = stelvio_env.deploy(infra)

    assert_lambda_function(
        outputs["function_consumer_arn"],
        environment={
            "STLV_LINKED_URL": outputs["appsync_linked_url"],
            "STLV_LINKED_API_KEY": outputs["appsync_linked_api_key"],
        },
    )
