"""Core AppSync component tests — constructor, schema, properties, naming."""

from pathlib import Path

import pulumi
import pytest

from stelvio.aws.appsync import AppSync, CognitoAuth
from stelvio.aws.appsync.constants import AUTH_TYPE_COGNITO
from stelvio.config import AwsConfig
from stelvio.context import AppContext, _ContextStore

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
        schema_path = Path(project_cwd) / "schema.graphql"
        assert schema == schema_path.read_text()

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
    api = AppSync("myapi", INLINE_SCHEMA, auth=CognitoAuth(user_pool_id=COGNITO_USER_POOL_ID))
    _ = api.resources

    with pytest.raises(RuntimeError, match="Cannot modify AppSync"):
        api.data_source_lambda("posts", handler="functions/simple.handler")


# --- Global customization propagation ---


@pulumi.runtime.test
def test_global_customization_propagates_to_api(pulumi_mocks, project_cwd, clean_registries):
    """Global customization for AppSync propagates to the GraphQL API."""
    _ContextStore.clear()
    _ContextStore.set(
        AppContext(
            name="test",
            env="test",
            aws=AwsConfig(profile="default", region="us-east-1"),
            home="aws",
            customize={AppSync: {"api": {"xray_enabled": True}}},
        )
    )

    api = AppSync("myapi", INLINE_SCHEMA, auth=CognitoAuth(user_pool_id=COGNITO_USER_POOL_ID))
    _ = api.resources

    def check_resources(_):
        apis = pulumi_mocks.created_appsync_apis(f"{TP}myapi")
        assert len(apis) == 1
        assert apis[0].inputs.get("xrayEnabled") is True

    api.resources.completed.apply(check_resources)


@pulumi.runtime.test
def test_global_customization_propagates_to_data_source(
    pulumi_mocks, project_cwd, clean_registries
):
    """Global customization for data_source key propagates to all data sources."""
    _ContextStore.clear()
    _ContextStore.set(
        AppContext(
            name="test",
            env="test",
            aws=AwsConfig(profile="default", region="us-east-1"),
            home="aws",
            customize={AppSync: {"service_role": {"path": "/global-appsync/"}}},
        )
    )

    api = AppSync("myapi", INLINE_SCHEMA, auth=CognitoAuth(user_pool_id=COGNITO_USER_POOL_ID))
    posts = api.data_source_lambda("posts", handler="functions/simple.handler")
    api.query("getPost", posts)
    _ = api.resources

    def check_resources(_):
        roles = pulumi_mocks.created_roles()
        ds_roles = [r for r in roles if "ds-posts-role" in r.name]
        assert len(ds_roles) == 1
        assert ds_roles[0].inputs["path"] == "/global-appsync/"

    api.resources.completed.apply(check_resources)


@pulumi.runtime.test
def test_per_ds_customize_overrides_global(pulumi_mocks, project_cwd, clean_registries):
    """Per-data-source customize overrides global customization."""
    _ContextStore.clear()
    _ContextStore.set(
        AppContext(
            name="test",
            env="test",
            aws=AwsConfig(profile="default", region="us-east-1"),
            home="aws",
            customize={AppSync: {"service_role": {"path": "/global-appsync/"}}},
        )
    )

    api = AppSync("myapi", INLINE_SCHEMA, auth=CognitoAuth(user_pool_id=COGNITO_USER_POOL_ID))
    posts = api.data_source_lambda(
        "posts",
        handler="functions/simple.handler",
        customize={"service_role": {"path": "/custom-ds/"}},
    )
    api.query("getPost", posts)
    _ = api.resources

    def check_resources(_):
        roles = pulumi_mocks.created_roles()
        ds_roles = [r for r in roles if "ds-posts-role" in r.name]
        assert len(ds_roles) == 1
        # Per-DS should override global
        assert ds_roles[0].inputs["path"] == "/custom-ds/"

    api.resources.completed.apply(check_resources)
