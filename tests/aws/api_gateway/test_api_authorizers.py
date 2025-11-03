"""Tests for API Gateway authorizer functionality."""

import pulumi
import pytest
from pulumi.runtime import set_mocks

from stelvio.aws.api_gateway import Api
from stelvio.aws.function import Function
from stelvio.component import ComponentRegistry

from ..pulumi_mocks import (
    ACCOUNT_ID,
    DEFAULT_REGION,
    SAMPLE_API_ID,
    PulumiTestMocks,
    tid,
    tn,
)
from .test_api import Funcs, PathPart, TestApiConfig, reset_api_gateway_caches

# Test prefix
TP = "test-test-"

# Test constants
TEST_USER_POOL_ARN = f"arn:aws:cognito-idp:{DEFAULT_REGION}:{ACCOUNT_ID}:userpool/us-east-1_ABC123"
TEST_SCOPE_SINGLE = ["users:read"]
TEST_SCOPES_MULTIPLE = ["users:write", "admin"]


@pytest.fixture
def pulumi_mocks():
    reset_api_gateway_caches()
    mocks = PulumiTestMocks()
    set_mocks(mocks)
    return mocks


@pytest.fixture(autouse=True)
def project_cwd(monkeypatch, pytestconfig):
    rootpath = pytestconfig.rootpath
    test_project_dir = rootpath / "tests" / "aws" / "sample_test_project"
    monkeypatch.chdir(test_project_dir)

    yield test_project_dir

    # Clean up generated files
    for file_path in test_project_dir.rglob("stlv_resources.py"):
        file_path.unlink()


def assert_authorizer(  # noqa: PLR0913
    mocks: PulumiTestMocks,
    name: str,
    expected_type: str,
    identity_source: str | None = None,
    ttl: int = 300,
    provider_arns: list[str] | None = None,
    function_handler: str | None = None,
):
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

    # For Lambda-based authorizers, verify exact URI
    if expected_type in ("TOKEN", "REQUEST"):
        assert function_handler is not None, "function_handler required for Lambda authorizers"

        # Build expected function name: {api_name}-auth-{authorizer_name}
        function_name = tn(f"{TP}{TestApiConfig.NAME}-auth-{name}")

        # Build exact expected URI
        expected_uri = (
            f"arn:aws:apigateway:{DEFAULT_REGION}:lambda:path/2015-03-31/functions/"
            f"arn:aws:lambda:{DEFAULT_REGION}:{ACCOUNT_ID}:function:{function_name}/invocations"
        )

        assert authorizer.inputs["authorizerUri"] == expected_uri

    return authorizer


def assert_authorizer_permission(mocks: PulumiTestMocks, authorizer_name: str):
    permissions = mocks.created_permissions()
    matching = [p for p in permissions if "authorizer" in p.name and authorizer_name in p.name]
    assert len(matching) == 1, f"Expected 1 permission for authorizer '{authorizer_name}'"

    permission = matching[0]

    # Check exact permission details
    assert permission.inputs["action"] == "lambda:InvokeFunction"
    assert permission.inputs["principal"] == "apigateway.amazonaws.com"

    # Check exact function name
    expected_function_name = tn(f"{TP}{TestApiConfig.NAME}-auth-{authorizer_name}")
    assert permission.inputs["function"] == expected_function_name

    # Build and check exact source ARN
    # sourceArn format: {execution_arn}/authorizers/{authorizer_id}
    authorizer_resource_name = f"{TP}{TestApiConfig.NAME}-authorizer-{authorizer_name}"
    authorizer_id = tid(authorizer_resource_name)
    expected_source_arn = (
        f"arn:aws:execute-api:{DEFAULT_REGION}:{ACCOUNT_ID}:{SAMPLE_API_ID}"
        f"/authorizers/{authorizer_id}"
    )
    assert permission.inputs["sourceArn"] == expected_source_arn

    return permission


def assert_method_authorization(
    mocks: PulumiTestMocks,
    path_part: str,
    expected_authorization: str,
    should_have_authorizer_id: bool = False,
    expected_scopes: list[str] | None = None,
):
    methods = mocks.created_methods()
    matching = [m for m in methods if path_part in m.name]
    assert len(matching) >= 1, f"No method found with '{path_part}' in name"

    method = matching[0]
    assert method.inputs["authorization"] == expected_authorization

    if should_have_authorizer_id:
        assert "authorizerId" in method.inputs
        assert method.inputs["authorizerId"] is not None
    else:
        assert method.inputs.get("authorizerId") is None

    if expected_scopes is not None:
        actual_scopes = method.inputs.get("authorizationScopes", [])
        assert set(actual_scopes) == set(expected_scopes), (
            f"Expected scopes {expected_scopes}, got {actual_scopes}"
        )
    else:
        assert method.inputs.get("authorizationScopes") is None

    return method


def assert_authorizer_function(api_name: str, authorizer_name: str, handler: str):
    functions = ComponentRegistry._instances.get(Function, [])
    function_map = {f.name: f for f in functions}

    expected_name = f"{api_name}-auth-{authorizer_name}"
    assert expected_name in function_map, f"Function '{expected_name}' not created"

    func = function_map[expected_name]
    assert func.config.handler == handler

    return func


@pulumi.runtime.test
def test_token_authorizer_creates_correct_resources(pulumi_mocks):
    api = Api(TestApiConfig.NAME)
    auth = api.add_token_authorizer(
        "jwt-auth",
        "functions/authorizers/jwt.handler",
        identity_source="method.request.header.Authorization",
        ttl=600,
    )
    api.route("GET", f"/{PathPart.USERS}", Funcs.SIMPLE.handler, auth=auth)

    _ = api.resources

    def check_resources(_):
        assert_authorizer(
            pulumi_mocks,
            "jwt-auth",
            "TOKEN",
            identity_source="method.request.header.Authorization",
            ttl=600,
            function_handler="functions/authorizers/jwt.handler",
        )

    api.resources.stage.id.apply(check_resources)


@pulumi.runtime.test
def test_token_authorizer_creates_lambda_function(pulumi_mocks):
    api = Api(TestApiConfig.NAME)
    auth = api.add_token_authorizer("jwt-auth", "functions/authorizers/jwt.handler")
    api.route("GET", f"/{PathPart.USERS}", Funcs.SIMPLE.handler, auth=auth)

    _ = api.resources

    def check_resources(_):
        assert_authorizer_function(
            TestApiConfig.NAME, "jwt-auth", "functions/authorizers/jwt.handler"
        )

    api.resources.stage.id.apply(check_resources)


@pulumi.runtime.test
def test_token_authorizer_creates_lambda_permission(pulumi_mocks):
    api = Api(TestApiConfig.NAME)
    auth = api.add_token_authorizer("jwt-auth", "functions/authorizers/jwt.handler")
    api.route("GET", f"/{PathPart.USERS}", Funcs.SIMPLE.handler, auth=auth)

    _ = api.resources

    def check_resources(_):
        assert_authorizer_permission(pulumi_mocks, "jwt-auth")

    api.resources.stage.id.apply(check_resources)


@pulumi.runtime.test
def test_token_authorizer_method_uses_custom_authorization(pulumi_mocks):
    api = Api(TestApiConfig.NAME)
    auth = api.add_token_authorizer("jwt-auth", "functions/authorizers/jwt.handler")
    api.route("GET", f"/{PathPart.USERS}", Funcs.SIMPLE.handler, auth=auth)

    _ = api.resources

    def check_resources(_):
        assert_method_authorization(
            pulumi_mocks, PathPart.USERS, "CUSTOM", should_have_authorizer_id=True
        )

    api.resources.stage.id.apply(check_resources)


@pulumi.runtime.test
def test_request_authorizer_creates_correct_resources(pulumi_mocks):
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

    _ = api.resources

    def check_resources(_):
        assert_authorizer(
            pulumi_mocks,
            "request-auth",
            "REQUEST",
            identity_source="method.request.header.X-Custom-Header,method.request.querystring.token",
            ttl=300,
            function_handler="functions/authorizers/request.handler",
        )

    api.resources.stage.id.apply(check_resources)


@pulumi.runtime.test
def test_request_authorizer_creates_lambda_function(pulumi_mocks):
    api = Api(TestApiConfig.NAME)
    auth = api.add_request_authorizer("request-auth", "functions/authorizers/request.handler")
    api.route("GET", f"/{PathPart.USERS}", Funcs.SIMPLE.handler, auth=auth)

    _ = api.resources

    def check_resources(_):
        assert_authorizer_function(
            TestApiConfig.NAME, "request-auth", "functions/authorizers/request.handler"
        )

    api.resources.stage.id.apply(check_resources)


@pulumi.runtime.test
def test_request_authorizer_creates_lambda_permission(pulumi_mocks):
    api = Api(TestApiConfig.NAME)
    auth = api.add_request_authorizer("request-auth", "functions/authorizers/request.handler")
    api.route("GET", f"/{PathPart.USERS}", Funcs.SIMPLE.handler, auth=auth)

    _ = api.resources

    def check_resources(_):
        assert_authorizer_permission(pulumi_mocks, "request-auth")

    api.resources.stage.id.apply(check_resources)


@pulumi.runtime.test
def test_cognito_authorizer_creates_correct_resources(pulumi_mocks):
    api = Api(TestApiConfig.NAME)
    auth = api.add_cognito_authorizer("cognito-auth", user_pools=[TEST_USER_POOL_ARN], ttl=450)
    api.route("GET", f"/{PathPart.USERS}", Funcs.SIMPLE.handler, auth=auth)

    _ = api.resources

    def check_resources(_):
        assert_authorizer(
            pulumi_mocks,
            "cognito-auth",
            "COGNITO_USER_POOLS",
            ttl=450,
            provider_arns=[TEST_USER_POOL_ARN],
        )

    api.resources.stage.id.apply(check_resources)


@pulumi.runtime.test
def test_cognito_authorizer_does_not_create_lambda_permission(pulumi_mocks):
    api = Api(TestApiConfig.NAME)
    auth = api.add_cognito_authorizer("cognito-auth", user_pools=[TEST_USER_POOL_ARN])
    api.route("GET", f"/{PathPart.USERS}", Funcs.SIMPLE.handler, auth=auth)

    _ = api.resources

    def check_resources(_):
        permissions = pulumi_mocks.created_permissions()
        authorizer_permissions = [
            p for p in permissions if "authorizer" in p.name and "cognito-auth" in p.name
        ]
        assert len(authorizer_permissions) == 0

    api.resources.stage.id.apply(check_resources)


@pulumi.runtime.test
def test_route_with_authorizer_uses_custom_authorization(pulumi_mocks):
    api = Api(TestApiConfig.NAME)
    token_auth = api.add_token_authorizer("jwt-auth", "functions/authorizers/jwt.handler")
    api.route("GET", "/protected", Funcs.SIMPLE.handler, auth=token_auth)

    _ = api.resources

    def check_resources(_):
        assert_method_authorization(
            pulumi_mocks, "protected", "CUSTOM", should_have_authorizer_id=True
        )

    api.resources.stage.id.apply(check_resources)


@pulumi.runtime.test
def test_route_with_iam_auth_uses_aws_iam_authorization(pulumi_mocks):
    api = Api(TestApiConfig.NAME)
    api.route("GET", "/iam", Funcs.USERS.handler, auth="IAM")

    _ = api.resources

    def check_resources(_):
        assert_method_authorization(
            pulumi_mocks, "iam", "AWS_IAM", should_have_authorizer_id=False
        )

    api.resources.stage.id.apply(check_resources)


@pulumi.runtime.test
def test_route_with_auth_false_uses_none_authorization(pulumi_mocks):
    api = Api(TestApiConfig.NAME)
    api.route("GET", "/public", Funcs.ORDERS.handler, auth=False)

    _ = api.resources

    def check_resources(_):
        assert_method_authorization(
            pulumi_mocks, "public", "NONE", should_have_authorizer_id=False
        )

    api.resources.stage.id.apply(check_resources)


@pulumi.runtime.test
def test_route_without_auth_parameter_uses_none_authorization(pulumi_mocks):
    api = Api(TestApiConfig.NAME)
    api.route("GET", "/default", "functions/simple.handler")

    _ = api.resources

    def check_resources(_):
        assert_method_authorization(
            pulumi_mocks, "default", "NONE", should_have_authorizer_id=False
        )

    api.resources.stage.id.apply(check_resources)


@pulumi.runtime.test
def test_default_auth_applied_to_routes_without_explicit_auth(pulumi_mocks):
    api = Api(TestApiConfig.NAME)
    token_auth = api.add_token_authorizer("jwt-auth", "functions/authorizers/jwt.handler")
    api.default_auth = token_auth

    api.route("GET", "/default", Funcs.SIMPLE.handler)

    _ = api.resources

    def check_resources(_):
        assert_method_authorization(
            pulumi_mocks, "default", "CUSTOM", should_have_authorizer_id=True
        )

    api.resources.stage.id.apply(check_resources)


@pulumi.runtime.test
def test_explicit_auth_overrides_default_auth(pulumi_mocks):
    api = Api(TestApiConfig.NAME)
    token_auth = api.add_token_authorizer("jwt-auth", "functions/authorizers/jwt.handler")
    request_auth = api.add_request_authorizer(
        "request-auth", "functions/authorizers/request.handler"
    )
    api.default_auth = token_auth

    api.route("GET", "/custom", Funcs.USERS.handler, auth=request_auth)

    _ = api.resources

    def check_resources(_):
        assert_method_authorization(
            pulumi_mocks, "custom", "CUSTOM", should_have_authorizer_id=True
        )
        # Verify both authorizers were created
        authorizers = pulumi_mocks.created_authorizers()
        assert len(authorizers) == 2

    api.resources.stage.id.apply(check_resources)


@pulumi.runtime.test
def test_auth_false_opts_out_of_default_auth(pulumi_mocks):
    api = Api(TestApiConfig.NAME)
    token_auth = api.add_token_authorizer("jwt-auth", "functions/authorizers/jwt.handler")
    api.default_auth = token_auth

    api.route("GET", "/public", Funcs.ORDERS.handler, auth=False)

    _ = api.resources

    def check_resources(_):
        assert_method_authorization(
            pulumi_mocks, "public", "NONE", should_have_authorizer_id=False
        )

    api.resources.stage.id.apply(check_resources)


@pulumi.runtime.test
def test_cognito_scopes_single_scope_passed_to_method(pulumi_mocks):
    api = Api(TestApiConfig.NAME)
    cognito_auth = api.add_cognito_authorizer("cognito-auth", user_pools=[TEST_USER_POOL_ARN])
    api.route(
        "GET",
        f"/{PathPart.USERS}",
        Funcs.SIMPLE.handler,
        auth=cognito_auth,
        cognito_scopes=TEST_SCOPE_SINGLE,
    )

    _ = api.resources

    def check_resources(_):
        assert_method_authorization(
            pulumi_mocks,
            PathPart.USERS,
            "CUSTOM",
            should_have_authorizer_id=True,
            expected_scopes=TEST_SCOPE_SINGLE,
        )

    api.resources.stage.id.apply(check_resources)


@pulumi.runtime.test
def test_cognito_scopes_multiple_scopes_passed_to_method(pulumi_mocks):
    api = Api(TestApiConfig.NAME)
    cognito_auth = api.add_cognito_authorizer("cognito-auth", user_pools=[TEST_USER_POOL_ARN])
    api.route(
        "POST",
        f"/{PathPart.USERS}",
        Funcs.USERS.handler,
        auth=cognito_auth,
        cognito_scopes=TEST_SCOPES_MULTIPLE,
    )

    _ = api.resources

    def check_resources(_):
        assert_method_authorization(
            pulumi_mocks,
            PathPart.USERS,
            "CUSTOM",
            should_have_authorizer_id=True,
            expected_scopes=TEST_SCOPES_MULTIPLE,
        )

    api.resources.stage.id.apply(check_resources)


@pulumi.runtime.test
def test_cognito_scopes_none_not_passed_to_method(pulumi_mocks):
    api = Api(TestApiConfig.NAME)
    cognito_auth = api.add_cognito_authorizer("cognito-auth", user_pools=[TEST_USER_POOL_ARN])
    api.route("GET", f"/{PathPart.ORDERS}", Funcs.ORDERS.handler, auth=cognito_auth)

    _ = api.resources

    def check_resources(_):
        assert_method_authorization(
            pulumi_mocks,
            PathPart.ORDERS,
            "CUSTOM",
            should_have_authorizer_id=True,
            expected_scopes=None,
        )

    api.resources.stage.id.apply(check_resources)
