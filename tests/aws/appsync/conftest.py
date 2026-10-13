"""AppSync test fixtures shared across AppSync test modules."""

from typing import Any

import pulumi

from stelvio.aws.appsync import AppSync, CognitoAuth
from stelvio.aws.dynamo_db import DynamoTable
from stelvio.component import ComponentRegistry

from ...conftest import TP
from ..pulumi_mocks import tid

INLINE_SCHEMA = """\
type Query {
    getPost(id: ID!): Post
}

type Mutation {
    createPost(title: String!, content: String!): Post
}

type Post {
    id: ID!
    title: String!
    content: String!
}
"""

COGNITO_USER_POOL_ID = "us-east-1_TestPool123"
API_ID = tid(f"{TP}myapi")


def make_api(
    name: str = "myapi",
    *,
    schema: str = INLINE_SCHEMA,
    auth: Any | None = None,
    **kwargs: Any,
) -> AppSync:
    """Create an AppSync API with sensible defaults for testing."""
    resolved_auth = CognitoAuth(user_pool_id=COGNITO_USER_POOL_ID) if auth is None else auth
    if kwargs.get("additional_auth") is None:
        kwargs.pop("additional_auth", None)
    return AppSync(name, schema=schema, auth=resolved_auth, **kwargs)


def make_lambda_ds(api: AppSync, name: str = "posts") -> Any:
    """Create a lambda data source with a default handler."""
    return api.data_source_lambda(name, handler="functions/simple.handler")


def make_dynamo_ds(api: AppSync, name: str = "items") -> tuple:
    """Create a DynamoDB data source with a simple table. Returns (data_source, table)."""
    table = DynamoTable(name, fields={"pk": "string"}, partition_key="pk")
    return api.data_source_dynamo(name, table=table), table


def make_data_source(api: AppSync, ds_type: str, **kwargs: Any) -> Any:
    """Create a data source of the given type for cross-type parametrized tests."""
    if ds_type == "lambda":
        return api.data_source_lambda("posts", handler="functions/simple.handler", **kwargs)
    if ds_type == "dynamo":
        table = DynamoTable("items", fields={"pk": "S"}, partition_key="pk")
        return api.data_source_dynamo("items", table=table, **kwargs)
    if ds_type == "http":
        return api.data_source_http("ext", url="https://api.example.com", **kwargs)
    if ds_type == "rds":
        return api.data_source_rds(
            "db",
            cluster_arn="arn:aws:rds:us-east-1:123456789012:cluster:my-cluster",
            secret_arn="arn:aws:secretsmanager:us-east-1:123456789012:secret:my-secret",  # noqa: S106
            database="mydb",
            **kwargs,
        )
    if ds_type == "opensearch":
        return api.data_source_opensearch(
            "search",
            endpoint="https://search-my-domain-abc123def456ghij.us-east-1.es.amazonaws.com",
            **kwargs,
        )
    raise ValueError(f"Unknown ds_type: {ds_type}")


def deploy_all() -> None:
    """Create every registered component and wait for all of it, as `stlv deploy` does.

    Data sources, resolvers and pipe functions are components of their own: the deploy loop
    (`StelvioApp.drive`) creates them, `AppSync.resources` doesn't.
    """

    @pulumi.runtime.test
    def deploy() -> None:
        for component in ComponentRegistry.all_instances():
            _ = component.resources

    deploy()


def add_resolver_for_ds(api: Any, ds: Any, ds_type: str) -> None:
    """Add a resolver appropriate for the data source type."""
    if ds_type == "lambda":
        api.query("getPost", ds)
    else:
        api.query("getPost", ds, code="resolvers/getItem.js")


def make_pipeline_steps(api: AppSync, *, with_ds: bool = True) -> list:
    """Create standard auth + optional delete pipeline steps."""
    auth = api.pipe_function("checkAuth", None, code="resolvers/auth.js")
    if not with_ds:
        return [auth]
    items, _ = make_dynamo_ds(api)
    delete = api.pipe_function("doDelete", items, code="resolvers/delete.js")
    return [auth, delete]
