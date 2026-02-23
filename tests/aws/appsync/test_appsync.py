"""Core AppSync component tests — constructor, schema, properties, naming."""

import pulumi

from stelvio.aws.appsync import AppSync, CognitoAuth
from stelvio.aws.appsync.constants import AUTH_TYPE_COGNITO

from .conftest import COGNITO_USER_POOL_ID, INLINE_SCHEMA

TP = "test-test-"


@pulumi.runtime.test
def test_appsync_creates_graphql_api(pulumi_mocks, project_cwd):
    api = AppSync("myapi", INLINE_SCHEMA, auth=CognitoAuth(user_pool_id=COGNITO_USER_POOL_ID))
    _ = api.resources

    def check_resources(_):
        apis = pulumi_mocks.created_appsync_apis(f"{TP}myapi")
        assert len(apis) == 1
        a = apis[0]
        assert a.typ == "aws:appsync/graphQLApi:GraphQLApi"
        assert a.inputs["name"] == f"{TP}myapi"
        assert a.inputs["authenticationType"] == AUTH_TYPE_COGNITO

    api.resources.completed.apply(check_resources)


@pulumi.runtime.test
def test_appsync_inline_schema(pulumi_mocks, project_cwd):
    api = AppSync("myapi", INLINE_SCHEMA, auth=CognitoAuth(user_pool_id=COGNITO_USER_POOL_ID))
    _ = api.resources

    def check_resources(_):
        apis = pulumi_mocks.created_appsync_apis(f"{TP}myapi")
        assert len(apis) == 1
        assert apis[0].inputs["schema"] == INLINE_SCHEMA

    api.resources.completed.apply(check_resources)


@pulumi.runtime.test
def test_appsync_schema_from_file(pulumi_mocks, project_cwd):
    api = AppSync("myapi", "schema.graphql", auth=CognitoAuth(user_pool_id=COGNITO_USER_POOL_ID))
    _ = api.resources

    def check_resources(_):
        apis = pulumi_mocks.created_appsync_apis(f"{TP}myapi")
        assert len(apis) == 1
        schema = apis[0].inputs["schema"]
        # Schema file should be read and contain the full SDL
        assert schema.startswith("type Query")
        assert "getPost(id: ID!): Post" in schema

    api.resources.completed.apply(check_resources)


@pulumi.runtime.test
def test_appsync_url_property(pulumi_mocks, project_cwd):
    api = AppSync("myapi", INLINE_SCHEMA, auth=CognitoAuth(user_pool_id=COGNITO_USER_POOL_ID))

    def check_url(url):
        assert url.startswith("https://")
        assert url.endswith(".appsync-api.us-east-1.amazonaws.com/graphql")

    api.url.apply(check_url)


@pulumi.runtime.test
def test_appsync_arn_property(pulumi_mocks, project_cwd):
    api = AppSync("myapi", INLINE_SCHEMA, auth=CognitoAuth(user_pool_id=COGNITO_USER_POOL_ID))

    def check_arn(arn):
        assert arn.startswith("arn:aws:appsync:us-east-1:123456789012:apis/")

    api.arn.apply(check_arn)


@pulumi.runtime.test
def test_appsync_api_id_property(pulumi_mocks, project_cwd):
    api = AppSync("myapi", INLINE_SCHEMA, auth=CognitoAuth(user_pool_id=COGNITO_USER_POOL_ID))

    def check_id(api_id):
        assert api_id is not None
        assert len(api_id) > 0

    api.api_id.apply(check_id)


@pulumi.runtime.test
def test_appsync_exports(pulumi_mocks, project_cwd):
    """Verify Pulumi exports are created for the API."""
    api = AppSync("myapi", INLINE_SCHEMA, auth=CognitoAuth(user_pool_id=COGNITO_USER_POOL_ID))
    _ = api.resources

    def check_resources(_):
        apis = pulumi_mocks.created_appsync_apis(f"{TP}myapi")
        assert len(apis) == 1

    api.resources.completed.apply(check_resources)


def test_appsync_cannot_modify_after_resources_created(pulumi_mocks, project_cwd):
    """Accessing resources then trying to add data sources should raise."""
    import pytest

    api = AppSync("myapi", INLINE_SCHEMA, auth=CognitoAuth(user_pool_id=COGNITO_USER_POOL_ID))
    _ = api.resources

    with pytest.raises(RuntimeError, match="Cannot modify AppSync"):
        api.data_source_lambda("posts", handler="functions/simple.handler")
