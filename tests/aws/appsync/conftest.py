"""AppSync test fixtures shared across AppSync test modules."""

from typing import Any

import pulumi

from stelvio.aws.appsync import AppSync, CognitoAuth

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


def when_appsync_ready(api: Any, callback: Any) -> None:
    outputs: list[pulumi.Output[Any]] = [api.resources.api.id, api.resources.api.arn]

    if api.resources.api_key is not None:
        outputs.append(api.resources.api_key.id)

    outputs.extend(getattr(api, "_auth_outputs", []))
    outputs.extend(getattr(api, "_domain_outputs", []))

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
