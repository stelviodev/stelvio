"""Tests for HTTP API authorizers."""

import pulumi
import pytest

from stelvio.aws.cognito import UserPool
from stelvio.aws.http_api import HttpApi

from .conftest import when_http_api_ready

pytestmark = pytest.mark.usefixtures("project_cwd")


# ---------------------------------------------------------------------------
# Lambda authorizer
# ---------------------------------------------------------------------------


@pulumi.runtime.test
def test_lambda_authorizer_creates_authorizer_resource(pulumi_mocks):
    api = HttpApi("my-api")
    auth = api.add_lambda_authorizer(
        "my-auth",
        "functions/simple.handler",
        identity_sources="$request.header.Authorization",
    )
    api.route("GET", "/secure", "functions/users.handler", auth=auth)
    _ = api.resources

    def check(_):
        authorizers = pulumi_mocks.created_http_api_authorizers()
        assert len(authorizers) == 1
        assert authorizers[0].inputs["authorizerType"] == "REQUEST"
        assert authorizers[0].inputs["authorizerPayloadFormatVersion"] == "2.0"
        assert authorizers[0].inputs["enableSimpleResponses"] is True
        assert authorizers[0].inputs["identitySources"] == ["$request.header.Authorization"]

    when_http_api_ready(api, check)


@pulumi.runtime.test
def test_lambda_authorizer_creates_permission(pulumi_mocks):
    api = HttpApi("my-api")
    auth = api.add_lambda_authorizer(
        "my-auth",
        "functions/simple.handler",
        identity_sources="$request.header.Authorization",
    )
    api.route("GET", "/secure", "functions/users.handler", auth=auth)
    _ = api.resources

    def check(_):
        perms = pulumi_mocks.created_permissions()
        # Should have one permission for authorizer (source_arn ends in /authorizers/*)
        # and one for the route lambda (source_arn ends in /*/*)
        auth_perms = [p for p in perms if "/authorizers/*" in str(p.inputs.get("sourceArn", ""))]
        assert len(auth_perms) == 1
        assert auth_perms[0].inputs["action"] == "lambda:InvokeFunction"

    when_http_api_ready(api, check)


@pulumi.runtime.test
def test_lambda_authorizer_route_has_custom_auth_type(pulumi_mocks):
    api = HttpApi("my-api")
    auth = api.add_lambda_authorizer(
        "my-auth",
        "functions/simple.handler",
        identity_sources="$request.header.Authorization",
    )
    api.route("GET", "/secure", "functions/users.handler", auth=auth)
    _ = api.resources

    def check(_):
        routes = pulumi_mocks.created_http_api_routes()
        secure_route = next(r for r in routes if r.inputs["routeKey"] == "GET /secure")
        assert secure_route.inputs["authorizationType"] == "CUSTOM"
        assert secure_route.inputs.get("authorizerId") is not None

    when_http_api_ready(api, check)


@pulumi.runtime.test
def test_lambda_authorizer_ttl_zero(pulumi_mocks):
    api = HttpApi("my-api")
    auth = api.add_lambda_authorizer(
        "my-auth",
        "functions/simple.handler",
        identity_sources="$request.header.Authorization",
        ttl=0,
    )
    api.route("GET", "/secure", "functions/users.handler", auth=auth)
    _ = api.resources

    def check(_):
        authorizers = pulumi_mocks.created_http_api_authorizers()
        assert authorizers[0].inputs["authorizerResultTtlInSeconds"] == 0

    when_http_api_ready(api, check)


def test_lambda_authorizer_invalid_ttl_raises(pulumi_mocks):
    api = HttpApi("my-api")
    with pytest.raises(ValueError, match="ttl"):
        api.add_lambda_authorizer(
            "bad-auth",
            "functions/simple.handler",
            identity_sources="$request.header.Authorization",
            ttl=3601,
        )


def test_lambda_authorizer_empty_identity_sources_raises(pulumi_mocks):
    api = HttpApi("my-api")
    with pytest.raises(ValueError, match="identity_source"):
        api.add_lambda_authorizer(
            "bad-auth",
            "functions/simple.handler",
            identity_sources=[],
        )


def test_lambda_authorizer_v1_identity_source_rewritten(pulumi_mocks):
    """v1-style identity sources are automatically rewritten to v2 format."""
    import warnings

    api = HttpApi("my-api")
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        auth = api.add_lambda_authorizer(
            "my-auth",
            "functions/simple.handler",
            identity_sources="method.request.header.Authorization",
        )
        assert len(w) >= 1
        assert any(
            "v1" in str(warning.message).lower() or "rewritten" in str(warning.message).lower()
            for warning in w
        )

    assert auth.identity_sources == ["$request.header.Authorization"]


def test_lambda_authorizer_unsupported_v1_source_raises(pulumi_mocks):
    """Unsupported v1 identity sources raise ValueError."""
    api = HttpApi("my-api")
    with pytest.raises(ValueError, match="no v2 equivalent"):
        api.add_lambda_authorizer(
            "my-auth",
            "functions/simple.handler",
            identity_sources="method.request.multivalueheader.X-Custom",
        )


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
        assert authorizers[0].inputs["authorizerType"] == "JWT"
        jwt_config = authorizers[0].inputs["jwtConfiguration"]
        assert jwt_config["issuer"] == "https://accounts.google.com"
        assert "my-client-id" in jwt_config["audiences"]

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
        assert routes[0].inputs["authorizationType"] == "JWT"

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


def test_jwt_authorizer_empty_issuer_raises(pulumi_mocks):
    api = HttpApi("my-api")
    with pytest.raises(ValueError, match="issuer"):
        api.add_jwt_authorizer("jwt", issuer="", audiences=["aud"])


def test_jwt_authorizer_empty_audiences_raises(pulumi_mocks):
    api = HttpApi("my-api")
    with pytest.raises(ValueError, match="audiences"):
        api.add_jwt_authorizer("jwt", issuer="https://example.com", audiences=[])


def test_jwt_authorizer_v1_identity_source_rewritten(pulumi_mocks):
    api = HttpApi("my-api")

    with pytest.warns(DeprecationWarning, match="v1 format"):
        auth = api.add_jwt_authorizer(
            "jwt",
            issuer="https://example.com",
            audiences=["aud"],
            identity_source="method.request.header.Authorization",
        )

    assert auth.identity_source == "$request.header.Authorization"


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
        identity_sources="$request.header.Authorization",
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


def test_route_rejects_jwt_and_cognito_scopes(pulumi_mocks):
    api = HttpApi("my-api")
    auth = api.add_jwt_authorizer(
        "my-jwt",
        issuer="https://accounts.google.com",
        audiences=["my-client-id"],
    )

    with pytest.raises(ValueError, match=r"jwt_scopes.*cognito_scopes"):
        api.route(
            "GET",
            "/secure",
            "functions/simple.handler",
            auth=auth,
            jwt_scopes=["read:users"],
            cognito_scopes=["read:users"],
        )


def _add_routes_with_cognito_scopes(api, auth):
    for index in range(2):
        api.route(
            "GET",
            f"/secure-{index}",
            "functions/simple.handler",
            auth=auth,
            cognito_scopes=["read:users"],
        )


@pulumi.runtime.test
def test_cognito_scopes_alias_warns_once_per_call_site(pulumi_mocks):
    api = HttpApi("my-api")
    auth = api.add_jwt_authorizer(
        "my-jwt",
        issuer="https://accounts.google.com",
        audiences=["my-client-id"],
    )

    with pytest.warns(DeprecationWarning, match="cognito_scopes") as warnings:
        _add_routes_with_cognito_scopes(api, auth)

    assert len(warnings) == 1
    _ = api.resources

    def check(_):
        routes = pulumi_mocks.created_http_api_routes()
        assert all(r.inputs.get("authorizationScopes") == ["read:users"] for r in routes)

    when_http_api_ready(api, check)


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
        assert authorizers[0].inputs["authorizerType"] == "JWT"
        assert authorizers[0].inputs["identitySources"] == ["$request.header.Authorization"]
        jwt_config = authorizers[0].inputs["jwtConfiguration"]
        assert jwt_config["issuer"] == (
            "https://cognito-idp.us-east-1.amazonaws.com/test-test-users-test-id"
        )
        assert len(jwt_config["audiences"]) == 1

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


def test_cognito_authorizer_v1_identity_source_rewritten(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client("web")
    api = HttpApi("my-api")

    with pytest.warns(DeprecationWarning, match="v1 format"):
        auth = api.add_cognito_authorizer(
            "my-cognito",
            user_pool=pool,
            audiences=[client],
            identity_source="method.request.header.Authorization",
        )

    assert auth.identity_source == "$request.header.Authorization"


# ---------------------------------------------------------------------------
# Duplicate authorizer names
# ---------------------------------------------------------------------------


def test_duplicate_authorizer_name_raises(pulumi_mocks):
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
        assert all(r.inputs["authorizationType"] == "JWT" for r in routes)

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


def test_default_auth_false_raises(pulumi_mocks):
    api = HttpApi("my-api")
    with pytest.raises(ValueError, match="default_auth cannot be False"):
        api.default_auth = False
