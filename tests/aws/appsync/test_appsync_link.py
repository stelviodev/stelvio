"""AppSync link system tests — linking to AppSync from other components."""

import pulumi

from stelvio.aws.appsync import ApiKeyAuth, AppSync, CognitoAuth

from .conftest import COGNITO_USER_POOL_ID, INLINE_SCHEMA, when_appsync_ready

TP = "test-test-"


@pulumi.runtime.test
def test_appsync_link_provides_url_and_permission(pulumi_mocks, project_cwd):
    api = AppSync("myapi", INLINE_SCHEMA, auth=CognitoAuth(user_pool_id=COGNITO_USER_POOL_ID))

    link = api.link()

    def check_resources(_):
        def verify_link(args):
            properties, permissions = args
            assert "url" in properties
            assert (
                properties["url"]
                == f"https://appsync-{TP}myapi-test-id.appsync-api.us-east-1.amazonaws.com/graphql"
            )

            assert len(permissions) == 1
            perm = permissions[0]
            assert perm.actions == ["appsync:GraphQL"]
            assert len(perm.resources) == 1

        pulumi.Output.all(link.properties, link.permissions).apply(verify_link)

    when_appsync_ready(api, check_resources)


@pulumi.runtime.test
def test_appsync_link_permission_resource_matches_arn(pulumi_mocks, project_cwd):
    api = AppSync("myapi", INLINE_SCHEMA, auth=CognitoAuth(user_pool_id=COGNITO_USER_POOL_ID))

    link = api.link()
    resource = link.permissions[0].resources[0]

    def check_resources(_):
        def verify_resource(args):
            resolved_resource, arn = args
            assert resolved_resource == f"{arn}/*"

        pulumi.Output.all(resource, api.arn).apply(verify_resource)

    when_appsync_ready(api, check_resources)


@pulumi.runtime.test
def test_appsync_link_includes_api_key_when_configured(pulumi_mocks, project_cwd):
    api = AppSync("myapi", INLINE_SCHEMA, auth=ApiKeyAuth())

    link = api.link()

    def check_resources(_):
        def verify_link(args):
            properties = args[0]
            assert "url" in properties
            assert "api_key" in properties
            assert properties["api_key"] == f"da2-test-api-key-{TP}myapi-api-key-test-id"

        pulumi.Output.all(link.properties).apply(verify_link)

    when_appsync_ready(api, check_resources)


@pulumi.runtime.test
def test_appsync_link_no_api_key_when_not_configured(pulumi_mocks, project_cwd):
    api = AppSync("myapi", INLINE_SCHEMA, auth="iam")

    link = api.link()

    def check_resources(_):
        def verify_link(args):
            properties = args[0]
            assert "url" in properties
            assert "api_key" not in properties

        pulumi.Output.all(link.properties).apply(verify_link)

    when_appsync_ready(api, check_resources)


@pulumi.runtime.test
def test_appsync_link_api_key_from_additional_auth(pulumi_mocks, project_cwd):
    api = AppSync(
        "myapi",
        INLINE_SCHEMA,
        auth=CognitoAuth(user_pool_id=COGNITO_USER_POOL_ID),
        additional_auth=[ApiKeyAuth()],
    )

    link = api.link()

    def check_resources(_):
        def verify_link(args):
            properties = args[0]
            assert "api_key" in properties

        pulumi.Output.all(link.properties).apply(verify_link)

    when_appsync_ready(api, check_resources)
