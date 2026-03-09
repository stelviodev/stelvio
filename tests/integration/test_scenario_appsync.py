"""Scenario tests: AppSync end-to-end.

Verifies that AppSync APIs actually handle GraphQL queries —
Lambda data source invocation, DynamoDB CRUD via codegen resolvers,
and linking AppSync to a Function injects correct STLV_ env vars.
"""

import time
from collections.abc import Callable

import pytest

from stelvio.aws.appsync import ApiKeyAuth, AppSync, dynamo_get, dynamo_put, dynamo_scan
from stelvio.aws.dynamo_db import DynamoTable
from stelvio.aws.function import Function

from .assert_helpers import assert_lambda_function, assert_lambda_role_permissions, graphql_query

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


def _query_until_no_errors(
    url: str,
    query: str,
    *,
    api_key: str,
    ready_check: Callable[[dict], bool],
    timeout_seconds: int = 30,
) -> dict:
    deadline = time.monotonic() + timeout_seconds
    last_result: dict | None = None

    while time.monotonic() < deadline:
        result = graphql_query(url, query, api_key=api_key)
        last_result = result
        if ready_check(result):
            return result
        time.sleep(2)

    if last_result is None:
        raise AssertionError("No GraphQL response received before timeout")
    raise AssertionError(f"GraphQL errors: {last_result.get('errors')}")


def test_scenario_appsync_lambda_query(stelvio_env, project_dir):
    """Query AppSync → Lambda data source returns expected data."""

    def infra():
        api = AppSync("lam-q", schema=LAMBDA_SCHEMA, auth=ApiKeyAuth())
        ds = api.data_source_lambda("echo", handler="handlers/appsync_echo.main")
        api.query("echo", ds)

    outputs = stelvio_env.deploy(infra)

    result = _query_until_no_errors(
        outputs["appsync_lam-q_url"],
        'query { echo(msg: "hello") { msg } }',
        api_key=outputs["appsync_lam-q_api_key"],
        ready_check=lambda response: (
            "errors" not in response and response.get("data", {}).get("echo") is not None
        ),
    )

    assert result["data"]["echo"]["msg"] == "hello"


def test_scenario_appsync_dynamo_crud(stelvio_env, project_dir):
    """Mutation and Query via DynamoDB codegen resolvers — put, get, scan."""

    def infra():
        table = DynamoTable("items", fields={"pk": "S"}, partition_key="pk")
        api = AppSync("dyn-crud", schema=DYNAMO_SCHEMA, auth=ApiKeyAuth())
        ds = api.data_source_dynamo("items", table=table)
        api.mutation("putItem", ds, code=dynamo_put(key_fields=["pk"]))
        api.query("getItem", ds, code=dynamo_get(pk="pk"))
        api.query("listItems", ds, code=dynamo_scan())

    outputs = stelvio_env.deploy(infra)
    url = outputs["appsync_dyn-crud_url"]
    key = outputs["appsync_dyn-crud_api_key"]

    # Create item
    put_result = _query_until_no_errors(
        url,
        'mutation { putItem(pk: "item-1", name: "Widget") { pk name } }',
        api_key=key,
        ready_check=lambda response: (
            "errors" not in response and response.get("data", {}).get("putItem") is not None
        ),
    )
    assert put_result["data"]["putItem"]["pk"] == "item-1"
    assert put_result["data"]["putItem"]["name"] == "Widget"

    # Read item back
    get_result = _query_until_no_errors(
        url,
        'query { getItem(pk: "item-1") { pk name } }',
        api_key=key,
        ready_check=lambda response: (
            "errors" not in response and response.get("data", {}).get("getItem") is not None
        ),
    )
    assert get_result["data"]["getItem"]["pk"] == "item-1"
    assert get_result["data"]["getItem"]["name"] == "Widget"

    # Scan all items
    scan_result = _query_until_no_errors(
        url,
        "query { listItems { items { pk name } } }",
        api_key=key,
        ready_check=lambda response: (
            "errors" not in response and response.get("data", {}).get("listItems") is not None
        ),
    )
    items = scan_result["data"]["listItems"]["items"]
    assert len(items) >= 1
    assert any(i["pk"] == "item-1" for i in items)


def test_scenario_appsync_link_env_vars(stelvio_env, project_dir):
    """Linking a Function to AppSync injects STLV_ env vars and IAM permissions."""

    def infra():
        api = AppSync("linked", schema=LAMBDA_SCHEMA, auth=ApiKeyAuth())
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
    assert_lambda_role_permissions(
        outputs["function_consumer_role_name"],
        expected_actions=["appsync:GraphQL"],
    )
