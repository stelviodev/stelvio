"""Tests for HTTP API authorizers."""

import pulumi
from pytest import mark, raises

from stelvio.aws.api_gateway.http_api import HttpApi
from stelvio.aws.cognito import UserPool
from stelvio.aws.function import Function

from ...pulumi_mocks import ACCOUNT_ID, DEFAULT_REGION, R, tid, tn
from .conftest import HTTP_API_ID, LAMBDA_INVOKE_ARN_TEMPLATE, TP, when_http_api_ready

pytestmark = mark.usefixtures("project_cwd")

AUTHORIZER_PERMISSION_SOURCE_ARN = (
    f"arn:aws:execute-api:{DEFAULT_REGION}:{ACCOUNT_ID}:{HTTP_API_ID}/authorizers/*"
)


def assert_route(  # noqa: PLR0913
    mocks,
    *,
    route_name: str,
    route_key: str,
    function_name: str,
    authorization_type: str,
    authorizer_name: str | None = None,
    authorization_scopes: list[str] | None = None,
) -> None:
    inputs = {
        "target": f"integrations/{tid(TP + f'my-api-integration-{function_name}')}",
        "routeKey": route_key,
        "authorizationType": authorization_type,
        "apiId": HTTP_API_ID,
    }
    if authorizer_name is not None:
        inputs["authorizerId"] = tid(TP + f"my-api-authorizer-{authorizer_name}")
    if authorization_scopes is not None:
        inputs["authorizationScopes"] = authorization_scopes
    mocks.assert_res(route_name, R.HTTP_API_ROUTE, inputs)


def assert_jwt_authorizer(
    mocks,
    *,
    name: str,
    issuer: str,
    audiences: list[str],
    identity_source: str = "$request.header.Authorization",
) -> None:
    mocks.assert_res(
        f"my-api-authorizer-{name}",
        R.HTTP_API_AUTHORIZER,
        {
            "authorizerType": "JWT",
            "identitySources": [identity_source],
            "jwtConfiguration": {"audiences": audiences, "issuer": issuer},
            "name": name,
            "apiId": HTTP_API_ID,
        },
    )


def assert_http_api_graph_counts(  # noqa: PLR0913
    mocks,
    *,
    function_count: int,
    route_count: int,
    authorizer_count: int = 0,
    integration_count: int | None = None,
    extra: dict[R, int] | None = None,
) -> None:
    integration_count = function_count if integration_count is None else integration_count
    counts = {
        R.API_ACCOUNT: 2,
        R.HTTP_API: 1,
        R.ROLE: function_count + 1,
        R.LOG_GROUP: 1,
        R.ROLE_POLICY_ATTACHMENT: function_count,
        R.HTTP_API_STAGE: 1,
        R.FUNCTION: function_count,
        R.HTTP_API_INTEGRATION: integration_count,
        R.LAMBDA_PERMISSION: function_count,
        R.HTTP_API_ROUTE: route_count,
    }
    if authorizer_count:
        counts[R.HTTP_API_AUTHORIZER] = authorizer_count
    if extra is not None:
        counts |= extra
    mocks.assert_res_counts(counts)


def assert_lambda_authorizer_graph(
    mocks,
    *,
    simple_response: bool = True,
    ttl: int = 300,
    authorizer_timeout: int = 60,
) -> None:
    authorizer = mocks.assert_res(
        "my-api-authorizer-my-auth",
        R.HTTP_API_AUTHORIZER,
        {
            "authorizerResultTtlInSeconds": float(ttl),
            "authorizerType": "REQUEST",
            "authorizerUri": LAMBDA_INVOKE_ARN_TEMPLATE.format(
                function_name=tn(TP + "my-api-auth-my-auth")
            ),
            "authorizerPayloadFormatVersion": "2.0",
            "enableSimpleResponses": simple_response,
            "identitySources": ["$request.header.Authorization"],
            "name": "my-auth",
            "apiId": HTTP_API_ID,
        },
    )
    mocks.assert_res(
        "my-api-auth-my-auth",
        R.FUNCTION,
        {"handler": "simple.handler", "timeout": float(authorizer_timeout)},
        partial=True,
    )
    mocks.assert_res(
        "my-api-functions-users_handler",
        R.FUNCTION,
        {"handler": "users.handler"},
        partial=True,
    )
    mocks.assert_res(
        "my-api-auth-permission-my-auth",
        R.LAMBDA_PERMISSION,
        {
            "action": "lambda:InvokeFunction",
            "function": tn(TP + "my-api-auth-my-auth"),
            "principal": "apigateway.amazonaws.com",
            "sourceArn": AUTHORIZER_PERMISSION_SOURCE_ARN,
        },
    )
    assert_route(
        mocks,
        route_name="my-api-route-GET--secure",
        route_key="GET /secure",
        function_name="my-api-functions-users_handler",
        authorization_type="CUSTOM",
        authorizer_name=authorizer.inputs["name"],
    )
    assert_http_api_graph_counts(
        mocks,
        function_count=2,
        route_count=1,
        authorizer_count=1,
        integration_count=1,
    )


# ---------------------------------------------------------------------------
# Lambda authorizer
# ---------------------------------------------------------------------------


@mark.parametrize("simple_response", [True, False])
@pulumi.runtime.test
def test_lambda_authorizer_creates_resource_graph(pulumi_mocks, simple_response):
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
        assert_lambda_authorizer_graph(pulumi_mocks, simple_response=simple_response)

    when_http_api_ready(api, check)


@mark.parametrize("ttl", [0, 3600])
@pulumi.runtime.test
def test_lambda_authorizer_valid_ttl_boundaries(pulumi_mocks, ttl):
    api = HttpApi("my-api")
    auth = api.add_lambda_authorizer(
        "my-auth",
        "functions/simple.handler",
        identity_sources=["$request.header.Authorization"],
        ttl=ttl,
    )
    api.route("GET", "/secure", "functions/users.handler", auth=auth)
    _ = api.resources

    def check(_):
        assert_lambda_authorizer_graph(pulumi_mocks, ttl=ttl)

    when_http_api_ready(api, check)


@mark.parametrize("ttl", [-1, 3601])
def test_lambda_authorizer_invalid_ttl_raises(ttl):
    api = HttpApi("my-api")
    with raises(ValueError, match="ttl"):
        api.add_lambda_authorizer(
            "bad-auth",
            "functions/simple.handler",
            identity_sources=["$request.header.Authorization"],
            ttl=ttl,
        )


def test_lambda_authorizer_rejects_function_handler_with_opts():
    api = HttpApi("my-api")
    auth_function = Function("auth-fn", handler="functions/simple.handler")

    with raises(ValueError, match="Cannot combine a Function handler with function options"):
        api.add_lambda_authorizer(
            "my-auth",
            auth_function,
            identity_sources=["$request.header.Authorization"],
            memory=512,
        )


@pulumi.runtime.test
def test_lambda_authorizer_uses_supplied_function(pulumi_mocks):
    auth_function = Function("auth-fn", handler="functions/simple.handler")
    api = HttpApi("my-api")
    auth = api.add_lambda_authorizer(
        "my-auth",
        auth_function,
        identity_sources=["$request.header.Authorization"],
    )
    api.route("GET", "/secure", "functions/users.handler", auth=auth)
    _ = api.resources

    def check(_):
        mocks = pulumi_mocks
        mocks.assert_res(
            "my-api-authorizer-my-auth",
            R.HTTP_API_AUTHORIZER,
            {
                "authorizerResultTtlInSeconds": 300.0,
                "authorizerType": "REQUEST",
                "authorizerUri": LAMBDA_INVOKE_ARN_TEMPLATE.format(
                    function_name=tn(TP + "auth-fn")
                ),
                "authorizerPayloadFormatVersion": "2.0",
                "enableSimpleResponses": True,
                "identitySources": ["$request.header.Authorization"],
                "name": "my-auth",
                "apiId": HTTP_API_ID,
            },
        )
        mocks.assert_res(
            "my-api-auth-permission-my-auth",
            R.LAMBDA_PERMISSION,
            {
                "action": "lambda:InvokeFunction",
                "function": tn(TP + "auth-fn"),
                "principal": "apigateway.amazonaws.com",
                "sourceArn": AUTHORIZER_PERMISSION_SOURCE_ARN,
            },
        )
        assert_route(
            mocks,
            route_name="my-api-route-GET--secure",
            route_key="GET /secure",
            function_name="my-api-functions-users_handler",
            authorization_type="CUSTOM",
            authorizer_name="my-auth",
        )
        assert_http_api_graph_counts(
            mocks,
            function_count=2,
            route_count=1,
            authorizer_count=1,
            integration_count=1,
        )

    when_http_api_ready(api, check)


def test_lambda_authorizer_empty_identity_sources_raises():
    api = HttpApi("my-api")
    with raises(ValueError, match="identity_source"):
        api.add_lambda_authorizer(
            "bad-auth",
            "functions/simple.handler",
            identity_sources=[],
        )


def test_lambda_authorizer_requires_identity_sources_list():
    api = HttpApi("my-api")
    with raises(TypeError, match="identity_sources"):
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
        assert_lambda_authorizer_graph(pulumi_mocks, authorizer_timeout=10)

    when_http_api_ready(api, check)


# ---------------------------------------------------------------------------
# JWT authorizer
# ---------------------------------------------------------------------------


@pulumi.runtime.test
def test_jwt_authorizer_creates_resource_graph(pulumi_mocks):
    api = HttpApi("my-api")
    auth = api.add_jwt_authorizer(
        "my-jwt",
        issuer="https://accounts.google.com",
        audiences=["my-client-id"],
    )
    api.route("GET", "/secure", "functions/simple.handler", auth=auth)
    _ = api.resources

    def check(_):
        assert_jwt_authorizer(
            pulumi_mocks,
            name="my-jwt",
            issuer="https://accounts.google.com",
            audiences=["my-client-id"],
        )
        assert_route(
            pulumi_mocks,
            route_name="my-api-route-GET--secure",
            route_key="GET /secure",
            function_name="my-api-functions-simple_handler",
            authorization_type="JWT",
            authorizer_name="my-jwt",
        )
        assert_http_api_graph_counts(
            pulumi_mocks, function_count=1, route_count=1, authorizer_count=1
        )

    when_http_api_ready(api, check)


@pulumi.runtime.test
def test_route_with_iam_auth_uses_aws_iam_authorization(pulumi_mocks):
    api = HttpApi("my-api")
    api.route("GET", "/secure", "functions/simple.handler", auth="IAM")
    _ = api.resources

    def check(_):
        assert_route(
            pulumi_mocks,
            route_name="my-api-route-GET--secure",
            route_key="GET /secure",
            function_name="my-api-functions-simple_handler",
            authorization_type="AWS_IAM",
        )
        assert_http_api_graph_counts(pulumi_mocks, function_count=1, route_count=1)

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
        assert_jwt_authorizer(
            pulumi_mocks,
            name="my-jwt",
            issuer="https://accounts.google.com",
            audiences=["my-client-id"],
        )
        assert_route(
            pulumi_mocks,
            route_name="my-api-route-GET--secure",
            route_key="GET /secure",
            function_name="my-api-functions-simple_handler",
            authorization_type="JWT",
            authorizer_name="my-jwt",
            authorization_scopes=["read:users"],
        )
        assert_http_api_graph_counts(
            pulumi_mocks, function_count=1, route_count=1, authorizer_count=1
        )

    when_http_api_ready(api, check)


def test_jwt_authorizer_empty_issuer_raises():
    api = HttpApi("my-api")
    with raises(ValueError, match="issuer"):
        api.add_jwt_authorizer("jwt", issuer="", audiences=["aud"])


def test_jwt_authorizer_empty_audiences_raises():
    api = HttpApi("my-api")
    with raises(ValueError, match="audiences"):
        api.add_jwt_authorizer("jwt", issuer="https://example.com", audiences=[])


@pulumi.runtime.test
def test_jwt_scopes_with_no_auth_raises(pulumi_mocks):
    api = HttpApi("my-api")
    api.route("GET", "/secure", "functions/simple.handler", jwt_scopes=["read:users"])

    with raises(ValueError, match="jwt_scopes only works with JWT"):
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

    with raises(ValueError, match="jwt_scopes only works with JWT"):
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

    with raises(ValueError, match="non-empty"):
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
        assert_jwt_authorizer(
            pulumi_mocks,
            name="my-cognito",
            issuer="https://cognito-idp.us-east-1.amazonaws.com/" + tid(TP + "users"),
            audiences=[tid(TP + "users-web")],
        )
        assert_route(
            pulumi_mocks,
            route_name="my-api-route-GET--secure",
            route_key="GET /secure",
            function_name="my-api-functions-simple_handler",
            authorization_type="JWT",
            authorizer_name="my-cognito",
        )
        assert_http_api_graph_counts(
            pulumi_mocks,
            function_count=1,
            route_count=1,
            authorizer_count=1,
            extra={R.USER_POOL: 1, R.USER_POOL_CLIENT: 1},
        )

    when_http_api_ready(api, check)


@pulumi.runtime.test
def test_cognito_authorizer_issuer_uses_resolved_region(pulumi_mocks, no_region_context):
    """Issuer carries the chain-resolved region when config has none.

    Scenario (the default new-user setup): no `region=` in @app.config, but the AWS
    chain resolves one (here AWS_REGION=eu-central-1 via the fixture) — so Stelvio
    deploys fine. The original bug interpolated the raw config value (None) into
    "https://cognito-idp.None.amazonaws.com/..." — deployed clean, rejected every
    token at runtime.
    """
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
        assert_jwt_authorizer(
            pulumi_mocks,
            name="my-cognito",
            issuer="https://cognito-idp.eu-central-1.amazonaws.com/" + tid(TP + "users"),
            audiences=[tid(TP + "users-web")],
        )

    when_http_api_ready(api, check)


def test_cognito_authorizer_rejects_client_from_different_pool(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    other_pool = UserPool("other-users", usernames=["email"])
    other_client = other_pool.add_client("web")
    api = HttpApi("my-api")

    with raises(ValueError, match="different UserPool"):
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
        assert_jwt_authorizer(
            pulumi_mocks,
            name="my-cognito",
            issuer="https://cognito-idp.us-east-1.amazonaws.com/us-east-1_abc",
            audiences=["client-id"],
        )
        assert_route(
            pulumi_mocks,
            route_name="my-api-route-GET--secure",
            route_key="GET /secure",
            function_name="my-api-functions-simple_handler",
            authorization_type="JWT",
            authorizer_name="my-cognito",
        )
        assert_http_api_graph_counts(
            pulumi_mocks, function_count=1, route_count=1, authorizer_count=1
        )

    when_http_api_ready(api, check)


def test_cognito_authorizer_rejects_client_with_user_pool_arn():
    api = HttpApi("my-api")

    with raises(TypeError, match="UserPoolClient audiences require"):
        api.add_cognito_authorizer(
            "my-cognito",
            user_pool="arn:aws:cognito-idp:us-east-1:123:userpool/us-east-1_abc",
            audiences=[UserPool("users", usernames=["email"]).add_client("web")],
        )


@mark.parametrize(
    "bad_arn",
    [
        "not-an-arn",
        "arn:aws:s3:::bucket",
        "arn:aws:cognito-idp:us-east-1:123:something/else",
        "arn:aws:cognito-idp::123:userpool/abc",
        "arn:aws:cognito-idp:us-east-1:123:userpool/",
    ],
)
def test_cognito_authorizer_rejects_malformed_user_pool_arn(bad_arn):
    api = HttpApi("my-api")

    with raises(ValueError, match="user_pool ARN is invalid"):
        api.add_cognito_authorizer("my-cognito", user_pool=bad_arn, audiences=["client-id"])


def test_cognito_authorizer_empty_audiences_raises(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    api = HttpApi("my-api")

    with raises(ValueError, match="audiences"):
        api.add_cognito_authorizer("my-cognito", user_pool=pool, audiences=[])


def test_cognito_authorizer_empty_raw_audience_raises(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    api = HttpApi("my-api")

    with raises(ValueError, match="audience values"):
        api.add_cognito_authorizer("my-cognito", user_pool=pool, audiences=[""])


@mark.parametrize("kind", ["jwt", "cognito"])
@pulumi.runtime.test
def test_jwt_authorizers_deploy_custom_identity_source(pulumi_mocks, kind):
    api = HttpApi("my-api")
    if kind == "jwt":
        auth = api.add_jwt_authorizer(
            "my-auth",
            issuer="https://example.com",
            audiences=["aud"],
            identity_source="method.request.header.Authorization",
        )
    else:
        auth = api.add_cognito_authorizer(
            "my-auth",
            user_pool="arn:aws:cognito-idp:us-east-1:123:userpool/us-east-1_abc",
            audiences=["client-id"],
            identity_source="method.request.header.Authorization",
        )
    api.route("GET", "/secure", "functions/simple.handler", auth=auth)
    _ = api.resources

    def check(_):
        issuer = (
            "https://example.com"
            if kind == "jwt"
            else "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_abc"
        )
        audiences = ["aud"] if kind == "jwt" else ["client-id"]
        assert_jwt_authorizer(
            pulumi_mocks,
            name="my-auth",
            issuer=issuer,
            audiences=audiences,
            identity_source="method.request.header.Authorization",
        )
        assert_route(
            pulumi_mocks,
            route_name="my-api-route-GET--secure",
            route_key="GET /secure",
            function_name="my-api-functions-simple_handler",
            authorization_type="JWT",
            authorizer_name="my-auth",
        )
        assert_http_api_graph_counts(
            pulumi_mocks, function_count=1, route_count=1, authorizer_count=1
        )

    when_http_api_ready(api, check)


# ---------------------------------------------------------------------------
# Duplicate authorizer names
# ---------------------------------------------------------------------------


@mark.parametrize(
    "add_authorizer",
    [
        lambda api: api.add_jwt_authorizer(
            "my-auth", issuer="https://example.com", audiences=["aud"]
        ),
        lambda api: api.add_lambda_authorizer(
            "my-auth",
            "functions/simple.handler",
            identity_sources=["$request.header.Authorization"],
        ),
        lambda api: api.add_cognito_authorizer(
            "my-auth",
            user_pool="arn:aws:cognito-idp:us-east-1:123:userpool/us-east-1_abc",
            audiences=["client-id"],
        ),
    ],
    ids=["jwt", "lambda", "cognito"],
)
def test_duplicate_authorizer_name_raises(add_authorizer):
    api = HttpApi("my-api")
    add_authorizer(api)
    with raises(ValueError, match=r"[Dd]uplicate"):
        add_authorizer(api)


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
        assert_jwt_authorizer(
            pulumi_mocks,
            name="my-jwt",
            issuer="https://example.com",
            audiences=["aud"],
        )
        assert_route(
            pulumi_mocks,
            route_name="my-api-route-GET--users",
            route_key="GET /users",
            function_name="my-api-functions-simple_handler",
            authorization_type="JWT",
            authorizer_name="my-jwt",
        )
        assert_route(
            pulumi_mocks,
            route_name="my-api-route-POST--users",
            route_key="POST /users",
            function_name="my-api-functions-users_handler",
            authorization_type="JWT",
            authorizer_name="my-jwt",
        )
        assert_http_api_graph_counts(
            pulumi_mocks, function_count=2, route_count=2, authorizer_count=1
        )

    when_http_api_ready(api, check)


@pulumi.runtime.test
def test_default_iam_auth_applies_to_routes(pulumi_mocks):
    api = HttpApi("my-api")
    api.default_auth = "IAM"
    api.route("GET", "/users", "functions/simple.handler")
    api.route("POST", "/orders", "functions/users.handler")
    _ = api.resources

    def check(_):
        assert_route(
            pulumi_mocks,
            route_name="my-api-route-GET--users",
            route_key="GET /users",
            function_name="my-api-functions-simple_handler",
            authorization_type="AWS_IAM",
        )
        assert_route(
            pulumi_mocks,
            route_name="my-api-route-POST--orders",
            route_key="POST /orders",
            function_name="my-api-functions-users_handler",
            authorization_type="AWS_IAM",
        )
        assert_http_api_graph_counts(pulumi_mocks, function_count=2, route_count=2)

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
        assert_jwt_authorizer(
            pulumi_mocks,
            name="my-jwt",
            issuer="https://example.com",
            audiences=["aud"],
        )
        assert_route(
            pulumi_mocks,
            route_name="my-api-route-GET--public",
            route_key="GET /public",
            function_name="my-api-functions-simple_handler",
            authorization_type="NONE",
        )
        assert_route(
            pulumi_mocks,
            route_name="my-api-route-GET--secure",
            route_key="GET /secure",
            function_name="my-api-functions-users_handler",
            authorization_type="JWT",
            authorizer_name="my-jwt",
        )
        assert_http_api_graph_counts(
            pulumi_mocks, function_count=2, route_count=2, authorizer_count=1
        )

    when_http_api_ready(api, check)


def test_default_auth_false_raises():
    api = HttpApi("my-api")
    with raises(ValueError, match="default_auth cannot be False"):
        api.default_auth = False


# ---------------------------------------------------------------------------
# Rejects after resources are created
# ---------------------------------------------------------------------------


@pulumi.runtime.test
def test_add_lambda_authorizer_rejects_after_resources_created(pulumi_mocks):
    api = HttpApi("my-api")
    api.route("GET", "/users", "functions/simple.handler")
    _ = api.resources

    with raises(RuntimeError, match="Cannot modify HttpApi 'my-api'"):
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

    with raises(RuntimeError, match="Cannot modify HttpApi 'my-api'"):
        api.add_cognito_authorizer("auth", user_pool=pool, audiences=["client-id"])


@pulumi.runtime.test
def test_default_auth_setter_rejects_after_resources_created(pulumi_mocks):
    api = HttpApi("my-api")
    api.route("GET", "/users", "functions/simple.handler")
    _ = api.resources

    with raises(RuntimeError, match="Cannot modify HttpApi 'my-api'"):
        api.default_auth = "IAM"
