"""AppSync auth mode tests — all 5 auth modes, multi-auth, API key creation."""

import pulumi

from stelvio.aws.appsync import ApiKeyAuth, AppSync, CognitoAuth, LambdaAuth, OidcAuth
from stelvio.aws.appsync.constants import (
    AUTH_TYPE_API_KEY,
    AUTH_TYPE_COGNITO,
    AUTH_TYPE_IAM,
    AUTH_TYPE_LAMBDA,
    AUTH_TYPE_OIDC,
)

from .conftest import COGNITO_USER_POOL_ID, INLINE_SCHEMA

TP = "test-test-"


@pulumi.runtime.test
def test_iam_auth(pulumi_mocks, project_cwd):
    api = AppSync("myapi", INLINE_SCHEMA, auth="iam")
    _ = api.resources

    def check_resources(_):
        apis = pulumi_mocks.created_appsync_apis(f"{TP}myapi")
        assert len(apis) == 1
        assert apis[0].inputs["authenticationType"] == AUTH_TYPE_IAM

    api.resources.completed.apply(check_resources)


@pulumi.runtime.test
def test_api_key_auth_creates_api_key(pulumi_mocks, project_cwd):
    api = AppSync("myapi", INLINE_SCHEMA, auth=ApiKeyAuth())
    _ = api.resources

    def check_resources(_):
        apis = pulumi_mocks.created_appsync_apis(f"{TP}myapi")
        assert len(apis) == 1
        assert apis[0].inputs["authenticationType"] == AUTH_TYPE_API_KEY

        api_keys = pulumi_mocks.created_appsync_api_keys()
        assert len(api_keys) == 1
        assert api_keys[0].typ == "aws:appsync/apiKey:ApiKey"

    api.resources.completed.apply(check_resources)


@pulumi.runtime.test
def test_api_key_auth_expiration(pulumi_mocks, project_cwd):
    api = AppSync("myapi", INLINE_SCHEMA, auth=ApiKeyAuth(expires=30))
    _ = api.resources

    def check_resources(_):
        api_keys = pulumi_mocks.created_appsync_api_keys()
        assert len(api_keys) == 1
        # Expires should be a numeric string (epoch seconds)
        expires_str = api_keys[0].inputs["expires"]
        expires = int(expires_str)
        assert expires > 0

    api.resources.completed.apply(check_resources)


@pulumi.runtime.test
def test_api_key_property_populated(pulumi_mocks, project_cwd):
    api = AppSync("myapi", INLINE_SCHEMA, auth=ApiKeyAuth())

    def check_key(key):
        assert key is not None
        assert "da2-" in key

    api.api_key.apply(check_key)


@pulumi.runtime.test
def test_api_key_property_none_when_not_configured(pulumi_mocks, project_cwd):
    api = AppSync("myapi", INLINE_SCHEMA, auth="iam")
    assert api.api_key is None


@pulumi.runtime.test
def test_cognito_auth(pulumi_mocks, project_cwd):
    api = AppSync("myapi", INLINE_SCHEMA, auth=CognitoAuth(user_pool_id=COGNITO_USER_POOL_ID))
    _ = api.resources

    def check_resources(_):
        apis = pulumi_mocks.created_appsync_apis(f"{TP}myapi")
        assert len(apis) == 1
        assert apis[0].inputs["authenticationType"] == AUTH_TYPE_COGNITO
        user_pool_config = apis[0].inputs["userPoolConfig"]
        assert user_pool_config["userPoolId"] == COGNITO_USER_POOL_ID

    api.resources.completed.apply(check_resources)


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
    _ = api.resources

    def check_resources(_):
        apis = pulumi_mocks.created_appsync_apis(f"{TP}myapi")
        config = apis[0].inputs["userPoolConfig"]
        assert config["userPoolId"] == COGNITO_USER_POOL_ID
        assert config["awsRegion"] == "eu-west-1"
        assert config["appIdClientRegex"] == "^my-app.*"

    api.resources.completed.apply(check_resources)


@pulumi.runtime.test
def test_oidc_auth(pulumi_mocks, project_cwd):
    api = AppSync("myapi", INLINE_SCHEMA, auth=OidcAuth(issuer="https://auth.example.com"))
    _ = api.resources

    def check_resources(_):
        apis = pulumi_mocks.created_appsync_apis(f"{TP}myapi")
        assert len(apis) == 1
        assert apis[0].inputs["authenticationType"] == AUTH_TYPE_OIDC
        oidc_config = apis[0].inputs["openidConnectConfig"]
        assert oidc_config["issuer"] == "https://auth.example.com"

    api.resources.completed.apply(check_resources)


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
    _ = api.resources

    def check_resources(_):
        apis = pulumi_mocks.created_appsync_apis(f"{TP}myapi")
        oidc_config = apis[0].inputs["openidConnectConfig"]
        assert oidc_config["issuer"] == "https://auth.example.com"
        assert oidc_config["clientId"] == "my-client"
        assert oidc_config["authTtl"] == 3600
        assert oidc_config["iatTtl"] == 7200

    api.resources.completed.apply(check_resources)


@pulumi.runtime.test
def test_lambda_auth_creates_function(pulumi_mocks, project_cwd):
    api = AppSync(
        "myapi",
        INLINE_SCHEMA,
        auth=LambdaAuth(handler="functions/simple.handler"),
    )
    _ = api.resources

    def check_resources(_):
        apis = pulumi_mocks.created_appsync_apis(f"{TP}myapi")
        assert len(apis) == 1
        assert apis[0].inputs["authenticationType"] == AUTH_TYPE_LAMBDA
        assert "lambdaAuthorizerConfig" in apis[0].inputs

        # Should create the authorizer Lambda
        fns = pulumi_mocks.created_functions(f"{TP}myapi-authorizer")
        assert len(fns) == 1
        assert fns[0].typ == "aws:lambda/function:Function"

    api.resources.completed.apply(check_resources)


@pulumi.runtime.test
def test_lambda_auth_creates_permission(pulumi_mocks, project_cwd):
    api = AppSync(
        "myapi",
        INLINE_SCHEMA,
        auth=LambdaAuth(handler="functions/simple.handler"),
    )
    _ = api.resources

    def check_resources(_):
        perms = pulumi_mocks.created_permissions()
        auth_perms = [p for p in perms if "auth-perm" in p.name]
        assert len(auth_perms) == 1
        perm = auth_perms[0]
        assert perm.inputs["action"] == "lambda:InvokeFunction"
        assert perm.inputs["principal"] == "appsync.amazonaws.com"

    api.resources.completed.apply(check_resources)


@pulumi.runtime.test
def test_lambda_auth_with_options(pulumi_mocks, project_cwd):
    api = AppSync(
        "myapi",
        INLINE_SCHEMA,
        auth=LambdaAuth(handler="functions/simple.handler", memory=256, timeout=10),
    )
    _ = api.resources

    def check_resources(_):
        fns = pulumi_mocks.created_functions(f"{TP}myapi-authorizer")
        assert len(fns) == 1
        assert fns[0].inputs["memorySize"] == 256
        assert fns[0].inputs["timeout"] == 10

    api.resources.completed.apply(check_resources)


@pulumi.runtime.test
def test_lambda_auth_with_result_ttl(pulumi_mocks, project_cwd):
    api = AppSync(
        "myapi",
        INLINE_SCHEMA,
        auth=LambdaAuth(handler="functions/simple.handler", result_ttl=300),
    )
    _ = api.resources

    def check_resources(_):
        apis = pulumi_mocks.created_appsync_apis(f"{TP}myapi")
        lambda_config = apis[0].inputs["lambdaAuthorizerConfig"]
        assert lambda_config["authorizerResultTtlInSeconds"] == 300

    api.resources.completed.apply(check_resources)


@pulumi.runtime.test
def test_multi_auth_default_plus_additional(pulumi_mocks, project_cwd):
    api = AppSync(
        "myapi",
        INLINE_SCHEMA,
        auth=CognitoAuth(user_pool_id=COGNITO_USER_POOL_ID),
        additional_auth=["iam", ApiKeyAuth()],
    )
    _ = api.resources

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

    api.resources.completed.apply(check_resources)


@pulumi.runtime.test
def test_multi_auth_with_lambda_additional(pulumi_mocks, project_cwd):
    api = AppSync(
        "myapi",
        INLINE_SCHEMA,
        auth="iam",
        additional_auth=[LambdaAuth(handler="functions/simple.handler")],
    )
    _ = api.resources

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

    api.resources.completed.apply(check_resources)


@pulumi.runtime.test
def test_api_key_in_additional_auth_creates_key(pulumi_mocks, project_cwd):
    api = AppSync(
        "myapi",
        INLINE_SCHEMA,
        auth="iam",
        additional_auth=[ApiKeyAuth(expires=90)],
    )
    _ = api.resources

    def check_resources(_):
        api_keys = pulumi_mocks.created_appsync_api_keys()
        assert len(api_keys) == 1

    api.resources.completed.apply(check_resources)


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
        assert "da2-" in key

    api.api_key.apply(check_key)
