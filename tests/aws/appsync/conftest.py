"""AppSync test fixtures shared across AppSync test modules."""

from typing import Any

import pulumi

from stelvio.aws.appsync import AppSync, CognitoAuth
from stelvio.aws.dynamo_db import DynamoTable

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


def make_api(name: str = "myapi") -> AppSync:
    return AppSync(name, schema=INLINE_SCHEMA, auth=CognitoAuth(user_pool_id=COGNITO_USER_POOL_ID))


def make_lambda_ds(api: AppSync, name: str = "posts") -> Any:
    """Create a lambda data source with a default handler."""
    return api.data_source_lambda(name, handler="functions/simple.handler")


def make_dynamo_ds(api: AppSync, name: str = "items") -> tuple:
    """Create a DynamoDB data source with a simple table. Returns (data_source, table)."""
    table = DynamoTable(name, fields={"pk": "S"}, partition_key="pk")
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
            endpoint="https://search-domain-abc123def456ghij.us-east-1.es.amazonaws.com",
            **kwargs,
        )
    raise ValueError(f"Unknown ds_type: {ds_type}")


def when_appsync_ready(api: Any, callback: Any) -> None:
    outputs: list[pulumi.Output[Any]] = [api.resources.api.id, api.resources.api.arn]

    if api.resources.api_key is not None:
        outputs.append(api.resources.api_key.id)

    if api.resources.auth_permissions:
        outputs.extend(permission.id for permission in api.resources.auth_permissions)

    if api.resources.acm_validated_domain is not None:
        outputs.append(api.resources.acm_validated_domain.resources.certificate.arn)
    if api.resources.domain_association is not None:
        outputs.append(api.resources.domain_association.id)
    if api.resources.domain_dns_record is not None:
        outputs.append(api.resources.domain_dns_record.name)

    for data_source in api._data_sources.values():
        resources = data_source.resources
        outputs.append(resources.data_source.arn)
        outputs.append(resources.service_role.arn)
        if resources.function is not None:
            outputs.append(resources.function.resources.function.arn)

    outputs.extend(
        pipe_function.resources.function.arn for pipe_function in api._pipe_functions.values()
    )
    outputs.extend(resolver.resources.resolver.arn for resolver in api._resolvers)

    pulumi.Output.all(*outputs).apply(callback)
