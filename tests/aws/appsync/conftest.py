"""AppSync test fixtures shared across AppSync test modules."""

import pytest

from stelvio.aws.appsync.constants import AUTH_TYPE_COGNITO

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
COGNITO_AUTH_TYPE = AUTH_TYPE_COGNITO


@pytest.fixture
def inline_schema():
    return INLINE_SCHEMA
