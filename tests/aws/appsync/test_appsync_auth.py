"""AppSync auth mode tests — all 5 auth modes, multi-auth, API key creation."""

from datetime import UTC, datetime, timedelta

import pulumi
import pytest

from stelvio.aws.appsync import (
    ApiKeyAuth,
    AppSync,
    CognitoAuth,
    LambdaAuth,
    OidcAuth,
)
from stelvio.aws.appsync.config import validate_auth_config
from stelvio.aws.appsync.constants import (
    AUTH_TYPE_API_KEY,
    AUTH_TYPE_COGNITO,
    AUTH_TYPE_IAM,
    AUTH_TYPE_LAMBDA,
    AUTH_TYPE_OIDC,
)
from stelvio.aws.cognito.user_pool import UserPool
from stelvio.aws.function import Function, FunctionConfig

from ..pulumi_mocks import ACCOUNT_ID, DEFAULT_REGION, R, tid, tn
from .conftest import (
    API_ID,
    COGNITO_USER_POOL_ID,
    INLINE_SCHEMA,
    TP,
    deploy_all,
    make_api,
)


def authorizer_uri(function: str) -> str:
    return f"arn:aws:lambda:{DEFAULT_REGION}:{ACCOUNT_ID}:function:{tn(TP + function)}"


def test_api_key_auth(pulumi_mocks, project_cwd):
    make_api(auth=ApiKeyAuth(expires=30))

    deploy_all()

    pulumi_mocks.assert_res(
        "myapi", R.GRAPHQL_API, {"authenticationType": AUTH_TYPE_API_KEY}, partial=True
    )
    api_key = pulumi_mocks.assert_res("myapi-api-key", R.APPSYNC_API_KEY)
    assert {k: v for k, v in api_key.inputs.items() if k != "expires"} == {
        "apiId": API_ID,
        "description": "Managed by Pulumi",  # pulumi_aws default, not a Stelvio input
    }
    pulumi_mocks.assert_res_counts(
        {R.GRAPHQL_API: 1, R.APPSYNC_DATA_SOURCE: 1, R.APPSYNC_API_KEY: 1}
    )
    expires_str = api_key.inputs["expires"]
    expires_dt = datetime.strptime(expires_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    now = datetime.now(tz=UTC)
    assert expires_dt > now + timedelta(days=29)
    assert expires_dt < now + timedelta(days=31)


@pytest.mark.parametrize(
    ("auth_config", "additional_auth", "expected_api_key"),
    [
        (ApiKeyAuth(), None, f"da2-test-api-key-{TP}myapi-api-key-test-id"),
        ("iam", [ApiKeyAuth()], f"da2-test-api-key-{TP}myapi-api-key-test-id"),
        ("iam", None, None),
    ],
    ids=["default-api-key", "additional-api-key", "no-api-key"],
)
@pulumi.runtime.test
def test_api_key_property(
    auth_config, additional_auth, expected_api_key, pulumi_mocks, project_cwd
):
    api = make_api(auth=auth_config, additional_auth=additional_auth)

    if expected_api_key is None:
        assert api.api_key is None
        return

    def check_key(key):
        assert key is not None
        assert key == expected_api_key

    api.api_key.apply(check_key)


@pytest.mark.parametrize(
    ("auth_config", "expected_auth_type", "config_key", "expected_config"),
    [
        ("iam", AUTH_TYPE_IAM, None, None),
        (
            CognitoAuth(user_pool_id=COGNITO_USER_POOL_ID),
            AUTH_TYPE_COGNITO,
            "userPoolConfig",
            {"defaultAction": "ALLOW", "userPoolId": COGNITO_USER_POOL_ID},
        ),
        (
            CognitoAuth(
                user_pool_id=COGNITO_USER_POOL_ID,
                region="eu-west-1",
                app_id_client_regex="^my-app.*",
            ),
            AUTH_TYPE_COGNITO,
            "userPoolConfig",
            {
                "defaultAction": "ALLOW",
                "userPoolId": COGNITO_USER_POOL_ID,
                "awsRegion": "eu-west-1",
                "appIdClientRegex": "^my-app.*",
            },
        ),
        (
            OidcAuth(issuer="https://auth.example.com"),
            AUTH_TYPE_OIDC,
            "openidConnectConfig",
            {"issuer": "https://auth.example.com"},
        ),
        (
            OidcAuth(
                issuer="https://auth.example.com",
                client_id="my-client",
                auth_ttl=3600,
                iat_ttl=7200,
            ),
            AUTH_TYPE_OIDC,
            "openidConnectConfig",
            {
                "issuer": "https://auth.example.com",
                "clientId": "my-client",
                "authTtl": 3600,
                "iatTtl": 7200,
            },
        ),
    ],
    ids=["iam", "cognito", "cognito-opts", "oidc", "oidc-opts"],
)
def test_auth_mode_creates_correct_api(  # noqa: PLR0913
    auth_config, expected_auth_type, config_key, expected_config, pulumi_mocks, project_cwd
):
    make_api(auth=auth_config)

    deploy_all()

    expected = {"authenticationType": expected_auth_type}
    if config_key:
        expected[config_key] = expected_config
    pulumi_mocks.assert_res("myapi", R.GRAPHQL_API, expected, partial=True)


def test_cognito_auth_accepts_user_pool_component(pulumi_mocks, project_cwd):
    pool = UserPool("auth-pool", usernames=["email"])
    make_api(auth=CognitoAuth(user_pool_id=pool))

    deploy_all()

    pulumi_mocks.assert_res(
        "myapi",
        R.GRAPHQL_API,
        {
            "authenticationType": AUTH_TYPE_COGNITO,
            "userPoolConfig": {"defaultAction": "ALLOW", "userPoolId": tid(f"{TP}auth-pool")},
        },
        partial=True,
    )


def test_cognito_auth_user_pool_component_with_options(pulumi_mocks, project_cwd):
    """UserPool component works alongside region and app_id_client_regex options."""
    pool = UserPool("auth-pool", usernames=["email"])
    make_api(auth=CognitoAuth(user_pool_id=pool, region="eu-west-1", app_id_client_regex="^web.*"))

    deploy_all()

    pulumi_mocks.assert_res(
        "myapi",
        R.GRAPHQL_API,
        {
            "authenticationType": AUTH_TYPE_COGNITO,
            "userPoolConfig": {
                "defaultAction": "ALLOW",
                "userPoolId": tid(f"{TP}auth-pool"),
                "awsRegion": "eu-west-1",
                "appIdClientRegex": "^web.*",
            },
        },
        partial=True,
    )


def test_cognito_auth_empty_string_still_rejected():
    """Empty string validation still works after adding UserPool support."""
    with pytest.raises(ValueError, match="user_pool_id cannot be empty"):
        CognitoAuth(user_pool_id="")


def test_cognito_auth_invalid_type_rejected():
    """Non-string, non-UserPool types are rejected at construction time."""
    with pytest.raises(TypeError, match="user_pool_id must be a UserPool or string"):
        CognitoAuth(user_pool_id=123)

    with pytest.raises(TypeError, match="user_pool_id must be a UserPool or string"):
        CognitoAuth(user_pool_id=None)


@pytest.mark.parametrize(
    ("auth_config", "expected_fn_inputs", "expected_auth_config"),
    [
        (LambdaAuth(handler="functions/simple.handler"), {}, {}),
        (
            LambdaAuth(handler="functions/simple.handler", memory=256, timeout=10),
            {"memorySize": 256, "timeout": 10},
            {},
        ),
        (
            LambdaAuth(handler=FunctionConfig(handler="functions/simple.handler", memory=512)),
            {"memorySize": 512},
            {},
        ),
        (
            LambdaAuth(handler="functions/simple.handler", result_ttl=300),
            {},
            {"authorizerResultTtlInSeconds": 300},
        ),
        (
            LambdaAuth(
                handler="functions/simple.handler",
                identity_validation_expression=r"^Bearer\\s.+$",
            ),
            {},
            {"identityValidationExpression": r"^Bearer\\s.+$"},
        ),
    ],
    ids=["basic", "fn-opts", "function-config", "result-ttl", "identity-validation"],
)
def test_lambda_auth_creates_function(
    auth_config, expected_auth_config, expected_fn_inputs, pulumi_mocks, project_cwd
):
    make_api(auth=auth_config)

    deploy_all()

    pulumi_mocks.assert_res(
        "myapi",
        R.GRAPHQL_API,
        {
            "authenticationType": AUTH_TYPE_LAMBDA,
            "lambdaAuthorizerConfig": {
                "authorizerUri": authorizer_uri("myapi-authorizer"),
                **expected_auth_config,
            },
        },
        partial=True,
    )
    pulumi_mocks.assert_res("myapi-authorizer", R.FUNCTION, expected_fn_inputs, partial=True)
    pulumi_mocks.assert_res(
        "myapi-auth-perm",
        R.LAMBDA_PERMISSION,
        {
            "action": "lambda:InvokeFunction",
            "function": tn(f"{TP}myapi-authorizer"),
            "principal": "appsync.amazonaws.com",
            "sourceArn": f"arn:aws:appsync:{DEFAULT_REGION}:{ACCOUNT_ID}:apis/appsync-{API_ID}",
        },
    )
    pulumi_mocks.assert_res_counts(
        {
            R.GRAPHQL_API: 1,
            R.APPSYNC_DATA_SOURCE: 1,
            R.FUNCTION: 1,
            R.ROLE: 1,
            R.ROLE_POLICY_ATTACHMENT: 1,
            R.LAMBDA_PERMISSION: 1,
        }
    )


def test_lambda_auth_with_existing_function_handler(pulumi_mocks, project_cwd):
    auth_fn = Function("existing-auth-fn", handler="functions/simple.handler")
    make_api(auth=LambdaAuth(handler=auth_fn))

    deploy_all()

    pulumi_mocks.assert_res(
        "myapi",
        R.GRAPHQL_API,
        {
            "authenticationType": AUTH_TYPE_LAMBDA,
            "lambdaAuthorizerConfig": {"authorizerUri": authorizer_uri("existing-auth-fn")},
        },
        partial=True,
    )
    pulumi_mocks.assert_res("existing-auth-fn", R.FUNCTION)
    assert len(pulumi_mocks.created(R.FUNCTION)) == 1


@pytest.mark.parametrize(
    ("auth_kwargs", "fn_name"),
    [
        ({"auth": LambdaAuth(handler="functions/simple.handler")}, "myapi-authorizer"),
        (
            {"additional_auth": [LambdaAuth(handler="functions/simple.handler")]},
            "myapi-authorizer-additional-0",
        ),
    ],
    ids=["auth", "additional-auth"],
)
def test_auth_functions_customize_reaches_built_authorizer(
    auth_kwargs, fn_name, pulumi_mocks, project_cwd
):
    make_api(**auth_kwargs, customize={"auth_functions": {"function": {"memory_size": 1024}}})

    deploy_all()

    pulumi_mocks.assert_res(fn_name, R.FUNCTION, {"memorySize": 1024}, partial=True)


@pytest.mark.parametrize(
    ("auth", "extra", "expected_type", "providers", "api_keys", "auth_fns"),
    [
        (
            CognitoAuth(user_pool_id=COGNITO_USER_POOL_ID),
            ["iam", ApiKeyAuth()],
            AUTH_TYPE_COGNITO,
            [{"authenticationType": AUTH_TYPE_IAM}, {"authenticationType": AUTH_TYPE_API_KEY}],
            1,
            0,
        ),
        (
            "iam",
            [LambdaAuth(handler="functions/simple.handler")],
            AUTH_TYPE_IAM,
            [
                {
                    "authenticationType": AUTH_TYPE_LAMBDA,
                    "lambdaAuthorizerConfig": {
                        "authorizerUri": authorizer_uri("myapi-authorizer-additional-0")
                    },
                }
            ],
            0,
            1,
        ),
        (
            "iam",
            [CognitoAuth(user_pool_id=COGNITO_USER_POOL_ID)],
            AUTH_TYPE_IAM,
            [
                {
                    "authenticationType": AUTH_TYPE_COGNITO,
                    "userPoolConfig": {"userPoolId": COGNITO_USER_POOL_ID},
                }
            ],
            0,
            0,
        ),
        (
            "iam",
            [OidcAuth(issuer="https://auth.example.com", client_id="my-app")],
            AUTH_TYPE_IAM,
            [
                {
                    "authenticationType": AUTH_TYPE_OIDC,
                    "openidConnectConfig": {
                        "issuer": "https://auth.example.com",
                        "clientId": "my-app",
                    },
                }
            ],
            0,
            0,
        ),
    ],
    ids=["iam+apikey", "iam+lambda", "iam+cognito", "iam+oidc"],
)
def test_additional_auth_configuration(  # noqa: PLR0913
    auth,
    extra,
    expected_type,
    providers,
    api_keys,
    auth_fns,
    pulumi_mocks,
    project_cwd,
):
    make_api(auth=auth, additional_auth=extra)

    deploy_all()

    pulumi_mocks.assert_res(
        "myapi",
        R.GRAPHQL_API,
        {"authenticationType": expected_type, "additionalAuthenticationProviders": providers},
        partial=True,
    )
    assert len(pulumi_mocks.created(R.APPSYNC_API_KEY)) == api_keys
    assert len(pulumi_mocks.created(R.FUNCTION)) == auth_fns


# --- Auth config validation ---


@pytest.mark.parametrize(
    ("constructor", "error_type", "match"),
    [
        (
            lambda: ApiKeyAuth(expires=0),
            ValueError,
            "expires must be an integer between 1 and 365",
        ),
        (
            lambda: ApiKeyAuth(expires=366),
            ValueError,
            "expires must be an integer between 1 and 365",
        ),
        (
            lambda: ApiKeyAuth(expires=-1),
            ValueError,
            "expires must be an integer between 1 and 365",
        ),
        (lambda: CognitoAuth(user_pool_id=""), ValueError, "user_pool_id cannot be empty"),
        (lambda: OidcAuth(issuer=""), ValueError, "issuer cannot be empty"),
        (lambda: LambdaAuth(handler=""), ValueError, "handler cannot be empty"),
        (
            lambda: LambdaAuth(
                handler=FunctionConfig(handler="functions/simple.handler"),
                memory=256,
            ),
            ValueError,
            "cannot combine complete handler configuration",
        ),
        (
            lambda: LambdaAuth(
                handler=Function("auth-fn", handler="functions/simple.handler"), memory=256
            ),
            ValueError,
            "Cannot combine a Function handler with function options",
        ),
        (lambda: validate_auth_config(42), TypeError, "Invalid auth config"),
        (lambda: validate_auth_config("invalid"), TypeError, "Invalid auth config"),
    ],
    ids=[
        "apikey-expires-zero",
        "apikey-expires-high",
        "apikey-expires-negative",
        "cognito-empty-pool",
        "oidc-empty-issuer",
        "lambda-empty-handler",
        "lambda-opts-with-config",
        "lambda-opts-with-function",
        "invalid-type-int",
        "invalid-type-str",
    ],
)
def test_auth_config_validation(constructor, error_type, match):
    with pytest.raises(error_type, match=match):
        constructor()


# --- Duplicate auth modes ---


@pytest.mark.parametrize(
    ("auth", "additional_auth"),
    [
        (ApiKeyAuth(), [ApiKeyAuth(expires=90)]),
        ("iam", [ApiKeyAuth(), ApiKeyAuth(expires=90)]),
        ("iam", ["iam"]),
    ],
    ids=["default-matches-additional", "duplicate-in-additional", "duplicate-iam"],
)
def test_duplicate_auth_mode_rejected(auth, additional_auth, project_cwd):
    with pytest.raises(ValueError, match="Duplicate authentication mode"):
        AppSync("myapi", schema=INLINE_SCHEMA, auth=auth, additional_auth=additional_auth)
