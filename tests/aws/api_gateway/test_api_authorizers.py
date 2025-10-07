"""Tests for API Gateway authorizer functionality.

Tests verify that authorizers are created with correct Pulumi resources and that
routes properly use authorization configuration.
"""

import pulumi
import pytest
from pulumi.runtime import set_mocks

from stelvio.aws.api_gateway import Api

from ..pulumi_mocks import (
    ACCOUNT_ID,
    DEFAULT_REGION,
    SAMPLE_API_ID,
    PulumiTestMocks,
    tid,
)
from .test_api import Funcs, PathPart, TestApiConfig, reset_api_gateway_caches

# Test prefix
TP = "test-test-"


@pytest.fixture
def pulumi_mocks():
    reset_api_gateway_caches()
    mocks = PulumiTestMocks()
    set_mocks(mocks)
    return mocks


@pytest.fixture(autouse=True)
def project_cwd(monkeypatch, pytestconfig):
    from pathlib import Path

    rootpath = pytestconfig.rootpath
    test_project_dir = rootpath / "tests" / "aws" / "sample_test_project"
    monkeypatch.chdir(test_project_dir)

    yield test_project_dir

    # Clean up generated files
    for file_path in test_project_dir.rglob("stlv_resources.py"):
        file_path.unlink()


def assert_authorizer(
    mocks: PulumiTestMocks,
    name: str,
    expected_type: str,
    identity_source: str | None = None,
    ttl: int = 300,
    provider_arns: list[str] | None = None,
):
    """Assert authorizer Pulumi resource was created correctly."""
    authorizers = mocks.created_authorizers()
    matching = [a for a in authorizers if a.inputs.get("name") == name]
    assert len(matching) == 1, f"Expected 1 authorizer named '{name}', found {len(matching)}"

    authorizer = matching[0]
    assert authorizer.inputs["type"] == expected_type
    assert authorizer.inputs["restApi"] == tid(TP + TestApiConfig.NAME)
    assert authorizer.inputs["authorizerResultTtlInSeconds"] == ttl

    if identity_source is not None:
        assert authorizer.inputs["identitySource"] == identity_source

    if provider_arns is not None:
        assert authorizer.inputs["providerArns"] == provider_arns

    # For Lambda-based authorizers, verify URI
    if expected_type in ("TOKEN", "REQUEST"):
        assert "authorizerUri" in authorizer.inputs
        uri = authorizer.inputs["authorizerUri"]
        assert DEFAULT_REGION in uri
        assert "lambda:path/2015-03-31/functions" in uri

    return authorizer


def assert_authorizer_permission(mocks: PulumiTestMocks, authorizer_name: str):
    """Assert Lambda permission for authorizer was created."""
    permissions = mocks.created_permissions()
    matching = [
        p
        for p in permissions
        if "authorizer" in p.name and authorizer_name in p.name
    ]
    assert len(matching) == 1, f"Expected 1 permission for authorizer '{authorizer_name}'"

    permission = matching[0]
    assert permission.inputs["action"] == "lambda:InvokeFunction"
    assert permission.inputs["principal"] == "apigateway.amazonaws.com"
    assert "sourceArn" in permission.inputs
    assert "authorizers" in permission.inputs["sourceArn"]

    return permission


def assert_method_authorization(
    mocks: PulumiTestMocks,
    path_part: str,
    expected_authorization: str,
    should_have_authorizer_id: bool = False,
):
    """Assert Method has correct authorization configuration."""
    methods = mocks.created_methods()
    matching = [m for m in methods if path_part in m.name]
    assert len(matching) >= 1, f"No method found with '{path_part}' in name"

    method = matching[0]
    assert method.inputs["authorization"] == expected_authorization

    if should_have_authorizer_id:
        assert "authorizerId" in method.inputs
        assert method.inputs["authorizerId"] is not None
    else:
        # Either not present or explicitly None
        assert method.inputs.get("authorizerId") is None

    return method


@pulumi.runtime.test
def test_add_token_authorizer(pulumi_mocks):
    """Test adding a TOKEN authorizer.

    Verifies:
    - Authorizer created with type=TOKEN
    - Identity source configured
    - TTL set correctly
    - Lambda permission created
    - Method uses CUSTOM authorization
    """
    # Arrange
    api = Api(TestApiConfig.NAME)
    auth = api.add_token_authorizer(
        "jwt-auth",
        "functions/authorizers/jwt.handler",
        identity_source="method.request.header.Authorization",
        ttl=600,
    )
    api.route("GET", f"/{PathPart.USERS}", Funcs.SIMPLE.handler, auth=auth)

    # Act
    _ = api.resources

    # Assert
    def check_resources(_):
        assert_authorizer(
            pulumi_mocks,
            "jwt-auth",
            "TOKEN",
            identity_source="method.request.header.Authorization",
            ttl=600,
        )
        assert_authorizer_permission(pulumi_mocks, "jwt-auth")
        assert_method_authorization(
            pulumi_mocks, PathPart.USERS, "CUSTOM", should_have_authorizer_id=True
        )

    api.resources.stage.id.apply(check_resources)


@pulumi.runtime.test
def test_add_request_authorizer(pulumi_mocks):
    """Test adding a REQUEST authorizer.

    Verifies:
    - Authorizer created with type=REQUEST
    - Multiple identity sources joined with commas
    - Lambda permission created
    """
    # Arrange
    api = Api(TestApiConfig.NAME)
    auth = api.add_request_authorizer(
        "request-auth",
        "functions/authorizers/request.handler",
        identity_source=[
            "method.request.header.X-Custom-Header",
            "method.request.querystring.token",
        ],
        ttl=300,
    )
    api.route("GET", f"/{PathPart.USERS}", Funcs.SIMPLE.handler, auth=auth)

    # Act
    _ = api.resources

    # Assert
    def check_resources(_):
        assert_authorizer(
            pulumi_mocks,
            "request-auth",
            "REQUEST",
            identity_source="method.request.header.X-Custom-Header,method.request.querystring.token",
            ttl=300,
        )
        assert_authorizer_permission(pulumi_mocks, "request-auth")
        assert_method_authorization(
            pulumi_mocks, PathPart.USERS, "CUSTOM", should_have_authorizer_id=True
        )

    api.resources.stage.id.apply(check_resources)


@pulumi.runtime.test
def test_add_cognito_authorizer(pulumi_mocks):
    """Test adding a COGNITO_USER_POOLS authorizer.

    Verifies:
    - Authorizer created with type=COGNITO_USER_POOLS
    - Provider ARNs configured
    - NO Lambda permission (Cognito doesn't use Lambda)
    """
    # Arrange
    api = Api(TestApiConfig.NAME)
    user_pool_arn = f"arn:aws:cognito-idp:{DEFAULT_REGION}:{ACCOUNT_ID}:userpool/us-east-1_ABC123"
    auth = api.add_cognito_authorizer("cognito-auth", user_pools=[user_pool_arn], ttl=450)
    api.route("GET", f"/{PathPart.USERS}", Funcs.SIMPLE.handler, auth=auth)

    # Act
    _ = api.resources

    # Assert
    def check_resources(_):
        assert_authorizer(
            pulumi_mocks,
            "cognito-auth",
            "COGNITO_USER_POOLS",
            ttl=450,
            provider_arns=[user_pool_arn],
        )

        # Verify NO Lambda permission for Cognito authorizers
        permissions = pulumi_mocks.created_permissions()
        authorizer_permissions = [
            p for p in permissions if "authorizer" in p.name and "cognito-auth" in p.name
        ]
        assert len(authorizer_permissions) == 0

        assert_method_authorization(
            pulumi_mocks, PathPart.USERS, "CUSTOM", should_have_authorizer_id=True
        )

    api.resources.stage.id.apply(check_resources)


@pulumi.runtime.test
def test_route_with_auth_parameter(pulumi_mocks):
    """Test route() method with various auth parameter values.

    Verifies:
    - auth=Authorizer -> CUSTOM authorization with authorizer_id
    - auth="IAM" -> AWS_IAM authorization without authorizer_id
    - auth=False -> NONE authorization without authorizer_id
    - No auth parameter -> NONE authorization without authorizer_id
    """
    # Arrange
    api = Api(TestApiConfig.NAME)
    token_auth = api.add_token_authorizer("jwt-auth", "functions/authorizers/jwt.handler")

    api.route("GET", "/protected", Funcs.SIMPLE.handler, auth=token_auth)
    api.route("GET", "/iam", Funcs.USERS.handler, auth="IAM")
    api.route("GET", "/public", Funcs.ORDERS.handler, auth=False)
    api.route("GET", "/default", "functions/simple.handler")

    # Act
    _ = api.resources

    # Assert
    def check_resources(_):
        assert_method_authorization(pulumi_mocks, "protected", "CUSTOM", should_have_authorizer_id=True)
        assert_method_authorization(pulumi_mocks, "iam", "AWS_IAM", should_have_authorizer_id=False)
        assert_method_authorization(pulumi_mocks, "public", "NONE", should_have_authorizer_id=False)
        assert_method_authorization(pulumi_mocks, "default", "NONE", should_have_authorizer_id=False)

    api.resources.stage.id.apply(check_resources)


@pulumi.runtime.test
def test_set_default_auth(pulumi_mocks):
    """Test set_default_auth() method.

    Verifies:
    - Default auth applied to routes without explicit auth
    - Routes with explicit auth override default
    - Routes with auth=False opt out of default
    """
    # Arrange
    api = Api(TestApiConfig.NAME)
    token_auth = api.add_token_authorizer("jwt-auth", "functions/authorizers/jwt.handler")
    request_auth = api.add_request_authorizer("request-auth", "functions/authorizers/request.handler")

    api.set_default_auth(token_auth)

    api.route("GET", "/default", Funcs.SIMPLE.handler)  # Should use token_auth
    api.route("GET", "/custom", Funcs.USERS.handler, auth=request_auth)  # Override
    api.route("GET", "/public", Funcs.ORDERS.handler, auth=False)  # Opt out

    # Act
    _ = api.resources

    # Assert
    def check_resources(_):
        # All should use CUSTOM authorization
        assert_method_authorization(pulumi_mocks, "default", "CUSTOM", should_have_authorizer_id=True)
        assert_method_authorization(pulumi_mocks, "custom", "CUSTOM", should_have_authorizer_id=True)
        # Except public which opted out
        assert_method_authorization(pulumi_mocks, "public", "NONE", should_have_authorizer_id=False)

        # Both authorizers should have been created
        authorizers = pulumi_mocks.created_authorizers()
        assert len(authorizers) == 2

    api.resources.stage.id.apply(check_resources)


@pulumi.runtime.test
def test_complete_authorizer_flow(pulumi_mocks):
    """Integration test for complete authorizer flow.

    Tests all three authorizer types together with various route configurations.
    """
    # Arrange
    api = Api(TestApiConfig.NAME)

    # Add all three types
    token_auth = api.add_token_authorizer("jwt-auth", "functions/authorizers/jwt.handler")
    request_auth = api.add_request_authorizer(
        "request-auth",
        "functions/authorizers/request.handler",
        identity_source=["method.request.header.X-Custom"],
    )
    cognito_auth = api.add_cognito_authorizer(
        "cognito-auth",
        user_pools=[f"arn:aws:cognito-idp:{DEFAULT_REGION}:{ACCOUNT_ID}:userpool/test"],
    )

    api.set_default_auth(token_auth)

    # Various configurations
    api.route("GET", "/protected", Funcs.SIMPLE.handler)  # Uses default (token_auth)
    api.route("POST", "/request", Funcs.USERS.handler, auth=request_auth)
    api.route("GET", "/cognito", Funcs.ORDERS.handler, auth=cognito_auth)
    api.route("GET", "/iam", "functions/simple.handler", auth="IAM")
    api.route("GET", "/public", "functions/users.handler", auth=False)

    # Act
    _ = api.resources

    # Assert
    def check_resources(_):
        # All 3 authorizers created
        authorizers = pulumi_mocks.created_authorizers()
        assert len(authorizers) == 3

        # Only 2 Lambda permissions (not for Cognito)
        permissions = pulumi_mocks.created_permissions()
        authorizer_permissions = [p for p in permissions if "authorizer" in p.name]
        assert len(authorizer_permissions) == 2

        # 5 methods created
        methods = pulumi_mocks.created_methods()
        assert len(methods) == 5

        # Verify authorization types
        assert_method_authorization(pulumi_mocks, "protected", "CUSTOM", should_have_authorizer_id=True)
        assert_method_authorization(pulumi_mocks, "request", "CUSTOM", should_have_authorizer_id=True)
        assert_method_authorization(pulumi_mocks, "cognito", "CUSTOM", should_have_authorizer_id=True)
        assert_method_authorization(pulumi_mocks, "iam", "AWS_IAM", should_have_authorizer_id=False)
        assert_method_authorization(pulumi_mocks, "public", "NONE", should_have_authorizer_id=False)

    api.resources.stage.id.apply(check_resources)
