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

from ...test_utils import assert_config_dict_matches_dataclass
from .conftest import (
    COGNITO_USER_POOL_ID,
    INLINE_SCHEMA,
    TP,
    assert_graphql_api_inputs,
    assert_role,
    make_api,
    make_lambda_ds,
    set_context_with_customize,
    when_appsync_ready,
)


@pulumi.runtime.test
def test_appsync_creates_graphql_api(pulumi_mocks, project_cwd):
    api = make_api()

    def check_resources(_):
        assert_graphql_api_inputs(
            pulumi_mocks,
            f"{TP}myapi",
            schema=INLINE_SCHEMA,
            authenticationType=AUTH_TYPE_COGNITO,
        )

    when_appsync_ready(api, check_resources)


@pulumi.runtime.test
def test_appsync_schema_from_file(pulumi_mocks, project_cwd):
    api = make_api(schema="schema.graphql")

    def check_resources(_):
        schema = assert_graphql_api_inputs(pulumi_mocks, f"{TP}myapi")["schema"]
        schema_path = Path(project_cwd) / "schema.graphql"
        assert schema == schema_path.read_text()

    when_appsync_ready(api, check_resources)


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


@pulumi.runtime.test
def test_global_customization_propagates_to_api(pulumi_mocks, project_cwd, clean_registries):
    """Global customization for AppSync propagates to the GraphQL API."""
    set_context_with_customize({AppSync: {"api": {"xray_enabled": True}}})
    api = make_api()

    def check_resources(_):
        inputs = assert_graphql_api_inputs(pulumi_mocks, f"{TP}myapi")
        assert inputs.get("xrayEnabled") is True

    when_appsync_ready(api, check_resources)


@pytest.mark.parametrize(
    ("per_ds_customize", "expected_path"),
    [
        (None, "/global-appsync/"),
        ({"service_role": {"path": "/custom-ds/"}}, "/custom-ds/"),
    ],
    ids=["global-propagates", "per-ds-overrides"],
)
@pulumi.runtime.test
def test_data_source_customization_precedence(
    per_ds_customize, expected_path, pulumi_mocks, project_cwd, clean_registries
):
    """Global customization propagates; per-DS customize overrides it."""
    set_context_with_customize({AppSyncDataSource: {"service_role": {"path": "/global-appsync/"}}})
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

    def check_resources(_):
        assert_role(pulumi_mocks, "ds-posts-role", path=expected_path)

    when_appsync_ready(api, check_resources)


@pytest.mark.parametrize(
    "tags",
    [{"Team": "platform", "Project": "api"}, None],
    ids=["with-tags", "no-tags"],
)
@pulumi.runtime.test
def test_appsync_tags_on_graphql_api(tags, pulumi_mocks, project_cwd):
    kwargs = {"tags": tags} if tags else {}
    api = make_api(**kwargs)

    def check_resources(_):
        inputs = assert_graphql_api_inputs(pulumi_mocks, f"{TP}myapi")
        if tags:
            assert inputs["tags"] == tags
        else:
            assert "tags" not in inputs

    when_appsync_ready(api, check_resources)


@pulumi.runtime.test
def test_appsync_tags_propagate_to_data_source_role(pulumi_mocks, project_cwd):
    tags = {"Team": "platform"}
    api = make_api(tags=tags)
    posts = make_lambda_ds(api)
    api.query("getPost", posts)

    def check_resources(_):
        assert_role(pulumi_mocks, "ds-posts-role", tags=tags)
        fns = [f for f in pulumi_mocks.created_functions() if "ds-posts" in f.name]
        assert len(fns) == 1
        assert fns[0].inputs["tags"] == tags

    when_appsync_ready(api, check_resources)


def test_appsync_config_dict_matches_dataclass():
    assert_config_dict_matches_dataclass(AppSyncConfig, AppSyncConfigDict)
