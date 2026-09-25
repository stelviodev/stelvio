from collections import Counter

import pulumi
from pytest import mark, param

from stelvio.aws.api_gateway import RestApi
from stelvio.aws.cognito.user_pool import UserPool

from ....conftest import TP
from ...pulumi_mocks import ACCOUNT_ID, DEFAULT_REGION, PulumiTestMocks, R, tid, tn
from .test_rest_api import (
    API_EXECUTION_ARN,
    API_NAME,
    LAMBDA_INVOKE_ARN_TEMPLATE,
    Funcs,
    rest_api_counts,
)

pytestmark = mark.usefixtures("project_cwd")

JWT = "functions/authorizers/jwt.handler"
REQUEST = "functions/authorizers/request.handler"
USER_POOL_ARN = f"arn:aws:cognito-idp:{DEFAULT_REGION}:{ACCOUNT_ID}:userpool/us-east-1_ABC123"
AUTHORIZER = Counter({R.API_AUTHORIZER: 1})


def assert_authorizer(mocks: PulumiTestMocks, name: str, inputs: dict) -> None:
    mocks.assert_res(
        f"{API_NAME}-authorizer-{name}",
        R.API_AUTHORIZER,
        {"restApi": tid(TP + API_NAME), "name": name, **inputs},
    )


def assert_lambda_authorizer(  # noqa: PLR0913
    mocks: PulumiTestMocks, name: str, handler: str, *, kind: str, identity_source: str, ttl: int
) -> None:
    """The authorizer, the Lambda it calls, and the invoke permission scoped to it."""
    function_name = f"{API_NAME}-auth-{name}"
    mocks.assert_res(
        function_name, R.FUNCTION, {"handler": handler.rsplit("/", 1)[-1]}, partial=True
    )
    assert_authorizer(
        mocks,
        name,
        {
            "type": kind,
            "authorizerUri": LAMBDA_INVOKE_ARN_TEMPLATE.format(
                function_name=tn(TP + function_name)
            ),
            "identitySource": identity_source,
            "authorizerResultTtlInSeconds": ttl,
        },
    )
    mocks.assert_res(
        f"{API_NAME}-authorizer-{name}-permission",
        R.LAMBDA_PERMISSION,
        {
            "action": "lambda:InvokeFunction",
            "function": tn(TP + function_name),
            "principal": "apigateway.amazonaws.com",
            "sourceArn": (
                f"{API_EXECUTION_ARN}/authorizers/{tid(f'{TP}{API_NAME}-authorizer-{name}')}"
            ),
        },
    )


def assert_method_auth(  # noqa: PLR0913
    mocks: PulumiTestMocks,
    method: str,
    path: str,
    authorization: str,
    *,
    authorizer: str | None = None,
    scopes: list[str] | None = None,
) -> None:
    expected = {
        "restApi": tid(TP + API_NAME),
        "resourceId": tid(f"{TP}{API_NAME}-resource-{path}"),
        "httpMethod": method,
        "authorization": authorization,
    }
    if authorizer is not None:
        expected["authorizerId"] = tid(f"{TP}{API_NAME}-authorizer-{authorizer}")
    if scopes is not None:
        expected["authorizationScopes"] = scopes
    mocks.assert_res(f"{API_NAME}-method-{method} {path}", R.API_METHOD, expected)


def test_token_authorizer_creates_resources(pulumi_mocks):
    """Route options still reach the route's own Lambda next to `auth`."""
    api = RestApi(API_NAME)
    auth = api.add_token_authorizer(
        "jwt-auth", JWT, identity_source="method.request.header.X-Token", ttl=600
    )
    api.route("GET", "/users", Funcs.SIMPLE.handler, auth=auth, memory=512)

    @pulumi.runtime.test
    def deploy():
        return api.resources

    deploy()

    assert_lambda_authorizer(
        pulumi_mocks,
        "jwt-auth",
        JWT,
        kind="TOKEN",
        identity_source="method.request.header.X-Token",
        ttl=600,
    )
    assert_method_auth(pulumi_mocks, "GET", "/users", "CUSTOM", authorizer="jwt-auth")
    pulumi_mocks.assert_res(
        Funcs.SIMPLE.full_name(API_NAME), R.FUNCTION, {"memorySize": 512}, partial=True
    )
    pulumi_mocks.assert_res_counts(rest_api_counts(2, 1, 1) + AUTHORIZER)


def test_request_authorizer_creates_resources(pulumi_mocks):
    """Several identity sources join into one comma-separated string; TTL defaults to 300."""
    api = RestApi(API_NAME)
    auth = api.add_request_authorizer(
        "request-auth",
        REQUEST,
        identity_source=[
            "method.request.header.X-Custom-Header",
            "method.request.querystring.token",
        ],
    )
    api.route("GET", "/users", Funcs.SIMPLE.handler, auth=auth)

    @pulumi.runtime.test
    def deploy():
        return api.resources

    deploy()

    assert_lambda_authorizer(
        pulumi_mocks,
        "request-auth",
        REQUEST,
        kind="REQUEST",
        identity_source="method.request.header.X-Custom-Header,method.request.querystring.token",
        ttl=300,
    )
    assert_method_auth(pulumi_mocks, "GET", "/users", "CUSTOM", authorizer="request-auth")
    pulumi_mocks.assert_res_counts(rest_api_counts(2, 1, 1) + AUTHORIZER)


def test_cognito_authorizer_creates_resources(pulumi_mocks):
    """No Lambda and no invoke permission: API Gateway talks to Cognito itself."""
    api = RestApi(API_NAME)
    auth = api.add_cognito_authorizer("cognito-auth", user_pools=[USER_POOL_ARN], ttl=450)
    api.route("GET", "/users", Funcs.SIMPLE.handler, auth=auth)

    @pulumi.runtime.test
    def deploy():
        return api.resources

    deploy()

    assert_authorizer(
        pulumi_mocks,
        "cognito-auth",
        {
            "type": "COGNITO_USER_POOLS",
            "providerArns": [USER_POOL_ARN],
            "authorizerResultTtlInSeconds": 450,
        },
    )
    assert_method_auth(
        pulumi_mocks, "GET", "/users", "COGNITO_USER_POOLS", authorizer="cognito-auth"
    )
    pulumi_mocks.assert_res_counts(rest_api_counts(1, 1, 1) + AUTHORIZER)


def test_cognito_authorizer_accepts_user_pool_components_and_arns(pulumi_mocks):
    pool = UserPool("auth-pool", usernames=["email"])
    external_arn = f"arn:aws:cognito-idp:eu-west-1:{ACCOUNT_ID}:userpool/eu-west-1_EXT456"
    api = RestApi(API_NAME)
    auth = api.add_cognito_authorizer("cognito-auth", user_pools=[pool, external_arn])
    api.route("GET", "/users", Funcs.SIMPLE.handler, auth=auth)

    @pulumi.runtime.test
    def deploy():
        return api.resources

    deploy()

    assert_authorizer(
        pulumi_mocks,
        "cognito-auth",
        {
            "type": "COGNITO_USER_POOLS",
            "providerArns": [
                f"arn:aws:cognito-idp:{DEFAULT_REGION}:{ACCOUNT_ID}:userpool/"
                f"{DEFAULT_REGION}_{tid(TP + 'auth-pool')}",
                external_arn,
            ],
            "authorizerResultTtlInSeconds": 300,
        },
    )


@mark.parametrize(
    ("auth", "authorization", "authorizer"),
    [
        param(
            lambda api: api.add_token_authorizer("jwt-auth", JWT),
            "CUSTOM",
            "jwt-auth",
            id="authorizer",
        ),
        param("IAM", "AWS_IAM", None, id="iam"),
        param(False, "NONE", None, id="public"),
        param(None, "NONE", None, id="unset"),
    ],
)
def test_route_auth_sets_method_authorization(pulumi_mocks, auth, authorization, authorizer):
    api = RestApi(API_NAME)
    api.route("GET", "/users", Funcs.SIMPLE.handler, auth=auth(api) if callable(auth) else auth)

    @pulumi.runtime.test
    def deploy():
        return api.resources

    deploy()

    assert_method_auth(pulumi_mocks, "GET", "/users", authorization, authorizer=authorizer)


def test_default_auth_applies_to_routes_without_explicit_auth(pulumi_mocks):
    api = RestApi(API_NAME)
    api.default_auth = api.add_token_authorizer("jwt-auth", JWT)
    api.route("GET", "/default", Funcs.SIMPLE.handler)

    @pulumi.runtime.test
    def deploy():
        return api.resources

    deploy()

    assert_lambda_authorizer(
        pulumi_mocks,
        "jwt-auth",
        JWT,
        kind="TOKEN",
        identity_source="method.request.header.Authorization",
        ttl=300,
    )
    assert_method_auth(pulumi_mocks, "GET", "/default", "CUSTOM", authorizer="jwt-auth")


def test_default_iam_applies_to_routes_without_explicit_auth(pulumi_mocks):
    api = RestApi(API_NAME)
    api.default_auth = "IAM"
    api.route("GET", "/default", Funcs.SIMPLE.handler)

    @pulumi.runtime.test
    def deploy():
        return api.resources

    deploy()

    assert_method_auth(pulumi_mocks, "GET", "/default", "AWS_IAM")
    pulumi_mocks.assert_res_counts(rest_api_counts(1, 1, 1))


def test_explicit_auth_overrides_default_auth(pulumi_mocks):
    api = RestApi(API_NAME)
    api.default_auth = api.add_token_authorizer("jwt-auth", JWT)
    request_auth = api.add_request_authorizer("request-auth", REQUEST)
    api.route("GET", "/custom", Funcs.USERS.handler, auth=request_auth)

    @pulumi.runtime.test
    def deploy():
        return api.resources

    deploy()

    assert_method_auth(pulumi_mocks, "GET", "/custom", "CUSTOM", authorizer="request-auth")
    pulumi_mocks.assert_res_counts(rest_api_counts(3, 1, 1) + Counter({R.API_AUTHORIZER: 2}))


def test_auth_false_opts_out_of_default_auth(pulumi_mocks):
    api = RestApi(API_NAME)
    api.default_auth = api.add_token_authorizer("jwt-auth", JWT)
    api.route("GET", "/public", Funcs.ORDERS.handler, auth=False)

    @pulumi.runtime.test
    def deploy():
        return api.resources

    deploy()

    assert_method_auth(pulumi_mocks, "GET", "/public", "NONE")


@mark.parametrize(
    "scopes",
    [
        param(["users:read"], id="single"),
        param(["users:write", "admin"], id="multiple"),
        param([], id="empty"),
        param(None, id="none"),
    ],
)
def test_cognito_scopes_reach_the_method(pulumi_mocks, scopes):
    api = RestApi(API_NAME)
    auth = api.add_cognito_authorizer("cognito-auth", user_pools=[USER_POOL_ARN])
    api.route("GET", "/users", Funcs.SIMPLE.handler, auth=auth, cognito_scopes=scopes)

    @pulumi.runtime.test
    def deploy():
        return api.resources

    deploy()

    assert_method_auth(
        pulumi_mocks,
        "GET",
        "/users",
        "COGNITO_USER_POOLS",
        authorizer="cognito-auth",
        scopes=scopes,
    )
