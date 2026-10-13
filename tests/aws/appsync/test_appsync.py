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

from ...test_utils import assert_config_dict_matches_dataclass
from ..conftest import create_app_context_with_global_customize
from ..pulumi_mocks import R
from .conftest import (
    API_ID,
    COGNITO_USER_POOL_ID,
    INLINE_SCHEMA,
    TP,
    deploy_all,
    make_api,
    make_lambda_ds,
)


def test_appsync_creates_graphql_api(pulumi_mocks, project_cwd):
    make_api()

    deploy_all()

    pulumi_mocks.assert_res(
        "myapi",
        R.GRAPHQL_API,
        {
            "name": f"{TP}myapi",
            "schema": INLINE_SCHEMA,
            "authenticationType": "AMAZON_COGNITO_USER_POOLS",
            "userPoolConfig": {"defaultAction": "ALLOW", "userPoolId": COGNITO_USER_POOL_ID},
        },
    )
    pulumi_mocks.assert_res(
        "myapi-none-ds",
        R.APPSYNC_DATA_SOURCE,
        {"name": "NONE", "type": "NONE", "apiId": API_ID},
    )
    pulumi_mocks.assert_res_counts({R.GRAPHQL_API: 1, R.APPSYNC_DATA_SOURCE: 1})


def test_appsync_schema_from_file(pulumi_mocks, project_cwd):
    make_api(schema="schema.graphql")

    deploy_all()

    schema = (Path(project_cwd) / "schema.graphql").read_text()
    pulumi_mocks.assert_res("myapi", R.GRAPHQL_API, {"schema": schema}, partial=True)


@pulumi.runtime.test
def test_appsync_output_properties(pulumi_mocks, project_cwd):
    api = make_api()

    def check_properties(args):
        url, arn, api_id, resource_api_id = args
        assert api_id == f"{TP}myapi-test-id"
        assert resource_api_id == api_id
        assert url == f"https://appsync-{api_id}.appsync-api.us-east-1.amazonaws.com/graphql"
        assert arn == f"arn:aws:appsync:us-east-1:123456789012:apis/appsync-{api_id}"

    pulumi.Output.all(api.url, api.arn, api.api_id, api.resources.api.id).apply(check_properties)


def test_appsync_config_property_exposes_parsed_config(project_cwd):
    """The config property returns the parsed AppSyncConfig."""
    api = make_api()
    assert isinstance(api.config, AppSyncConfig)
    assert isinstance(api.config.auth, CognitoAuth)
    assert api.config.auth.user_pool_id == COGNITO_USER_POOL_ID


@pulumi.runtime.test
def test_appsync_allows_adding_data_source_after_resources_created(pulumi_mocks, project_cwd):
    """Adding a data source after accessing resources should work."""
    api = make_api()
    _ = api.resources
    posts = api.data_source_lambda("posts", handler="functions/simple.handler")
    assert posts.name == "posts"


@pytest.mark.parametrize(
    ("setup", "error_type", "match"),
    [
        (
            lambda: make_api(customize={"service_role": {"path": "/x/"}}),
            ValueError,
            r"Unknown customization key\(s\)",
        ),
        (
            lambda: make_api(schema="missing.graphql"),
            FileNotFoundError,
            r"missing\.graphql",
        ),
    ],
    ids=["bad-customize-key", "missing-schema-file"],
)
def test_appsync_constructor_validation(setup, error_type, match, project_cwd):
    with pytest.raises(error_type, match=match):
        setup()


@pulumi.runtime.test
def test_none_data_source_accessible_through_resources(pulumi_mocks, project_cwd):
    """none_data_source is accessible via resources after creation."""
    api = make_api()

    def check_none_ds(none_ds_id):
        assert none_ds_id is not None

    api.none_data_source.id.apply(check_none_ds)


# --- Global customization propagation ---


def test_global_customization_propagates_to_api(pulumi_mocks, project_cwd, clean_registries):
    """Global customization for AppSync propagates to the GraphQL API."""
    create_app_context_with_global_customize({AppSync: {"api": {"xray_enabled": True}}})
    make_api()

    deploy_all()

    pulumi_mocks.assert_res("myapi", R.GRAPHQL_API, {"xrayEnabled": True}, partial=True)


@pytest.mark.parametrize(
    ("per_ds_customize", "expected_path"),
    [
        (None, "/global-appsync/"),
        ({"service_role": {"path": "/custom-ds/"}}, "/custom-ds/"),
    ],
    ids=["global-propagates", "per-ds-overrides"],
)
def test_data_source_customization_precedence(
    per_ds_customize, expected_path, pulumi_mocks, project_cwd, clean_registries
):
    """Global customization propagates; per-DS customize overrides it."""
    create_app_context_with_global_customize(
        {AppSyncDataSource: {"service_role": {"path": "/global-appsync/"}}}
    )
    api = make_api()
    if per_ds_customize:
        posts = api.data_source_lambda(
            "posts",
            handler="functions/simple.handler",
            customize=per_ds_customize,
        )
    else:
        posts = make_lambda_ds(api)
    api.query("getPost", posts)

    deploy_all()

    pulumi_mocks.assert_res("myapi-ds-posts-role", R.ROLE, {"path": expected_path}, partial=True)
    assert len(pulumi_mocks.created(R.ROLE)) == 2


@pytest.mark.parametrize(
    "tags",
    [{"Team": "platform", "Project": "api"}, None],
    ids=["with-tags", "no-tags"],
)
def test_appsync_tags_on_graphql_api(tags, pulumi_mocks, project_cwd):
    kwargs = {"tags": tags} if tags else {}
    make_api(**kwargs)

    deploy_all()

    pulumi_mocks.assert_res("myapi", R.GRAPHQL_API, {"tags": tags}, partial=True)


def test_appsync_tags_propagate_to_data_source_role(pulumi_mocks, project_cwd):
    tags = {"Team": "platform"}
    api = make_api(tags=tags)
    posts = make_lambda_ds(api)
    api.query("getPost", posts)

    deploy_all()

    pulumi_mocks.assert_res("myapi-ds-posts-role", R.ROLE, {"tags": tags}, partial=True)
    pulumi_mocks.assert_res("myapi-ds-posts-fn", R.FUNCTION, {"tags": tags}, partial=True)
    assert len(pulumi_mocks.created(R.ROLE)) == 2
    assert len(pulumi_mocks.created(R.FUNCTION)) == 1


def test_appsync_config_dict_matches_dataclass():
    assert_config_dict_matches_dataclass(AppSyncConfig, AppSyncConfigDict)
