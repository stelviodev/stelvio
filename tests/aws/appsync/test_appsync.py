"""Tests for AppSync API creation and configuration."""

import pulumi
import pytest

from stelvio.aws.appsync import AppSync

# Test prefix (matches conftest.py setup)
TP = "test-test-"

# Sample GraphQL schema for testing
SAMPLE_SCHEMA = """
type Query {
    getUser(id: ID!): User
}

type User {
    id: ID!
    name: String!
    email: String
}
"""


# =============================================================================
# API Creation Tests
# =============================================================================


@pulumi.runtime.test
def test_creates_graphql_api(pulumi_mocks, project_cwd):
    """Test that AppSync creates a GraphQL API resource."""
    api = AppSync("my-api", SAMPLE_SCHEMA)

    def check_resources(_):
        apis = pulumi_mocks.created_appsync_apis()
        assert len(apis) == 1
        assert apis[0].typ == "aws:appsync/graphQLApi:GraphQLApi"
        assert f"{TP}my-api" in apis[0].name
        assert apis[0].inputs["schema"] == SAMPLE_SCHEMA

    api.resources.api.id.apply(check_resources)


@pulumi.runtime.test
def test_creates_api_key_by_default(pulumi_mocks, project_cwd):
    """Test that API key is created by default."""
    api = AppSync("my-api", SAMPLE_SCHEMA)

    def check_resources(_):
        keys = pulumi_mocks.created_appsync_api_keys()
        assert len(keys) == 1
        assert keys[0].typ == "aws:appsync/apiKey:ApiKey"

    # Wait for API key to be created, not just API
    api.resources.api_key.id.apply(check_resources)


@pulumi.runtime.test
def test_default_auth_is_api_key(pulumi_mocks, project_cwd):
    """Test that default authentication type is API_KEY."""
    api = AppSync("my-api", SAMPLE_SCHEMA)

    def check_resources(_):
        apis = pulumi_mocks.created_appsync_apis()
        assert len(apis) == 1
        assert apis[0].inputs["authenticationType"] == "API_KEY"

    api.resources.api.id.apply(check_resources)


@pulumi.runtime.test
def test_api_key_expires_custom(pulumi_mocks, project_cwd):
    """Test custom API key expiration."""
    api = AppSync("my-api", SAMPLE_SCHEMA, api_key_expires=30)

    def check_resources(_):
        keys = pulumi_mocks.created_appsync_api_keys()
        assert len(keys) == 1
        # Check that expires is set (we can't easily check the exact value)
        assert "expires" in keys[0].inputs

    # Wait for API key to be created, not just API
    api.resources.api_key.id.apply(check_resources)


@pulumi.runtime.test
def test_no_api_key_when_expires_zero(pulumi_mocks, project_cwd):
    """Test that no API key is created when api_key_expires=0."""
    api = AppSync(
        "my-api",
        SAMPLE_SCHEMA,
        api_key_expires=0,
        additional_auth=[
            {
                "type": "OPENID_CONNECT",
                "issuer": "https://auth.example.com",
            }
        ],
    )

    def check_resources(_):
        keys = pulumi_mocks.created_appsync_api_keys()
        assert len(keys) == 0

    api.resources.api.id.apply(check_resources)


# =============================================================================
# Authentication Configuration Tests
# =============================================================================


@pulumi.runtime.test
def test_cognito_additional_auth(pulumi_mocks, project_cwd):
    """Test Cognito user pool additional authentication."""
    api = AppSync(
        "my-api",
        SAMPLE_SCHEMA,
        additional_auth=[
            {
                "type": "AMAZON_COGNITO_USER_POOLS",
                "user_pool_id": "us-east-1_xxxxx",
                "aws_region": "us-east-1",
                "app_id_client_regex": None,
            }
        ],
    )

    def check_resources(_):
        apis = pulumi_mocks.created_appsync_apis()
        assert len(apis) == 1
        providers = apis[0].inputs.get("additionalAuthenticationProviders", [])
        assert len(providers) == 1
        assert providers[0]["authenticationType"] == "AMAZON_COGNITO_USER_POOLS"

    api.resources.api.id.apply(check_resources)


@pulumi.runtime.test
def test_oidc_additional_auth(pulumi_mocks, project_cwd):
    """Test OIDC additional authentication."""
    api = AppSync(
        "my-api",
        SAMPLE_SCHEMA,
        additional_auth=[
            {
                "type": "OPENID_CONNECT",
                "issuer": "https://auth.example.com",
                "client_id": "my-client",
                "auth_ttl": 3600000,
                "iat_ttl": 3600000,
            }
        ],
    )

    def check_resources(_):
        apis = pulumi_mocks.created_appsync_apis()
        assert len(apis) == 1
        providers = apis[0].inputs.get("additionalAuthenticationProviders", [])
        assert len(providers) == 1
        assert providers[0]["authenticationType"] == "OPENID_CONNECT"

    api.resources.api.id.apply(check_resources)


@pulumi.runtime.test
def test_lambda_additional_auth(pulumi_mocks, project_cwd):
    """Test Lambda authorizer additional authentication."""
    api = AppSync(
        "my-api",
        SAMPLE_SCHEMA,
        additional_auth=[
            {
                "type": "AWS_LAMBDA",
                "handler": "functions/auth.authorize",
            }
        ],
    )

    def check_resources(_):
        apis = pulumi_mocks.created_appsync_apis()
        assert len(apis) == 1
        providers = apis[0].inputs.get("additionalAuthenticationProviders", [])
        assert len(providers) == 1
        assert providers[0]["authenticationType"] == "AWS_LAMBDA"

        # Verify Lambda function was created
        functions = pulumi_mocks.created_functions()
        auth_functions = [f for f in functions if "auth" in f.name]
        assert len(auth_functions) >= 1

    api.resources.api.id.apply(check_resources)


@pulumi.runtime.test
def test_multiple_additional_auth(pulumi_mocks, project_cwd):
    """Test multiple additional authentication providers."""
    api = AppSync(
        "my-api",
        SAMPLE_SCHEMA,
        additional_auth=[
            {
                "type": "AMAZON_COGNITO_USER_POOLS",
                "user_pool_id": "us-east-1_xxxxx",
                "aws_region": None,
                "app_id_client_regex": None,
            },
            {
                "type": "OPENID_CONNECT",
                "issuer": "https://auth.example.com",
                "client_id": None,
                "auth_ttl": None,
                "iat_ttl": None,
            },
        ],
    )

    def check_resources(_):
        apis = pulumi_mocks.created_appsync_apis()
        assert len(apis) == 1
        providers = apis[0].inputs.get("additionalAuthenticationProviders", [])
        assert len(providers) == 2

    api.resources.api.id.apply(check_resources)


# =============================================================================
# Properties Tests
# =============================================================================


@pulumi.runtime.test
def test_api_properties(pulumi_mocks, project_cwd):
    """Test API properties are accessible."""
    api = AppSync("my-api", SAMPLE_SCHEMA)

    def check_properties(args):
        url, api_id, arn = args
        assert url is not None
        assert api_id is not None
        assert arn is not None

    pulumi.Output.all(
        api.url,
        api.api_id,
        api.arn,
    ).apply(check_properties)


# =============================================================================
# Validation Tests
# =============================================================================


def test_empty_schema_raises_error():
    """Test that empty schema raises ValueError."""
    with pytest.raises(ValueError, match="Schema cannot be empty"):
        AppSync("my-api", "")


def test_negative_api_key_expires_raises_error():
    """Test that negative api_key_expires raises ValueError."""
    with pytest.raises(ValueError, match="api_key_expires must be non-negative"):
        AppSync("my-api", SAMPLE_SCHEMA, api_key_expires=-1)


def test_cognito_auth_missing_user_pool_raises_error():
    """Test that Cognito auth without user_pool_id raises ValueError."""
    with pytest.raises(ValueError, match="Cognito auth requires 'user_pool_id'"):
        AppSync(
            "my-api",
            SAMPLE_SCHEMA,
            additional_auth=[
                {
                    "type": "AMAZON_COGNITO_USER_POOLS",
                    "user_pool_id": "",
                    "aws_region": None,
                    "app_id_client_regex": None,
                }
            ],
        )


def test_oidc_auth_missing_issuer_raises_error():
    """Test that OIDC auth without issuer raises ValueError."""
    with pytest.raises(ValueError, match="OIDC auth requires 'issuer'"):
        AppSync(
            "my-api",
            SAMPLE_SCHEMA,
            additional_auth=[
                {
                    "type": "OPENID_CONNECT",
                    "issuer": "",
                    "client_id": None,
                    "auth_ttl": None,
                    "iat_ttl": None,
                }
            ],
        )


def test_lambda_auth_missing_handler_raises_error():
    """Test that Lambda auth without handler raises ValueError."""
    with pytest.raises(ValueError, match="Lambda auth requires 'handler'"):
        AppSync(
            "my-api",
            SAMPLE_SCHEMA,
            additional_auth=[
                {
                    "type": "AWS_LAMBDA",
                }
            ],
        )


def test_no_auth_raises_error(pulumi_mocks, project_cwd):
    """Test that no authentication method raises ValueError."""
    api = AppSync(
        "my-api",
        SAMPLE_SCHEMA,
        api_key_expires=0,
    )
    with pytest.raises(ValueError, match="At least one authentication method is required"):
        _ = api.resources


def test_unknown_auth_type_raises_error():
    """Test that unknown auth type raises ValueError."""
    with pytest.raises(ValueError, match="Unknown auth type"):
        AppSync(
            "my-api",
            SAMPLE_SCHEMA,
            additional_auth=[
                {
                    "type": "INVALID_TYPE",
                }
            ],
        )


def test_cannot_modify_after_resources_created(pulumi_mocks, project_cwd):
    """Test that modifications after resource creation raise RuntimeError."""
    api = AppSync("my-api", SAMPLE_SCHEMA)
    # Trigger resource creation
    _ = api.resources

    with pytest.raises(RuntimeError, match="Cannot modify AppSync"):
        api.add_data_source("users", handler="functions/users.handler")


# =============================================================================
# Link Tests
# =============================================================================


@pulumi.runtime.test
def test_appsync_link(pulumi_mocks, project_cwd):
    """Test that AppSync can be linked to Lambda functions."""
    api = AppSync("my-api", SAMPLE_SCHEMA)

    link = api.link()

    def verify_link(args):
        properties, permissions, url, api_id, arn = args

        assert properties["url"] == url
        assert properties["api_id"] == api_id
        assert properties["arn"] == arn

        assert len(permissions) == 1
        permission = permissions[0]
        assert permission.actions == ["appsync:GraphQL"]

    pulumi.Output.all(
        link.properties,
        link.permissions,
        api.url,
        api.api_id,
        api.arn,
    ).apply(verify_link)
