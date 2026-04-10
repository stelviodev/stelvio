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

from .conftest import (
    COGNITO_USER_POOL_ID,
    INLINE_SCHEMA,
    TP,
    assert_graphql_api_inputs,
    make_api,
    when_appsync_ready,
)


@pulumi.runtime.test
def test_api_key_auth(pulumi_mocks, project_cwd):
    api = make_api(auth=ApiKeyAuth(expires=30))

    def check_resources(_):
        assert_graphql_api_inputs(pulumi_mocks, f"{TP}myapi", authenticationType=AUTH_TYPE_API_KEY)

        api_keys = pulumi_mocks.created_appsync_api_keys()
        assert len(api_keys) == 1
        assert api_keys[0].typ == "aws:appsync/apiKey:ApiKey"
        expires_str = api_keys[0].inputs["expires"]
        expires_dt = datetime.strptime(expires_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        now = datetime.now(tz=UTC)
        assert expires_dt > now + timedelta(days=29)
        assert expires_dt < now + timedelta(days=31)

    when_appsync_ready(api, check_resources)


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
            {"userPoolId": COGNITO_USER_POOL_ID},
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
@pulumi.runtime.test
def test_auth_mode_creates_correct_api(  # noqa: PLR0913
    auth_config, expected_auth_type, config_key, expected_config, pulumi_mocks, project_cwd
):
    api = make_api(auth=auth_config)

    def check_resources(_):
        inputs = assert_graphql_api_inputs(
            pulumi_mocks, f"{TP}myapi", authenticationType=expected_auth_type
        )
        if config_key:
            config = inputs[config_key]
            for key, value in expected_config.items():
                assert config[key] == value

    when_appsync_ready(api, check_resources)


@pulumi.runtime.test
def test_cognito_auth_accepts_user_pool_component(pulumi_mocks, project_cwd):
    pool = UserPool("auth-pool", usernames=["email"])
    api = make_api(auth=CognitoAuth(user_pool_id=pool))

    def check_resources(_):
        inputs = assert_graphql_api_inputs(
            pulumi_mocks, f"{TP}myapi", authenticationType=AUTH_TYPE_COGNITO
        )
        config = inputs["userPoolConfig"]
        assert config["userPoolId"] == "test-test-auth-pool-test-id"

    when_appsync_ready(api, check_resources)


@pulumi.runtime.test
def test_cognito_auth_user_pool_component_with_options(pulumi_mocks, project_cwd):
    """UserPool component works alongside region and app_id_client_regex options."""
    pool = UserPool("auth-pool", usernames=["email"])
    api = make_api(
        auth=CognitoAuth(user_pool_id=pool, region="eu-west-1", app_id_client_regex="^web.*")
    )

    def check_resources(_):
        inputs = assert_graphql_api_inputs(
            pulumi_mocks, f"{TP}myapi", authenticationType=AUTH_TYPE_COGNITO
        )
        config = inputs["userPoolConfig"]
        assert config["userPoolId"] == "test-test-auth-pool-test-id"
        assert config["awsRegion"] == "eu-west-1"
        assert config["appIdClientRegex"] == "^web.*"

    when_appsync_ready(api, check_resources)


@pulumi.runtime.test
def test_cognito_auth_string_pool_id_still_works(pulumi_mocks, project_cwd):
    """Existing string pool ID usage continues to work after the UserPool change."""
    api = make_api(auth=CognitoAuth(user_pool_id=COGNITO_USER_POOL_ID))

    def check_resources(_):
        inputs = assert_graphql_api_inputs(
            pulumi_mocks, f"{TP}myapi", authenticationType=AUTH_TYPE_COGNITO
        )
        config = inputs["userPoolConfig"]
        assert config["userPoolId"] == COGNITO_USER_POOL_ID

    when_appsync_ready(api, check_resources)


def test_cognito_auth_empty_string_still_rejected():
    """Empty string validation still works after adding UserPool support."""
    with pytest.raises(ValueError, match="user_pool_id cannot be empty"):
        CognitoAuth(user_pool_id="")


@pytest.mark.parametrize(
    ("auth_config", "expected_fn_inputs", "expected_auth_config"),
    [
        (LambdaAuth(handler="functions/simple.handler"), {}, None),
        (
            LambdaAuth(handler="functions/simple.handler", memory=256, timeout=10),
            {"memorySize": 256, "timeout": 10},
            None,
        ),
        (
            LambdaAuth(handler=FunctionConfig(handler="functions/simple.handler", memory=512)),
            {"memorySize": 512},
            None,
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
@pulumi.runtime.test
def test_lambda_auth_creates_function(
    auth_config, expected_auth_config, expected_fn_inputs, pulumi_mocks, project_cwd
):
    api = make_api(auth=auth_config)

    def check_resources(_):
        inputs = assert_graphql_api_inputs(
            pulumi_mocks, f"{TP}myapi", authenticationType=AUTH_TYPE_LAMBDA
        )
        assert "lambdaAuthorizerConfig" in inputs

        if expected_auth_config:
            for key, value in expected_auth_config.items():
                assert inputs["lambdaAuthorizerConfig"][key] == value

        fns = pulumi_mocks.created_functions(f"{TP}myapi-authorizer")
        assert len(fns) == 1
        assert fns[0].typ == "aws:lambda/function:Function"
        for key, value in expected_fn_inputs.items():
            assert fns[0].inputs[key] == value

        perms = [p for p in pulumi_mocks.created_permissions() if "auth-perm" in p.name]
        assert len(perms) == 1
        assert perms[0].inputs["action"] == "lambda:InvokeFunction"
        assert perms[0].inputs["principal"] == "appsync.amazonaws.com"

    when_appsync_ready(api, check_resources)


@pulumi.runtime.test
def test_lambda_auth_with_existing_function_handler(pulumi_mocks, project_cwd):
    auth_fn = Function("existing-auth-fn", handler="functions/simple.handler")
    api = make_api(auth=LambdaAuth(handler=auth_fn))

    def check_resources(_):
        inputs = assert_graphql_api_inputs(
            pulumi_mocks, f"{TP}myapi", authenticationType=AUTH_TYPE_LAMBDA
        )
        assert "lambdaAuthorizerConfig" in inputs

        existing_fn = pulumi_mocks.assert_function_created(f"{TP}existing-auth-fn")
        assert existing_fn.typ == "aws:lambda/function:Function"
        assert len(pulumi_mocks.created_functions(f"{TP}myapi-authorizer")) == 0

    when_appsync_ready(api, check_resources)


@pytest.mark.parametrize(
    ("auth", "extra", "expected_type", "providers", "api_keys", "auth_fns"),
    [
        (
            CognitoAuth(user_pool_id=COGNITO_USER_POOL_ID),
            ["iam", ApiKeyAuth()],
            AUTH_TYPE_COGNITO,
            [(AUTH_TYPE_IAM, None, None), (AUTH_TYPE_API_KEY, None, None)],
            1,
            0,
        ),
        (
            "iam",
            [LambdaAuth(handler="functions/simple.handler")],
            AUTH_TYPE_IAM,
            [(AUTH_TYPE_LAMBDA, "lambdaAuthorizerConfig", None)],
            0,
            1,
        ),
        (
            "iam",
            [CognitoAuth(user_pool_id=COGNITO_USER_POOL_ID)],
            AUTH_TYPE_IAM,
            [(AUTH_TYPE_COGNITO, "userPoolConfig", {"userPoolId": COGNITO_USER_POOL_ID})],
            0,
            0,
        ),
        (
            "iam",
            [OidcAuth(issuer="https://auth.example.com", client_id="my-app")],
            AUTH_TYPE_IAM,
            [
                (
                    AUTH_TYPE_OIDC,
                    "openidConnectConfig",
                    {"issuer": "https://auth.example.com", "clientId": "my-app"},
                )
            ],
            0,
            0,
        ),
    ],
    ids=["iam+apikey", "iam+lambda", "iam+cognito", "iam+oidc"],
)
@pulumi.runtime.test
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
    api = make_api(auth=auth, additional_auth=extra)

    def check_resources(_):
        inputs = assert_graphql_api_inputs(
            pulumi_mocks,
            f"{TP}myapi",
            authenticationType=expected_type,
        )
        actual_providers = inputs["additionalAuthenticationProviders"]
        assert len(actual_providers) == len(providers)

        for provider, (exp_type, config_key, exp_config) in zip(
            actual_providers, providers, strict=False
        ):
            assert provider["authenticationType"] == exp_type
            if config_key is None:
                continue
            assert config_key in provider
            if exp_config is not None:
                for key, value in exp_config.items():
                    assert provider[config_key][key] == value

        assert len(pulumi_mocks.created_appsync_api_keys()) == api_keys
        actual_auth_fns = [f for f in pulumi_mocks.created_functions() if "authorizer" in f.name]
        assert len(actual_auth_fns) == auth_fns

    when_appsync_ready(api, check_resources)


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
            "Cannot specify function options",
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
