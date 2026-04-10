"""AppSync test fixtures shared across AppSync test modules."""

import json
from typing import Any

import pulumi

from stelvio.aws.appsync import AppSync, CognitoAuth
from stelvio.aws.dynamo_db import DynamoTable
from stelvio.config import AwsConfig
from stelvio.context import AppContext, _ContextStore

TP = "test-test-"

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
            endpoint="https://search-my-domain-abc123def456ghij.us-east-1.es.amazonaws.com",
            **kwargs,
        )
    raise ValueError(f"Unknown ds_type: {ds_type}")


def when_appsync_ready(api: Any, callback: Any) -> None:
    r = api.resources
    outputs: list[pulumi.Output[Any]] = [r.api.id, r.api.arn]

    if r.api_key is not None:
        outputs.append(r.api_key.id)
    if r.auth_permissions:
        outputs.extend(p.id for p in r.auth_permissions)
    if r.acm_validated_domain is not None:
        outputs.append(r.acm_validated_domain.resources.certificate.arn)
    if r.domain_association is not None:
        outputs.append(r.domain_association.id)
    if r.domain_dns_record is not None:
        outputs.append(r.domain_dns_record.name)

    for ds in api._data_sources.values():
        dr = ds.resources
        outputs.extend([dr.data_source.arn, dr.service_role.arn])
        if dr.function is not None:
            outputs.append(dr.function.resources.function.arn)
        outputs.extend(p.name for p in ds._policies)

    outputs.extend(pf.resources.function.arn for pf in api._pipe_functions.values())
    outputs.extend(resolver.resources.resolver.arn for resolver in api._resolvers)
    pulumi.Output.all(*outputs).apply(callback)


def assert_iam_policy(  # noqa: PLR0913
    pulumi_mocks: Any,
    name_pattern: str,
    expected_actions: list[str],
    expected_resources: Any,
    *,
    effect: str = "Allow",
    statement_count: int = 1,
) -> list[dict]:
    """Extract and verify an IAM role policy by name pattern.

    Returns the parsed policy statements for additional assertions.
    """
    policies = pulumi_mocks.created_role_policies()
    matched = [p for p in policies if name_pattern in p.name]
    assert len(matched) == 1
    assert matched[0].typ == "aws:iam/rolePolicy:RolePolicy"
    policy_doc = json.loads(matched[0].inputs["policy"])
    assert len(policy_doc["Statement"]) == statement_count

    if statement_count == 1:
        stmt = policy_doc["Statement"][0]
        assert stmt["Effect"] == effect
        if isinstance(expected_actions, set):
            assert set(stmt["Action"]) == expected_actions
        else:
            assert stmt["Action"] == expected_actions
        assert stmt["Resource"] == expected_resources

    return policy_doc["Statement"]


def assert_graphql_api_inputs(pulumi_mocks: Any, name: str, **expected_inputs: Any) -> dict:
    """Assert a single GraphQL API exists and check its inputs.

    Returns the full inputs dict for additional assertions.
    """
    apis = pulumi_mocks.created_appsync_apis(name)
    assert len(apis) == 1
    for key, value in expected_inputs.items():
        assert apis[0].inputs[key] == value, f"Expected {key}={value}, got {apis[0].inputs[key]}"
    return apis[0].inputs


def assert_data_source_inputs(
    pulumi_mocks: Any,
    *,
    ds_type: str | None = None,
    name: str | None = None,
    **expected_inputs: Any,
) -> dict:
    """Assert exactly one data source matches the given filters and inputs."""
    data_sources = pulumi_mocks.created_appsync_data_sources()
    matched = data_sources

    if ds_type is not None:
        matched = [data_source for data_source in matched if data_source.inputs["type"] == ds_type]
    if name is not None:
        matched = [data_source for data_source in matched if data_source.inputs["name"] == name]

    assert len(matched) == 1
    for key, value in expected_inputs.items():
        assert matched[0].inputs[key] == value
    return matched[0].inputs


def assert_appsync_function_inputs(
    pulumi_mocks: Any, name: str | None = None, **expected_inputs: Any
) -> dict:
    """Assert exactly one AppSync function matches the given filters and inputs."""
    functions = pulumi_mocks.created_appsync_functions()
    matched = (
        functions
        if name is None
        else [function for function in functions if function.inputs["name"] == name]
    )

    assert len(matched) == 1
    for key, value in expected_inputs.items():
        assert matched[0].inputs[key] == value
    return matched[0].inputs


def assert_resolver_inputs(pulumi_mocks: Any, **expected: Any) -> dict:
    """Assert exactly one resolver exists and check its inputs.

    Returns the full inputs dict for additional assertions.
    """
    resolvers = pulumi_mocks.created_appsync_resolvers()
    assert len(resolvers) == 1
    for key, value in expected.items():
        assert resolvers[0].inputs[key] == value
    return resolvers[0].inputs


def add_resolver_for_ds(api: Any, ds: Any, ds_type: str) -> None:
    """Add a resolver appropriate for the data source type."""
    if ds_type == "lambda":
        api.query("getPost", ds)
    else:
        api.query("getPost", ds, code="resolvers/getItem.js")


def set_context_with_customize(customize: dict) -> None:
    """Set test context with customization config."""
    _ContextStore.clear()
    _ContextStore.set(
        AppContext(
            name="test",
            env="test",
            aws=AwsConfig(profile="default", region="us-east-1"),
            home="aws",
            customize=customize,
        )
    )


def assert_role(pulumi_mocks: Any, name_pattern: str, **expected_inputs: Any) -> Any:
    """Assert exactly one IAM role matches pattern and check inputs."""
    roles = pulumi_mocks.created_roles()
    matched = [r for r in roles if name_pattern in r.name]
    assert len(matched) == 1
    for key, value in expected_inputs.items():
        assert matched[0].inputs[key] == value
    return matched[0]


def make_pipeline_steps(api: AppSync, *, with_ds: bool = True) -> list:
    """Create standard auth + optional delete pipeline steps."""
    auth = api.pipe_function("checkAuth", None, code="resolvers/auth.js")
    if not with_ds:
        return [auth]
    items, _ = make_dynamo_ds(api)
    delete = api.pipe_function("doDelete", items, code="resolvers/delete.js")
    return [auth, delete]
