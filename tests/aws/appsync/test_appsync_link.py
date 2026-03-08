"""AppSync link system tests — linking to AppSync from other components."""

import pulumi
import pytest

from stelvio.aws.appsync import ApiKeyAuth, CognitoAuth

from .conftest import COGNITO_USER_POOL_ID, make_api

TP = "test-test-"


@pytest.mark.parametrize(
    "case",
    [
        (CognitoAuth(user_pool_id=COGNITO_USER_POOL_ID), None, False, None),
        (ApiKeyAuth(), None, True, f"da2-test-api-key-{TP}myapi-api-key-test-id"),
        ("iam", None, False, None),
        (
            CognitoAuth(user_pool_id=COGNITO_USER_POOL_ID),
            [ApiKeyAuth()],
            True,
            f"da2-test-api-key-{TP}myapi-api-key-test-id",
        ),
    ],
    ids=["default-cognito", "default-api-key", "iam-no-api-key", "additional-api-key"],
)
@pulumi.runtime.test
def test_appsync_link_properties_and_permissions(case, pulumi_mocks, project_cwd):
    auth, additional_auth, expect_api_key, expected_api_key = case
    api = make_api(auth=auth, additional_auth=additional_auth)
    link = api.link()
    resource = link.permissions[0].resources[0]

    def verify_link(args):
        _, properties, permissions, resolved_resource, arn = args
        assert properties["url"] == (
            f"https://appsync-{TP}myapi-test-id.appsync-api.us-east-1.amazonaws.com/graphql"
        )

        assert len(permissions) == 1
        assert permissions[0].actions == ["appsync:GraphQL"]
        assert len(permissions[0].resources) == 1
        assert resolved_resource == f"{arn}/*"

        if expect_api_key:
            assert properties["api_key"] == expected_api_key
        else:
            assert "api_key" not in properties

    pulumi.Output.all(
        api.resources.api.id,
        link.properties,
        link.permissions,
        resource,
        api.arn,
    ).apply(verify_link)
