"""Core AppSync component tests — constructor, schema, properties, naming."""

from pathlib import Path

import pulumi
import pytest

from stelvio.aws.appsync import (
    AppSync,
    AppSyncConfig,
    AppSyncConfigDict,
    AppSyncDataSource,
    CognitoAuth,
)
from stelvio.aws.appsync.constants import AUTH_TYPE_COGNITO
from stelvio.config import AwsConfig
from stelvio.context import AppContext, _ContextStore

from ...test_utils import assert_config_dict_matches_dataclass
from .conftest import COGNITO_USER_POOL_ID, INLINE_SCHEMA, make_lambda_ds, when_appsync_ready

TP = "test-test-"


@pulumi.runtime.test
def test_appsync_creates_graphql_api(pulumi_mocks, project_cwd):
    api = AppSync(
        "myapi", schema=INLINE_SCHEMA, auth=CognitoAuth(user_pool_id=COGNITO_USER_POOL_ID)
    )

    def check_resources(_):
        apis = pulumi_mocks.created_appsync_apis(f"{TP}myapi")
        assert len(apis) == 1
        a = apis[0]
        assert a.typ == "aws:appsync/graphQLApi:GraphQLApi"
        assert a.inputs["name"] == f"{TP}myapi"
        assert a.inputs["authenticationType"] == AUTH_TYPE_COGNITO

    when_appsync_ready(api, check_resources)


@pulumi.runtime.test
def test_appsync_inline_schema(pulumi_mocks, project_cwd):
    api = AppSync(
        "myapi", schema=INLINE_SCHEMA, auth=CognitoAuth(user_pool_id=COGNITO_USER_POOL_ID)
    )

    def check_resources(_):
        apis = pulumi_mocks.created_appsync_apis(f"{TP}myapi")
        assert len(apis) == 1
        assert apis[0].inputs["schema"] == INLINE_SCHEMA

    when_appsync_ready(api, check_resources)


@pulumi.runtime.test
def test_appsync_schema_from_file(pulumi_mocks, project_cwd):
    api = AppSync(
        "myapi", schema="schema.graphql", auth=CognitoAuth(user_pool_id=COGNITO_USER_POOL_ID)
    )

    def check_resources(_):
        apis = pulumi_mocks.created_appsync_apis(f"{TP}myapi")
        assert len(apis) == 1
        schema = apis[0].inputs["schema"]
        # Schema file should be read and contain the full SDL
        schema_path = Path(project_cwd) / "schema.graphql"
        assert schema == schema_path.read_text()

    when_appsync_ready(api, check_resources)


@pulumi.runtime.test
def test_appsync_url_property(pulumi_mocks, project_cwd):
    api = AppSync(
        "myapi", schema=INLINE_SCHEMA, auth=CognitoAuth(user_pool_id=COGNITO_USER_POOL_ID)
    )

    def check_url(args):
        url, api_id = args
        assert url == f"https://appsync-{api_id}.appsync-api.us-east-1.amazonaws.com/graphql"

    pulumi.Output.all(api.url, api.resources.api.id).apply(check_url)


@pulumi.runtime.test
def test_appsync_arn_property(pulumi_mocks, project_cwd):
    api = AppSync(
        "myapi", schema=INLINE_SCHEMA, auth=CognitoAuth(user_pool_id=COGNITO_USER_POOL_ID)
    )

    def check_arn(args):
        arn, api_id = args
        assert arn == f"arn:aws:appsync:us-east-1:123456789012:apis/appsync-{api_id}"

    pulumi.Output.all(api.arn, api.resources.api.id).apply(check_arn)


@pulumi.runtime.test
def test_appsync_api_id_property(pulumi_mocks, project_cwd):
    api = AppSync(
        "myapi", schema=INLINE_SCHEMA, auth=CognitoAuth(user_pool_id=COGNITO_USER_POOL_ID)
    )

    def check_id(args):
        api_id, arn, resource_api_id = args
        assert api_id == f"{TP}myapi-test-id"
        assert resource_api_id == api_id
        assert arn == f"arn:aws:appsync:us-east-1:123456789012:apis/appsync-{api_id}"

    pulumi.Output.all(api.api_id, api.arn, api.resources.api.id).apply(check_id)


def test_appsync_config_property_exposes_parsed_config(project_cwd):
    """The config property returns the parsed AppSyncConfig."""
    api = AppSync(
        "myapi", schema=INLINE_SCHEMA, auth=CognitoAuth(user_pool_id=COGNITO_USER_POOL_ID)
    )
    assert isinstance(api.config, AppSyncConfig)
    assert isinstance(api.config.auth, CognitoAuth)
    assert api.config.auth.user_pool_id == COGNITO_USER_POOL_ID


@pulumi.runtime.test
def test_appsync_allows_adding_data_source_after_resources_created(pulumi_mocks, project_cwd):
    """Adding a data source after accessing resources should work."""
    api = AppSync(
        "myapi", schema=INLINE_SCHEMA, auth=CognitoAuth(user_pool_id=COGNITO_USER_POOL_ID)
    )
    _ = api.resources

    # Should not raise — freeze behavior has been removed
    posts = api.data_source_lambda("posts", handler="functions/simple.handler")
    assert posts.name == "posts"


def test_appsync_constructor_rejects_child_resource_customize_key(project_cwd):
    with pytest.raises(ValueError, match=r"Unknown customization key\(s\)"):
        AppSync(
            "myapi",
            schema=INLINE_SCHEMA,
            auth=CognitoAuth(user_pool_id=COGNITO_USER_POOL_ID),
            customize={"service_role": {"path": "/x/"}},
        )


@pulumi.runtime.test
def test_none_data_source_accessible_through_resources(pulumi_mocks, project_cwd):
    """none_data_source is accessible via resources after creation."""
    api = AppSync(
        "myapi", schema=INLINE_SCHEMA, auth=CognitoAuth(user_pool_id=COGNITO_USER_POOL_ID)
    )

    def check_none_ds(none_ds_id):
        assert none_ds_id is not None

    api.none_data_source.id.apply(check_none_ds)


def test_appsync_missing_schema_file_raises(project_cwd):
    with pytest.raises(FileNotFoundError, match=r"missing\.graphql"):
        AppSync(
            "myapi",
            schema="missing.graphql",
            auth=CognitoAuth(user_pool_id=COGNITO_USER_POOL_ID),
        )


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

    api = AppSync(
        "myapi", schema=INLINE_SCHEMA, auth=CognitoAuth(user_pool_id=COGNITO_USER_POOL_ID)
    )

    def check_resources(_):
        apis = pulumi_mocks.created_appsync_apis(f"{TP}myapi")
        assert len(apis) == 1
        assert apis[0].inputs.get("xrayEnabled") is True

    when_appsync_ready(api, check_resources)


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
            customize={AppSyncDataSource: {"service_role": {"path": "/global-appsync/"}}},
        )
    )

    api = AppSync(
        "myapi", schema=INLINE_SCHEMA, auth=CognitoAuth(user_pool_id=COGNITO_USER_POOL_ID)
    )
    posts = make_lambda_ds(api)
    api.query("getPost", posts)

    def check_resources(_):
        roles = pulumi_mocks.created_roles()
        ds_roles = [r for r in roles if "ds-posts-role" in r.name]
        assert len(ds_roles) == 1
        assert ds_roles[0].inputs["path"] == "/global-appsync/"

    when_appsync_ready(api, check_resources)


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
            customize={AppSyncDataSource: {"service_role": {"path": "/global-appsync/"}}},
        )
    )

    api = AppSync(
        "myapi", schema=INLINE_SCHEMA, auth=CognitoAuth(user_pool_id=COGNITO_USER_POOL_ID)
    )
    posts = api.data_source_lambda(
        "posts",
        handler="functions/simple.handler",
        customize={"service_role": {"path": "/custom-ds/"}},
    )
    api.query("getPost", posts)

    def check_resources(_):
        roles = pulumi_mocks.created_roles()
        ds_roles = [r for r in roles if "ds-posts-role" in r.name]
        assert len(ds_roles) == 1
        # Per-DS should override global
        assert ds_roles[0].inputs["path"] == "/custom-ds/"

    when_appsync_ready(api, check_resources)


@pulumi.runtime.test
def test_appsync_tags_applied_to_graphql_api(pulumi_mocks, project_cwd):
    tags = {"Team": "platform", "Project": "api"}
    api = AppSync(
        "myapi",
        schema=INLINE_SCHEMA,
        auth=CognitoAuth(user_pool_id=COGNITO_USER_POOL_ID),
        tags=tags,
    )

    def check_resources(_):
        apis = pulumi_mocks.created_appsync_apis(f"{TP}myapi")
        assert len(apis) == 1
        assert apis[0].inputs["tags"] == tags

    when_appsync_ready(api, check_resources)


@pulumi.runtime.test
def test_appsync_tags_propagate_to_data_source_role(pulumi_mocks, project_cwd):
    tags = {"Team": "platform"}
    api = AppSync(
        "myapi",
        schema=INLINE_SCHEMA,
        auth=CognitoAuth(user_pool_id=COGNITO_USER_POOL_ID),
        tags=tags,
    )
    posts = make_lambda_ds(api)
    api.query("getPost", posts)

    def check_resources(_):
        roles = pulumi_mocks.created_roles()
        ds_roles = [r for r in roles if "ds-posts-role" in r.name]
        assert len(ds_roles) == 1
        assert ds_roles[0].inputs["tags"] == tags

        fns = pulumi_mocks.created_functions()
        ds_fns = [f for f in fns if "ds-posts" in f.name]
        assert len(ds_fns) == 1
        assert ds_fns[0].inputs["tags"] == tags

    when_appsync_ready(api, check_resources)


@pulumi.runtime.test
def test_appsync_no_tags_means_no_tags_key(pulumi_mocks, project_cwd):
    api = AppSync(
        "myapi",
        schema=INLINE_SCHEMA,
        auth=CognitoAuth(user_pool_id=COGNITO_USER_POOL_ID),
    )

    def check_resources(_):
        apis = pulumi_mocks.created_appsync_apis(f"{TP}myapi")
        assert len(apis) == 1
        assert "tags" not in apis[0].inputs

    when_appsync_ready(api, check_resources)


def test_appsync_config_dict_matches_dataclass():
    assert_config_dict_matches_dataclass(AppSyncConfig, AppSyncConfigDict)
