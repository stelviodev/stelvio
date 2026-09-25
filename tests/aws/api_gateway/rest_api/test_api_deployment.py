"""The Deployment's trigger hash: API Gateway redeploys exactly when it changes, so it has
to follow every route-level setting and ignore what Pulumi already tracks on its own."""

from collections.abc import Callable
from functools import partial

import pulumi
from pytest import mark, param

from stelvio.aws.api_gateway import RestApi
from stelvio.aws.cors import CorsConfig

from ...pulumi_mocks import PulumiTestMocks, R

pytestmark = mark.usefixtures("project_cwd")

USERS = "functions/users.handler"
ORDERS = "functions/orders.handler"
JWT = "functions/authorizers/jwt.handler"
POOL_A = "arn:aws:cognito-idp:us-east-1:123456789012:userpool/us-east-1_AAA"
POOL_B = "arn:aws:cognito-idp:us-east-1:123456789012:userpool/us-east-1_BBB"


def deployment_hash(mocks: PulumiTestMocks, api_name: str) -> str:
    deployment = mocks.assert_res(f"{api_name}-deployment", R.API_DEPLOYMENT)
    return deployment.inputs["triggers"]["configuration_hash"]


def test_deployment_hash_is_stable(pulumi_mocks):
    """Pinned: a change in what feeds the hash redeploys every user's stage on upgrade."""
    api = RestApi("test-api")
    api.route("GET", "/users", USERS)
    api.route("POST", "/orders", ORDERS)

    @pulumi.runtime.test
    def deploy():
        return api.resources

    deploy()

    assert (
        deployment_hash(pulumi_mocks, "test-api")
        == "ba47a49cee7fbf249bf45ce9353cebf9fb665bd2a18e1c35797e81e28c6c9398"
    )


# Builders for the table below: each takes the API name and returns a configured RestApi.


def plain(name: str, **api_kwargs) -> RestApi:
    """GET /users, the base every row varies from."""
    api = RestApi(name, **api_kwargs)
    api.route("GET", "/users", USERS)
    return api


def routes(*specs: tuple) -> Callable[[str], RestApi]:
    def build(name: str) -> RestApi:
        api = RestApi(name)
        for spec in specs:
            api.route(*spec)
        return api

    return build


def token(authorizer: str = "jwt", ttl: int = 300) -> Callable[[RestApi], object]:
    return lambda api: api.add_token_authorizer(authorizer, JWT, ttl=ttl)


def cognito(pools: tuple[str, ...] = (POOL_A,), ttl: int = 300) -> Callable[[RestApi], object]:
    return lambda api: api.add_cognito_authorizer("cognito", user_pools=list(pools), ttl=ttl)


def with_auth(name: str, auth: object, scopes: list[str] | None = None) -> RestApi:
    api = RestApi(name)
    api.route(
        "GET", "/users", USERS, auth=auth(api) if callable(auth) else auth, cognito_scopes=scopes
    )
    return api


def with_default_auth(name: str, default: object, route_auth: object = None) -> RestApi:
    api = RestApi(name)
    api.default_auth = default(api) if callable(default) else default
    api.route("GET", "/users", USERS, auth=route_auth(api) if callable(route_auth) else route_auth)
    return api


ORIGIN = "https://example.com"


@mark.parametrize(
    ("build_a", "build_b", "same"),
    [
        param(plain, plain, True, id="identical"),
        param(
            routes(
                ("GET", "/users", USERS),
                ("POST", "/orders", ORDERS),
                (["GET", "POST"], "/items", USERS),
            ),
            routes(
                (["POST", "GET"], "/items", USERS),
                ("POST", "/orders", ORDERS),
                ("GET", "/users", USERS),
            ),
            True,
            id="declaration_and_method_order",
        ),
        param(plain, routes(("GET", "/customers", USERS)), False, id="path"),
        param(plain, routes(("POST", "/users", USERS)), False, id="method"),
        param(plain, routes(("GET", "/users", ORDERS)), False, id="handler"),
        # Same handler file and Lambda name either way (packaging differs, and Pulumi tracks
        # that on the Function itself): nothing for API Gateway to redeploy.
        param(
            routes(("GET", "/users", "functions/folder/handler.fn")),
            routes(("GET", "/users", "functions/folder::handler.fn")),
            True,
            id="file_vs_folder_mode_of_one_handler",
        ),
        param(plain, partial(with_auth, auth=token()), False, id="token_authorizer"),
        param(plain, partial(with_auth, auth="IAM"), False, id="iam"),
        param(
            partial(with_auth, auth=token()),
            partial(with_auth, auth="IAM"),
            False,
            id="authorizer_vs_iam",
        ),
        param(
            partial(with_auth, auth=token("a")),
            partial(with_auth, auth=token("b")),
            False,
            id="authorizer_name",
        ),
        param(
            partial(with_auth, auth=token(ttl=300)),
            partial(with_auth, auth=token(ttl=600)),
            True,
            id="authorizer_ttl_ignored",
        ),
        param(
            partial(with_auth, auth=cognito(pools=(POOL_A,))),
            partial(with_auth, auth=cognito(pools=(POOL_B,))),
            True,
            id="authorizer_pools_ignored",
        ),
        param(
            plain, partial(with_default_auth, default=token()), False, id="default_auth_inherited"
        ),
        param(plain, partial(with_default_auth, default="IAM"), False, id="default_iam_inherited"),
        param(
            partial(with_auth, auth=token()),
            partial(with_default_auth, default=token("other"), route_auth=token()),
            True,
            id="route_auth_ignores_default",
        ),
        param(
            partial(with_auth, auth=False),
            partial(with_default_auth, default=token(), route_auth=False),
            True,
            id="auth_false_opts_out_of_default",
        ),
        param(
            partial(with_auth, auth=cognito()),
            partial(with_auth, auth=cognito(), scopes=["read"]),
            False,
            id="scopes_added",
        ),
        param(
            partial(with_auth, auth=cognito(), scopes=["read"]),
            partial(with_auth, auth=cognito(), scopes=["write"]),
            False,
            id="scopes_changed",
        ),
        param(
            partial(with_auth, auth=cognito(), scopes=["read", "write"]),
            partial(with_auth, auth=cognito(), scopes=["write", "read"]),
            True,
            id="scopes_order",
        ),
        param(
            partial(with_auth, auth=cognito(), scopes=[]),
            partial(with_auth, auth=cognito()),
            True,
            id="empty_scopes_are_none",
        ),
        param(plain, partial(plain, cors=True), False, id="cors_added"),
        param(
            partial(plain, cors=True),
            partial(plain, cors=CorsConfig(allow_origins=ORIGIN)),
            False,
            id="cors_origin",
        ),
        param(
            partial(plain, cors=CorsConfig(allow_origins=ORIGIN)),
            partial(plain, cors=CorsConfig(allow_origins=ORIGIN, allow_credentials=True)),
            False,
            id="cors_credentials",
        ),
        param(
            partial(plain, cors=CorsConfig(allow_origins=ORIGIN)),
            partial(plain, cors=CorsConfig(allow_origins=ORIGIN, max_age=600)),
            False,
            id="cors_max_age",
        ),
        param(
            partial(plain, cors=CorsConfig(allow_origins=ORIGIN)),
            partial(plain, cors=CorsConfig(allow_origins=ORIGIN, expose_headers=["X-Custom"])),
            False,
            id="cors_expose_headers",
        ),
        param(
            partial(
                plain,
                cors=CorsConfig(
                    allow_methods=["GET", "POST"],
                    allow_headers=["Content-Type", "Authorization"],
                    expose_headers=["X-A", "X-B"],
                ),
            ),
            partial(
                plain,
                cors=CorsConfig(
                    allow_methods=["POST", "GET"],
                    allow_headers=["Authorization", "Content-Type"],
                    expose_headers=["X-B", "X-A"],
                ),
            ),
            True,
            id="cors_list_order",
        ),
    ],
)
def test_deployment_hash_follows_the_route_definition(pulumi_mocks, build_a, build_b, same):
    a = build_a("a")
    b = build_b("b")

    @pulumi.runtime.test
    def deploy():
        return [a.resources, b.resources]

    deploy()

    assert (deployment_hash(pulumi_mocks, "a") == deployment_hash(pulumi_mocks, "b")) is same
