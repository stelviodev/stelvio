"""Tests for HTTP API authorizers."""

import pulumi
import pytest

from stelvio.aws.api_gateway.http_api import HttpApi
from stelvio.aws.cognito import UserPool

from ...pulumi_mocks import (
    ACCOUNT_ID,
    DEFAULT_REGION,
    tid,
    tn,
)
from .conftest import TP, when_http_api_ready

pytestmark = pytest.mark.usefixtures("project_cwd")

# The PulumiTestMocks derive the API id from the resource id: api_id = tid(name)[:8].
HTTP_API_ID = tid(TP + "my-api")[:8]
API_EXECUTION_ARN = f"arn:aws:execute-api:{DEFAULT_REGION}:{ACCOUNT_ID}:{HTTP_API_ID}"
AUTHORIZER_PERMISSION_SOURCE_ARN = f"{API_EXECUTION_ARN}/authorizers/*"
LAMBDA_INVOKE_ARN_TEMPLATE = (
    f"arn:aws:apigateway:{DEFAULT_REGION}:lambda:path/2015-03-31/functions/"
    f"arn:aws:lambda:{DEFAULT_REGION}:{ACCOUNT_ID}:function:{{function_name}}/invocations"
)


# ---------------------------------------------------------------------------
# Lambda authorizer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("simple_response", "expected_simple_response"),
    [(True, True), (False, False)],
    ids=["simple_response_enabled", "simple_response_disabled"],
)
@pulumi.runtime.test
def test_lambda_authorizer_creates_authorizer_resource(
    pulumi_mocks, simple_response, expected_simple_response
):
    api = HttpApi("my-api")
    auth = api.add_lambda_authorizer(
        "my-auth",
        "functions/simple.handler",
        identity_sources=["$request.header.Authorization"],
        simple_response=simple_response,
    )
    api.route("GET", "/secure", "functions/users.handler", auth=auth)
    _ = api.resources

    def check(_):
        authorizers = pulumi_mocks.created_http_api_authorizers()
        assert len(authorizers) == 1
        assert authorizers[0].typ == "aws:apigatewayv2/authorizer:Authorizer"
        assert authorizers[0].name == TP + "my-api-authorizer-my-auth"
        assert authorizers[0].inputs["authorizerType"] == "REQUEST"
        assert authorizers[0].inputs["apiId"] == tid(TP + "my-api")
        assert authorizers[0].inputs["name"] == "my-auth"
        assert authorizers[0].inputs["authorizerPayloadFormatVersion"] == "2.0"
        assert authorizers[0].inputs["enableSimpleResponses"] is expected_simple_response
        assert authorizers[0].inputs["identitySources"] == ["$request.header.Authorization"]
        # Authorizer URI must be the invoke ARN of its dedicated Lambda
        expected_uri = LAMBDA_INVOKE_ARN_TEMPLATE.format(
            function_name=tn(TP + "my-api-auth-my-auth")
        )
        assert authorizers[0].inputs["authorizerUri"] == expected_uri

        authorizer_functions = pulumi_mocks.created_functions(TP + "my-api-auth-my-auth")
        assert len(authorizer_functions) == 1
        assert authorizer_functions[0].typ == "aws:lambda/function:Function"
        assert authorizer_functions[0].inputs["handler"] == "simple.handler"

    when_http_api_ready(api, check)


@pulumi.runtime.test
def test_lambda_authorizer_creates_permission(pulumi_mocks):
    api = HttpApi("my-api")
    auth = api.add_lambda_authorizer(
        "my-auth",
        "functions/simple.handler",
        identity_sources=["$request.header.Authorization"],
    )
    api.route("GET", "/secure", "functions/users.handler", auth=auth)
    _ = api.resources

    def check(_):
        perms = pulumi_mocks.created_permissions()
        # One permission for the route Lambda (/*/*) and one for the authorizer (/authorizers/*)
        assert len(perms) == 2
        auth_perms = [
            p for p in perms if p.inputs["sourceArn"] == AUTHORIZER_PERMISSION_SOURCE_ARN
        ]
        assert len(auth_perms) == 1
        perm = auth_perms[0]
        assert perm.typ == "aws:lambda/permission:Permission"
        assert perm.inputs["action"] == "lambda:InvokeFunction"
        assert perm.inputs["principal"] == "apigateway.amazonaws.com"
        assert perm.inputs["function"] == tn(TP + "my-api-auth-my-auth")

    when_http_api_ready(api, check)


@pulumi.runtime.test
def test_lambda_authorizer_route_has_custom_auth_type(pulumi_mocks):
    api = HttpApi("my-api")
    auth = api.add_lambda_authorizer(
        "my-auth",
        "functions/simple.handler",
        identity_sources=["$request.header.Authorization"],
    )
    api.route("GET", "/secure", "functions/users.handler", auth=auth)
    _ = api.resources

    def check(_):
        routes = pulumi_mocks.created_http_api_routes()
        assert len(routes) == 1
        assert routes[0].typ == "aws:apigatewayv2/route:Route"
        assert routes[0].inputs["routeKey"] == "GET /secure"
        assert routes[0].inputs["authorizationType"] == "CUSTOM"
        authorizers = pulumi_mocks.created_http_api_authorizers()
        assert len(authorizers) == 1
        assert routes[0].inputs["authorizerId"] == tid(authorizers[0].name)

    when_http_api_ready(api, check)


@pulumi.runtime.test
def test_lambda_authorizer_ttl_zero(pulumi_mocks):
    api = HttpApi("my-api")
    auth = api.add_lambda_authorizer(
        "my-auth",
        "functions/simple.handler",
        identity_sources=["$request.header.Authorization"],
        ttl=0,
    )
    api.route("GET", "/secure", "functions/users.handler", auth=auth)
    _ = api.resources

    def check(_):
        authorizers = pulumi_mocks.created_http_api_authorizers()
        assert authorizers[0].inputs["authorizerResultTtlInSeconds"] == 0

    when_http_api_ready(api, check)


def test_lambda_authorizer_invalid_ttl_raises():
    api = HttpApi("my-api")
    with pytest.raises(ValueError, match="ttl"):
        api.add_lambda_authorizer(
            "bad-auth",
            "functions/simple.handler",
            identity_sources=["$request.header.Authorization"],
            ttl=3601,
        )


def test_lambda_authorizer_empty_identity_sources_raises():
    api = HttpApi("my-api")
    with pytest.raises(ValueError, match="identity_source"):
        api.add_lambda_authorizer(
            "bad-auth",
            "functions/simple.handler",
            identity_sources=[],
        )


def test_lambda_authorizer_requires_identity_sources_list():
    api = HttpApi("my-api")
    with pytest.raises(TypeError, match="identity_sources"):
        api.add_lambda_authorizer(
            "my-auth",
            "functions/simple.handler",
            identity_sources="method.request.header.Authorization",
        )


@pulumi.runtime.test
def test_lambda_authorizer_supports_function_config_dict(pulumi_mocks):
    api = HttpApi("my-api")
    auth = api.add_lambda_authorizer(
        "my-auth",
        {"handler": "functions/simple.handler", "timeout": 10},
        identity_sources=["$request.header.Authorization"],
    )
    api.route("GET", "/secure", "functions/users.handler", auth=auth)
    _ = api.resources

    def check(_):
        functions = pulumi_mocks.created_functions(TP + "my-api-auth-my-auth")
        assert len(functions) == 1
        assert functions[0].inputs["handler"] == "simple.handler"
        assert functions[0].inputs["timeout"] == 10

    when_http_api_ready(api, check)


@pulumi.runtime.test
def test_lambda_authorizer_uses_default_ttl(pulumi_mocks):
    api = HttpApi("my-api")
    auth = api.add_lambda_authorizer(
        "my-auth",
        "functions/simple.handler",
        identity_sources=["$request.header.Authorization"],
    )
    api.route("GET", "/secure", "functions/users.handler", auth=auth)
    _ = api.resources

    def check(_):
        authorizers = pulumi_mocks.created_http_api_authorizers()
        assert authorizers[0].inputs["authorizerResultTtlInSeconds"] == 300

    when_http_api_ready(api, check)


# ---------------------------------------------------------------------------
# JWT authorizer
# ---------------------------------------------------------------------------


@pulumi.runtime.test
def test_jwt_authorizer_creates_authorizer_resource(pulumi_mocks):
    api = HttpApi("my-api")
    auth = api.add_jwt_authorizer(
        "my-jwt",
        issuer="https://accounts.google.com",
        audiences=["my-client-id"],
    )
    api.route("GET", "/secure", "functions/simple.handler", auth=auth)
    _ = api.resources

    def check(_):
        authorizers = pulumi_mocks.created_http_api_authorizers()
        assert len(authorizers) == 1
        assert authorizers[0].typ == "aws:apigatewayv2/authorizer:Authorizer"
        assert authorizers[0].name == TP + "my-api-authorizer-my-jwt"
        assert authorizers[0].inputs["authorizerType"] == "JWT"
        assert authorizers[0].inputs["apiId"] == tid(TP + "my-api")
        assert authorizers[0].inputs["name"] == "my-jwt"
        assert authorizers[0].inputs["identitySources"] == ["$request.header.Authorization"]
        jwt_config = authorizers[0].inputs["jwtConfiguration"]
        assert jwt_config["issuer"] == "https://accounts.google.com"
        assert jwt_config["audiences"] == ["my-client-id"]

    when_http_api_ready(api, check)


@pulumi.runtime.test
def test_jwt_authorizer_route_has_jwt_auth_type(pulumi_mocks):
    api = HttpApi("my-api")
    auth = api.add_jwt_authorizer(
        "my-jwt",
        issuer="https://accounts.google.com",
        audiences=["my-client-id"],
    )
    api.route("GET", "/secure", "functions/simple.handler", auth=auth)
    _ = api.resources

    def check(_):
        routes = pulumi_mocks.created_http_api_routes()
        assert len(routes) == 1
        assert routes[0].typ == "aws:apigatewayv2/route:Route"
        assert routes[0].inputs["routeKey"] == "GET /secure"
        assert routes[0].inputs["authorizationType"] == "JWT"
        authorizers = pulumi_mocks.created_http_api_authorizers()
        assert len(authorizers) == 1
        assert routes[0].inputs["authorizerId"] == tid(authorizers[0].name)

    when_http_api_ready(api, check)


@pulumi.runtime.test
def test_route_with_iam_auth_uses_aws_iam_authorization(pulumi_mocks):
    api = HttpApi("my-api")
    api.route("GET", "/secure", "functions/simple.handler", auth="IAM")
    _ = api.resources

    def check(_):
        routes = pulumi_mocks.created_http_api_routes()

        assert len(routes) == 1
        assert routes[0].typ == "aws:apigatewayv2/route:Route"
        assert routes[0].inputs["routeKey"] == "GET /secure"
        assert routes[0].inputs["authorizationType"] == "AWS_IAM"
        assert "authorizerId" not in routes[0].inputs

    when_http_api_ready(api, check)


@pulumi.runtime.test
def test_jwt_authorizer_with_scopes(pulumi_mocks):
    api = HttpApi("my-api")
    auth = api.add_jwt_authorizer(
        "my-jwt",
        issuer="https://accounts.google.com",
        audiences=["my-client-id"],
    )
    api.route(
        "GET",
        "/secure",
        "functions/simple.handler",
        auth=auth,
        jwt_scopes=["read:users"],
    )
    _ = api.resources

    def check(_):
        routes = pulumi_mocks.created_http_api_routes()
        assert routes[0].inputs.get("authorizationScopes") == ["read:users"]

    when_http_api_ready(api, check)


def test_jwt_authorizer_empty_issuer_raises():
    api = HttpApi("my-api")
    with pytest.raises(ValueError, match="issuer"):
        api.add_jwt_authorizer("jwt", issuer="", audiences=["aud"])


def test_jwt_authorizer_empty_audiences_raises():
    api = HttpApi("my-api")
    with pytest.raises(ValueError, match="audiences"):
        api.add_jwt_authorizer("jwt", issuer="https://example.com", audiences=[])


def test_jwt_authorizer_keeps_identity_source_as_given():
    api = HttpApi("my-api")
    auth = api.add_jwt_authorizer(
        "jwt",
        issuer="https://example.com",
        audiences=["aud"],
        identity_source="method.request.header.Authorization",
    )

    assert auth.identity_source == "method.request.header.Authorization"


@pulumi.runtime.test
def test_jwt_scopes_with_no_auth_raises(pulumi_mocks):
    api = HttpApi("my-api")
    api.route("GET", "/secure", "functions/simple.handler", jwt_scopes=["read:users"])

    with pytest.raises(ValueError, match="jwt_scopes only works with JWT"):
        _ = api.resources


@pulumi.runtime.test
def test_jwt_scopes_with_lambda_authorizer_raises(pulumi_mocks):
    api = HttpApi("my-api")
    auth = api.add_lambda_authorizer(
        "my-auth",
        "functions/simple.handler",
        identity_sources=["$request.header.Authorization"],
    )
    api.route(
        "GET",
        "/secure",
        "functions/users.handler",
        auth=auth,
        jwt_scopes=["read:users"],
    )

    with pytest.raises(ValueError, match="jwt_scopes only works with JWT"):
        _ = api.resources


@pulumi.runtime.test
def test_jwt_scopes_reject_empty_scope(pulumi_mocks):
    api = HttpApi("my-api")
    auth = api.add_jwt_authorizer(
        "my-jwt",
        issuer="https://accounts.google.com",
        audiences=["my-client-id"],
    )
    api.route("GET", "/secure", "functions/simple.handler", auth=auth, jwt_scopes=[""])

    with pytest.raises(ValueError, match="non-empty"):
        _ = api.resources


# ---------------------------------------------------------------------------
# Cognito authorizer
# ---------------------------------------------------------------------------


@pulumi.runtime.test
def test_cognito_authorizer_creates_jwt_authorizer(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client("web")
    api = HttpApi("my-api")
    auth = api.add_cognito_authorizer(
        "my-cognito",
        user_pool=pool,
        audiences=[client],
    )
    api.route("GET", "/secure", "functions/simple.handler", auth=auth)
    _ = api.resources

    def check(_):
        authorizers = pulumi_mocks.created_http_api_authorizers()
        assert len(authorizers) == 1
        assert authorizers[0].typ == "aws:apigatewayv2/authorizer:Authorizer"
        assert authorizers[0].name == TP + "my-api-authorizer-my-cognito"
        assert authorizers[0].inputs["authorizerType"] == "JWT"
        assert authorizers[0].inputs["apiId"] == tid(TP + "my-api")
        assert authorizers[0].inputs["name"] == "my-cognito"
        assert authorizers[0].inputs["identitySources"] == ["$request.header.Authorization"]
        jwt_config = authorizers[0].inputs["jwtConfiguration"]
        assert jwt_config["issuer"] == (
            "https://cognito-idp.us-east-1.amazonaws.com/test-test-users-test-id"
        )
        assert len(jwt_config["audiences"]) == 1
        assert jwt_config["audiences"][0]  # non-empty client id

    when_http_api_ready(api, check)


def test_cognito_authorizer_rejects_client_from_different_pool(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    other_pool = UserPool("other-users", usernames=["email"])
    other_client = other_pool.add_client("web")
    api = HttpApi("my-api")

    with pytest.raises(ValueError, match="different UserPool"):
        api.add_cognito_authorizer(
            "my-cognito",
            user_pool=pool,
            audiences=[other_client],
        )


@pulumi.runtime.test
def test_cognito_authorizer_accepts_user_pool_arn(pulumi_mocks):
    api = HttpApi("my-api")
    auth = api.add_cognito_authorizer(
        "my-cognito",
        user_pool="arn:aws:cognito-idp:us-east-1:123:userpool/us-east-1_abc",
        audiences=["client-id"],
    )
    api.route("GET", "/secure", "functions/simple.handler", auth=auth)
    _ = api.resources

    def check(_):
        authorizers = pulumi_mocks.created_http_api_authorizers()
        jwt_config = authorizers[0].inputs["jwtConfiguration"]
        assert jwt_config["issuer"] == "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_abc"
        assert jwt_config["audiences"] == ["client-id"]

    when_http_api_ready(api, check)


def test_cognito_authorizer_rejects_client_with_user_pool_arn():
    api = HttpApi("my-api")

    with pytest.raises(TypeError):
        api.add_cognito_authorizer(
            "my-cognito",
            user_pool="arn:aws:cognito-idp:us-east-1:123:userpool/us-east-1_abc",
            audiences=[UserPool("users", usernames=["email"]).add_client("web")],
        )


def test_cognito_authorizer_rejects_malformed_user_pool_arn():
    api = HttpApi("my-api")

    with pytest.raises(ValueError, match="not-a-user-pool-arn"):
        api.add_cognito_authorizer(
            "my-cognito",
            user_pool="not-a-user-pool-arn",
            audiences=["client-id"],
        )


def test_cognito_authorizer_empty_audiences_raises(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    api = HttpApi("my-api")

    with pytest.raises(ValueError, match="audiences"):
        api.add_cognito_authorizer("my-cognito", user_pool=pool, audiences=[])


def test_cognito_authorizer_empty_raw_audience_raises(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    api = HttpApi("my-api")

    with pytest.raises(ValueError, match="audience values"):
        api.add_cognito_authorizer("my-cognito", user_pool=pool, audiences=[""])


def test_cognito_authorizer_keeps_identity_source_as_given(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client("web")
    api = HttpApi("my-api")

    auth = api.add_cognito_authorizer(
        "my-cognito",
        user_pool=pool,
        audiences=[client],
        identity_source="method.request.header.Authorization",
    )

    assert auth.identity_source == "method.request.header.Authorization"


# ---------------------------------------------------------------------------
# Duplicate authorizer names
# ---------------------------------------------------------------------------


def test_duplicate_authorizer_name_raises():
    api = HttpApi("my-api")
    api.add_jwt_authorizer("my-auth", issuer="https://example.com", audiences=["aud"])
    with pytest.raises(ValueError, match=r"[Dd]uplicate"):
        api.add_jwt_authorizer("my-auth", issuer="https://example.com", audiences=["aud"])


# ---------------------------------------------------------------------------
# default_auth
# ---------------------------------------------------------------------------


@pulumi.runtime.test
def test_default_auth_applies_to_routes(pulumi_mocks):
    api = HttpApi("my-api")
    auth = api.add_jwt_authorizer(
        "my-jwt",
        issuer="https://example.com",
        audiences=["aud"],
    )
    api.default_auth = auth
    api.route("GET", "/users", "functions/simple.handler")
    api.route("POST", "/users", "functions/users.handler")
    _ = api.resources

    def check(_):
        routes = pulumi_mocks.created_http_api_routes()
        assert len(routes) == 2
        assert {route.inputs["routeKey"] for route in routes} == {"GET /users", "POST /users"}
        assert {route.inputs["authorizationType"] for route in routes} == {"JWT"}

    when_http_api_ready(api, check)


@pulumi.runtime.test
def test_default_iam_auth_applies_to_routes(pulumi_mocks):
    api = HttpApi("my-api")
    api.default_auth = "IAM"
    api.route("GET", "/users", "functions/simple.handler")
    api.route("POST", "/orders", "functions/users.handler")
    _ = api.resources

    def check(_):
        routes = pulumi_mocks.created_http_api_routes()

        assert len(routes) == 2
        assert {route.inputs["routeKey"] for route in routes} == {"GET /users", "POST /orders"}
        assert {route.inputs["authorizationType"] for route in routes} == {"AWS_IAM"}
        assert all("authorizerId" not in route.inputs for route in routes)

    when_http_api_ready(api, check)


@pulumi.runtime.test
def test_route_auth_false_overrides_default(pulumi_mocks):
    api = HttpApi("my-api")
    auth = api.add_jwt_authorizer(
        "my-jwt",
        issuer="https://example.com",
        audiences=["aud"],
    )
    api.default_auth = auth
    api.route("GET", "/public", "functions/simple.handler", auth=False)
    api.route("GET", "/secure", "functions/users.handler")
    _ = api.resources

    def check(_):
        routes = pulumi_mocks.created_http_api_routes()
        public = next(r for r in routes if r.inputs["routeKey"] == "GET /public")
        secure = next(r for r in routes if r.inputs["routeKey"] == "GET /secure")
        assert public.inputs["authorizationType"] == "NONE"
        assert secure.inputs["authorizationType"] == "JWT"

    when_http_api_ready(api, check)


def test_default_auth_false_raises():
    api = HttpApi("my-api")
    with pytest.raises(ValueError, match="default_auth cannot be False"):
        api.default_auth = False


# ---------------------------------------------------------------------------
# Rejects after resources are created
# ---------------------------------------------------------------------------


@pulumi.runtime.test
def test_add_lambda_authorizer_rejects_after_resources_created(pulumi_mocks):
    api = HttpApi("my-api")
    api.route("GET", "/users", "functions/simple.handler")
    _ = api.resources

    with pytest.raises(RuntimeError, match="Cannot modify HttpApi 'my-api'"):
        api.add_lambda_authorizer(
            "auth",
            "functions/simple.handler",
            identity_sources=["$request.header.Authorization"],
        )


@pulumi.runtime.test
def test_add_cognito_authorizer_rejects_after_resources_created(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    api = HttpApi("my-api")
    api.route("GET", "/users", "functions/simple.handler")
    _ = api.resources

    with pytest.raises(RuntimeError, match="Cannot modify HttpApi 'my-api'"):
        api.add_cognito_authorizer("auth", user_pool=pool, audiences=["client-id"])


@pulumi.runtime.test
def test_default_auth_setter_rejects_after_resources_created(pulumi_mocks):
    api = HttpApi("my-api")
    api.route("GET", "/users", "functions/simple.handler")
    _ = api.resources

    with pytest.raises(RuntimeError, match="Cannot modify HttpApi 'my-api'"):
        api.default_auth = "IAM"
