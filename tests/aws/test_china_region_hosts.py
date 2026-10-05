"""AWS hosts built from a region end in that region's DNS suffix: `amazonaws.com.cn` in China.

One file for every component, so a new hand-built regional host gets its case here.
"""

import pulumi
from pytest import mark, param

from stelvio.aws.api_gateway import HttpApi, RestApi, WebsocketApi
from stelvio.aws.cloudfront.router import Router
from stelvio.aws.cognito.identity_pool import IdentityPool
from stelvio.aws.cognito.types import IdentityPoolBinding
from stelvio.aws.cognito.user_pool import UserPool
from stelvio.config import AwsConfig
from stelvio.context import AppContext, _ContextStore

from ..conftest import TP
from .pulumi_mocks import ACCOUNT_ID, R, tid

API_ID = tid(TP + "api")


def use_region(region):
    _ContextStore.clear()
    _ContextStore.set(
        AppContext(
            name="test",
            env="test",
            aws=AwsConfig(profile="default", region=region),
            home="aws",
            customize={},
        )
    )


@mark.parametrize(
    ("region", "read_url", "expected"),
    [
        param(
            "cn-north-1",
            lambda: HttpApi("api").url,
            f"https://{API_ID}.execute-api.cn-north-1.amazonaws.com.cn/",
            id="http",
        ),
        param(
            "cn-northwest-1",
            lambda: HttpApi("api").url,
            f"https://{API_ID}.execute-api.cn-northwest-1.amazonaws.com.cn/",
            id="http-second-china-region",
        ),
        param(
            "cn-north-1",
            lambda: RestApi("api").url,
            f"https://{API_ID}.execute-api.cn-north-1.amazonaws.com.cn/v1",
            id="rest",
        ),
        param(
            "cn-north-1",
            lambda: WebsocketApi("api").url,
            f"wss://{API_ID}.execute-api.cn-north-1.amazonaws.com.cn/$default",
            id="websocket",
        ),
        param(
            "cn-north-1",
            lambda: WebsocketApi("api").management_url,
            f"https://{API_ID}.execute-api.cn-north-1.amazonaws.com.cn/$default",
            id="websocket-management",
        ),
    ],
)
@pulumi.runtime.test
def test_api_url_in_china_region(pulumi_mocks, region, read_url, expected):
    use_region(region)

    def check(url):
        assert url == expected

    return read_url().apply(check)


@mark.usefixtures("mock_get_or_install_dependencies_function", "project_cwd")
@mark.parametrize("api_cls", [RestApi, HttpApi])
def test_router_api_origin_host_in_china_region(pulumi_mocks, api_cls):
    use_region("cn-north-1")
    api = api_cls("api")
    api.route("GET", "/users", "functions/simple.handler")
    router = Router("edge")
    router.route("/api", api)

    @pulumi.runtime.test
    def deploy():
        return router.resources

    deploy()

    distribution = pulumi_mocks.assert_res("edge", R.DISTRIBUTION)
    assert [origin["domainName"] for origin in distribution.inputs["origins"]] == [
        f"{API_ID}.execute-api.cn-north-1.amazonaws.com.cn"
    ]


@mark.usefixtures("mock_get_or_install_dependencies_function", "project_cwd")
@mark.parametrize(
    ("make_pool", "pool_id"),
    [
        param(lambda: UserPool("users", usernames=["email"]), tid(TP + "users"), id="component"),
        param(
            lambda: f"arn:aws-cn:cognito-idp:cn-north-1:{ACCOUNT_ID}:userpool/cn-north-1_abc123",
            "cn-north-1_abc123",
            id="arn",
        ),
    ],
)
def test_cognito_authorizer_issuer_in_china_region(pulumi_mocks, make_pool, pool_id):
    use_region("cn-north-1")
    api = HttpApi("api")
    auth = api.add_cognito_authorizer("cognito", user_pool=make_pool(), audiences=["client-id"])
    api.route("GET", "/secure", "functions/simple.handler", auth=auth)

    @pulumi.runtime.test
    def deploy():
        return api.resources

    deploy()

    pulumi_mocks.assert_res(
        "api-authorizer-cognito",
        R.HTTP_API_AUTHORIZER,
        {
            "jwtConfiguration": {
                "audiences": ["client-id"],
                "issuer": f"https://cognito-idp.cn-north-1.amazonaws.com.cn/{pool_id}",
            }
        },
        partial=True,
    )


@mark.parametrize(
    ("make_pool", "pool_id"),
    [
        param(lambda: UserPool("users", usernames=["email"]), tid(TP + "users"), id="component"),
        param(lambda: "cn-north-1_abc123", "cn-north-1_abc123", id="pool-id"),
    ],
)
def test_identity_provider_name_in_china_region(pulumi_mocks, make_pool, pool_id):
    use_region("cn-north-1")
    identity = IdentityPool(
        "identity", user_pools=[IdentityPoolBinding(user_pool=make_pool(), client="client-id")]
    )

    @pulumi.runtime.test
    def deploy():
        return identity.resources

    deploy()

    identity_pool = pulumi_mocks.assert_res("identity", R.IDENTITY_POOL)
    providers = identity_pool.inputs["cognitoIdentityProviders"]
    assert [provider["providerName"] for provider in providers] == [
        f"cognito-idp.cn-north-1.amazonaws.com.cn/{pool_id}"
    ]
