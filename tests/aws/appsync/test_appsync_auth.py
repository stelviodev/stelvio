"""AppSync auth mode tests — all 5 auth modes, multi-auth, API key creation."""

from datetime import UTC, datetime, timedelta

import pulumi
import pytest

from stelvio.aws.appsync import ApiKeyAuth, AppSync, CognitoAuth, LambdaAuth, OidcAuth
from stelvio.aws.appsync.config import validate_auth_config
from stelvio.aws.appsync.constants import (
    AUTH_TYPE_API_KEY,
    AUTH_TYPE_COGNITO,
    AUTH_TYPE_IAM,
    AUTH_TYPE_LAMBDA,
    AUTH_TYPE_OIDC,
)
from stelvio.aws.function import Function, FunctionConfig

from .conftest import COGNITO_USER_POOL_ID, INLINE_SCHEMA, when_appsync_ready

TP = "test-test-"


@pulumi.runtime.test
def test_iam_auth(pulumi_mocks, project_cwd):
    api = AppSync("myapi", INLINE_SCHEMA, auth="iam")

    def check_resources(_):
        apis = pulumi_mocks.created_appsync_apis(f"{TP}myapi")
        assert len(apis) == 1
        assert apis[0].inputs["authenticationType"] == AUTH_TYPE_IAM

    when_appsync_ready(api, check_resources)


@pulumi.runtime.test
def test_api_key_auth_creates_api_key(pulumi_mocks, project_cwd):
    api = AppSync("myapi", INLINE_SCHEMA, auth=ApiKeyAuth())

    def check_resources(_):
        apis = pulumi_mocks.created_appsync_apis(f"{TP}myapi")
        assert len(apis) == 1
        assert apis[0].inputs["authenticationType"] == AUTH_TYPE_API_KEY

        api_keys = pulumi_mocks.created_appsync_api_keys()
        assert len(api_keys) == 1
        assert api_keys[0].typ == "aws:appsync/apiKey:ApiKey"

    when_appsync_ready(api, check_resources)


@pulumi.runtime.test
def test_api_key_auth_expiration(pulumi_mocks, project_cwd):
    api = AppSync("myapi", INLINE_SCHEMA, auth=ApiKeyAuth(expires=30))

    def check_resources(_):
        api_keys = pulumi_mocks.created_appsync_api_keys()
        assert len(api_keys) == 1
        # Expires should be an RFC3339 date string
        expires_str = api_keys[0].inputs["expires"]

        expires_dt = datetime.strptime(expires_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        now = datetime.now(tz=UTC)
        # Should expire roughly 30 days from now (with some tolerance)
        assert expires_dt > now + timedelta(days=29)
        assert expires_dt < now + timedelta(days=31)

    when_appsync_ready(api, check_resources)


@pulumi.runtime.test
def test_api_key_property_populated(pulumi_mocks, project_cwd):
    api = AppSync("myapi", INLINE_SCHEMA, auth=ApiKeyAuth())

    def check_key(key):
        assert key is not None
        assert key == f"da2-test-api-key-{TP}myapi-api-key-test-id"

    api.api_key.apply(check_key)


@pulumi.runtime.test
def test_api_key_property_none_when_not_configured(pulumi_mocks, project_cwd):
    api = AppSync("myapi", INLINE_SCHEMA, auth="iam")
    assert api.api_key is None


@pulumi.runtime.test
def test_cognito_auth(pulumi_mocks, project_cwd):
    api = AppSync("myapi", INLINE_SCHEMA, auth=CognitoAuth(user_pool_id=COGNITO_USER_POOL_ID))

    def check_resources(_):
        apis = pulumi_mocks.created_appsync_apis(f"{TP}myapi")
        assert len(apis) == 1
        assert apis[0].inputs["authenticationType"] == AUTH_TYPE_COGNITO
        user_pool_config = apis[0].inputs["userPoolConfig"]
        assert user_pool_config["userPoolId"] == COGNITO_USER_POOL_ID

    when_appsync_ready(api, check_resources)


@pulumi.runtime.test
def test_cognito_auth_with_region_and_regex(pulumi_mocks, project_cwd):
    api = AppSync(
        "myapi",
        INLINE_SCHEMA,
        auth=CognitoAuth(
            user_pool_id=COGNITO_USER_POOL_ID,
            region="eu-west-1",
            app_id_client_regex="^my-app.*",
        ),
    )

    def check_resources(_):
        apis = pulumi_mocks.created_appsync_apis(f"{TP}myapi")
        config = apis[0].inputs["userPoolConfig"]
        assert config["userPoolId"] == COGNITO_USER_POOL_ID
        assert config["awsRegion"] == "eu-west-1"
        assert config["appIdClientRegex"] == "^my-app.*"

    when_appsync_ready(api, check_resources)


@pulumi.runtime.test
def test_oidc_auth(pulumi_mocks, project_cwd):
    api = AppSync("myapi", INLINE_SCHEMA, auth=OidcAuth(issuer="https://auth.example.com"))

    def check_resources(_):
        apis = pulumi_mocks.created_appsync_apis(f"{TP}myapi")
        assert len(apis) == 1
        assert apis[0].inputs["authenticationType"] == AUTH_TYPE_OIDC
        oidc_config = apis[0].inputs["openidConnectConfig"]
        assert oidc_config["issuer"] == "https://auth.example.com"

    when_appsync_ready(api, check_resources)


@pulumi.runtime.test
def test_oidc_auth_with_all_options(pulumi_mocks, project_cwd):
    api = AppSync(
        "myapi",
        INLINE_SCHEMA,
        auth=OidcAuth(
            issuer="https://auth.example.com",
            client_id="my-client",
            auth_ttl=3600,
            iat_ttl=7200,
        ),
    )

    def check_resources(_):
        apis = pulumi_mocks.created_appsync_apis(f"{TP}myapi")
        oidc_config = apis[0].inputs["openidConnectConfig"]
        assert oidc_config["issuer"] == "https://auth.example.com"
        assert oidc_config["clientId"] == "my-client"
        assert oidc_config["authTtl"] == 3600
        assert oidc_config["iatTtl"] == 7200

    when_appsync_ready(api, check_resources)


@pulumi.runtime.test
def test_lambda_auth_creates_function(pulumi_mocks, project_cwd):
    api = AppSync(
        "myapi",
        INLINE_SCHEMA,
        auth=LambdaAuth(handler="functions/simple.handler"),
    )

    def check_resources(_):
        apis = pulumi_mocks.created_appsync_apis(f"{TP}myapi")
        assert len(apis) == 1
        assert apis[0].inputs["authenticationType"] == AUTH_TYPE_LAMBDA
        assert "lambdaAuthorizerConfig" in apis[0].inputs

        # Should create the authorizer Lambda
        fns = pulumi_mocks.created_functions(f"{TP}myapi-authorizer")
        assert len(fns) == 1
        assert fns[0].typ == "aws:lambda/function:Function"

    when_appsync_ready(api, check_resources)


@pulumi.runtime.test
def test_lambda_auth_creates_permission(pulumi_mocks, project_cwd):
    api = AppSync(
        "myapi",
        INLINE_SCHEMA,
        auth=LambdaAuth(handler="functions/simple.handler"),
    )

    def check_resources(_):
        perms = pulumi_mocks.created_permissions()
        auth_perms = [p for p in perms if "auth-perm" in p.name]
        assert len(auth_perms) == 1
        perm = auth_perms[0]
        assert perm.inputs["action"] == "lambda:InvokeFunction"
        assert perm.inputs["principal"] == "appsync.amazonaws.com"

    when_appsync_ready(api, check_resources)


@pulumi.runtime.test
def test_lambda_auth_with_options(pulumi_mocks, project_cwd):
    api = AppSync(
        "myapi",
        INLINE_SCHEMA,
        auth=LambdaAuth(handler="functions/simple.handler", memory=256, timeout=10),
    )

    def check_resources(_):
        fns = pulumi_mocks.created_functions(f"{TP}myapi-authorizer")
        assert len(fns) == 1
        assert fns[0].inputs["memorySize"] == 256
        assert fns[0].inputs["timeout"] == 10

    when_appsync_ready(api, check_resources)


@pulumi.runtime.test
def test_lambda_auth_with_existing_function_handler(pulumi_mocks, project_cwd):
    auth_fn = Function("existing-auth-fn", handler="functions/simple.handler")
    api = AppSync(
        "myapi",
        INLINE_SCHEMA,
        auth=LambdaAuth(handler=auth_fn),
    )

    def check_resources(_):
        apis = pulumi_mocks.created_appsync_apis(f"{TP}myapi")
        assert len(apis) == 1
        assert apis[0].inputs["authenticationType"] == AUTH_TYPE_LAMBDA
        assert "lambdaAuthorizerConfig" in apis[0].inputs

        existing_fn = pulumi_mocks.assert_function_created(f"{TP}existing-auth-fn")
        assert existing_fn.typ == "aws:lambda/function:Function"
        assert len(pulumi_mocks.created_functions(f"{TP}myapi-authorizer")) == 0

    when_appsync_ready(api, check_resources)


@pulumi.runtime.test
def test_lambda_auth_with_result_ttl(pulumi_mocks, project_cwd):
    api = AppSync(
        "myapi",
        INLINE_SCHEMA,
        auth=LambdaAuth(handler="functions/simple.handler", result_ttl=300),
    )

    def check_resources(_):
        apis = pulumi_mocks.created_appsync_apis(f"{TP}myapi")
        lambda_config = apis[0].inputs["lambdaAuthorizerConfig"]
        assert lambda_config["authorizerResultTtlInSeconds"] == 300

    when_appsync_ready(api, check_resources)


@pulumi.runtime.test
def test_lambda_auth_with_identity_validation_expression(pulumi_mocks, project_cwd):
    api = AppSync(
        "myapi",
        INLINE_SCHEMA,
        auth=LambdaAuth(
            handler="functions/simple.handler",
            identity_validation_expression=r"^Bearer\\s.+$",
        ),
    )

    def check_resources(_):
        apis = pulumi_mocks.created_appsync_apis(f"{TP}myapi")
        lambda_config = apis[0].inputs["lambdaAuthorizerConfig"]
        assert lambda_config["identityValidationExpression"] == r"^Bearer\\s.+$"

    when_appsync_ready(api, check_resources)


@pulumi.runtime.test
def test_multi_auth_default_plus_additional(pulumi_mocks, project_cwd):
    api = AppSync(
        "myapi",
        INLINE_SCHEMA,
        auth=CognitoAuth(user_pool_id=COGNITO_USER_POOL_ID),
        additional_auth=["iam", ApiKeyAuth()],
    )

    def check_resources(_):
        apis = pulumi_mocks.created_appsync_apis(f"{TP}myapi")
        assert len(apis) == 1
        assert apis[0].inputs["authenticationType"] == AUTH_TYPE_COGNITO

        providers = apis[0].inputs["additionalAuthenticationProviders"]
        assert len(providers) == 2
        assert providers[0]["authenticationType"] == AUTH_TYPE_IAM
        assert providers[1]["authenticationType"] == AUTH_TYPE_API_KEY

        # API key should be created since API_KEY is in additional auth
        api_keys = pulumi_mocks.created_appsync_api_keys()
        assert len(api_keys) == 1

    when_appsync_ready(api, check_resources)


@pulumi.runtime.test
def test_multi_auth_with_lambda_additional(pulumi_mocks, project_cwd):
    api = AppSync(
        "myapi",
        INLINE_SCHEMA,
        auth="iam",
        additional_auth=[LambdaAuth(handler="functions/simple.handler")],
    )

    def check_resources(_):
        apis = pulumi_mocks.created_appsync_apis(f"{TP}myapi")
        assert apis[0].inputs["authenticationType"] == AUTH_TYPE_IAM

        providers = apis[0].inputs["additionalAuthenticationProviders"]
        assert len(providers) == 1
        assert providers[0]["authenticationType"] == AUTH_TYPE_LAMBDA
        assert "lambdaAuthorizerConfig" in providers[0]

        # Lambda authorizer function should be created
        fns = pulumi_mocks.created_functions()
        auth_fns = [f for f in fns if "authorizer" in f.name]
        assert len(auth_fns) == 1

    when_appsync_ready(api, check_resources)


@pulumi.runtime.test
def test_api_key_in_additional_auth_creates_key(pulumi_mocks, project_cwd):
    api = AppSync(
        "myapi",
        INLINE_SCHEMA,
        auth="iam",
        additional_auth=[ApiKeyAuth(expires=90)],
    )

    def check_resources(_):
        api_keys = pulumi_mocks.created_appsync_api_keys()
        assert len(api_keys) == 1

    when_appsync_ready(api, check_resources)


@pulumi.runtime.test
def test_api_key_property_from_additional_auth(pulumi_mocks, project_cwd):
    api = AppSync(
        "myapi",
        INLINE_SCHEMA,
        auth="iam",
        additional_auth=[ApiKeyAuth()],
    )

    def check_key(key):
        assert key is not None
        assert key == f"da2-test-api-key-{TP}myapi-api-key-test-id"

    api.api_key.apply(check_key)


# --- Auth config validation ---


def test_api_key_auth_expires_too_low():
    with pytest.raises(ValueError, match="expires must be an integer between 1 and 365"):
        ApiKeyAuth(expires=0)


def test_api_key_auth_expires_too_high():
    with pytest.raises(ValueError, match="expires must be an integer between 1 and 365"):
        ApiKeyAuth(expires=366)


def test_api_key_auth_expires_negative():
    with pytest.raises(ValueError, match="expires must be an integer between 1 and 365"):
        ApiKeyAuth(expires=-1)


def test_cognito_auth_empty_user_pool_id():
    with pytest.raises(ValueError, match="user_pool_id cannot be empty"):
        CognitoAuth(user_pool_id="")


def test_oidc_auth_empty_issuer():
    with pytest.raises(ValueError, match="issuer cannot be empty"):
        OidcAuth(issuer="")


def test_lambda_auth_empty_handler():
    with pytest.raises(ValueError, match="handler cannot be empty"):
        LambdaAuth(handler="")


def test_lambda_auth_extra_opts_with_function_config():
    with pytest.raises(ValueError, match="Cannot specify function options"):
        LambdaAuth(handler=FunctionConfig(handler="functions/simple.handler"), memory=256)


def test_validate_auth_config_invalid_type():
    with pytest.raises(TypeError, match="Invalid auth config"):
        validate_auth_config(42)


def test_validate_auth_config_invalid_string():
    with pytest.raises(TypeError, match="Invalid auth config"):
        validate_auth_config("invalid")


# --- Duplicate auth modes ---


def test_duplicate_auth_default_and_additional(project_cwd):
    with pytest.raises(ValueError, match="Duplicate authentication mode"):
        AppSync(
            "myapi",
            INLINE_SCHEMA,
            auth=ApiKeyAuth(),
            additional_auth=[ApiKeyAuth(expires=90)],
        )


def test_duplicate_auth_within_additional(project_cwd):
    with pytest.raises(ValueError, match="Duplicate authentication mode"):
        AppSync(
            "myapi",
            INLINE_SCHEMA,
            auth="iam",
            additional_auth=[ApiKeyAuth(), ApiKeyAuth(expires=90)],
        )


def test_duplicate_iam_in_additional(project_cwd):
    with pytest.raises(ValueError, match="Duplicate authentication mode"):
        AppSync(
            "myapi",
            INLINE_SCHEMA,
            auth="iam",
            additional_auth=["iam"],
        )


# --- Cognito/OIDC as additional auth ---


@pulumi.runtime.test
def test_cognito_as_additional_auth(pulumi_mocks, project_cwd):
    """Cognito in additional_auth should produce user_pool_config in provider."""
    api = AppSync(
        "myapi",
        INLINE_SCHEMA,
        auth="iam",
        additional_auth=[CognitoAuth(user_pool_id=COGNITO_USER_POOL_ID)],
    )

    def check_resources(_):
        apis = pulumi_mocks.created_appsync_apis(f"{TP}myapi")
        assert apis[0].inputs["authenticationType"] == AUTH_TYPE_IAM

        providers = apis[0].inputs["additionalAuthenticationProviders"]
        assert len(providers) == 1
        assert providers[0]["authenticationType"] == AUTH_TYPE_COGNITO
        user_pool_config = providers[0]["userPoolConfig"]
        assert user_pool_config["userPoolId"] == COGNITO_USER_POOL_ID

    when_appsync_ready(api, check_resources)


@pulumi.runtime.test
def test_oidc_as_additional_auth(pulumi_mocks, project_cwd):
    """OIDC in additional_auth should produce openid_connect_config in provider."""
    api = AppSync(
        "myapi",
        INLINE_SCHEMA,
        auth="iam",
        additional_auth=[OidcAuth(issuer="https://auth.example.com", client_id="my-app")],
    )

    def check_resources(_):
        apis = pulumi_mocks.created_appsync_apis(f"{TP}myapi")
        assert apis[0].inputs["authenticationType"] == AUTH_TYPE_IAM

        providers = apis[0].inputs["additionalAuthenticationProviders"]
        assert len(providers) == 1
        assert providers[0]["authenticationType"] == AUTH_TYPE_OIDC
        oidc_config = providers[0]["openidConnectConfig"]
        assert oidc_config["issuer"] == "https://auth.example.com"
        assert oidc_config["clientId"] == "my-app"

    when_appsync_ready(api, check_resources)


# --- LambdaAuth with FunctionConfig ---


@pulumi.runtime.test
def test_lambda_auth_with_function_config(pulumi_mocks, project_cwd):
    """LambdaAuth accepts a FunctionConfig as handler."""
    api = AppSync(
        "myapi",
        INLINE_SCHEMA,
        auth=LambdaAuth(handler=FunctionConfig(handler="functions/simple.handler", memory=512)),
    )

    def check_resources(_):
        apis = pulumi_mocks.created_appsync_apis(f"{TP}myapi")
        assert apis[0].inputs["authenticationType"] == AUTH_TYPE_LAMBDA
        assert "lambdaAuthorizerConfig" in apis[0].inputs

        fns = pulumi_mocks.created_functions(f"{TP}myapi-authorizer")
        assert len(fns) == 1
        assert fns[0].inputs["memorySize"] == 512

    when_appsync_ready(api, check_resources)
