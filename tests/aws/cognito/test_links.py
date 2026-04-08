import pulumi

from stelvio.aws.cognito.identity_pool import IdentityPool
from stelvio.aws.cognito.types import IdentityPoolBinding
from stelvio.aws.cognito.user_pool import UserPool

from ...conftest import TP
from ..pulumi_mocks import ACCOUNT_ID, DEFAULT_REGION, tid

POOL_ARN_TEMPLATE = f"arn:aws:cognito-idp:{DEFAULT_REGION}:{ACCOUNT_ID}:userpool/{{pool_id}}"


def _expected_pool_id(pool_name: str) -> str:
    """Pool .id resolves to the mock's resource_id; ARN uses {region}_{resource_id}."""
    return tid(f"{TP}{pool_name}")


def _expected_pool_arn(pool_name: str) -> str:
    resource_id = tid(f"{TP}{pool_name}")
    pool_id = f"{DEFAULT_REGION}_{resource_id}"
    return POOL_ARN_TEMPLATE.format(pool_id=pool_id)


# =========================================================================
# UserPool link tests
# =========================================================================


@pulumi.runtime.test
def test_user_pool_link_properties(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    link = pool.link()

    def check(args):
        properties, _ = args

        assert properties == {
            "user_pool_id": _expected_pool_id("users"),
            "user_pool_arn": _expected_pool_arn("users"),
        }

    pulumi.Output.all(link.properties, link.permissions).apply(check)


@pulumi.runtime.test
def test_user_pool_link_permissions(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    link = pool.link()

    def check(args):
        _, permissions = args

        assert len(permissions) == 1

        permission = permissions[0]
        assert sorted(permission.actions) == sorted(
            [
                "cognito-idp:GetUser",
                "cognito-idp:AdminGetUser",
                "cognito-idp:ListUsers",
            ]
        )
        assert len(permission.resources) == 1
        return permission.resources[0]

    def verify_resource(resource):
        assert resource == _expected_pool_arn("users")

    pulumi.Output.all(link.properties, link.permissions).apply(check).apply(verify_resource)


# =========================================================================
# UserPoolClient link tests
# =========================================================================


@pulumi.runtime.test
def test_client_link_properties(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client("web")
    _ = pool.resources  # trigger pool build so client gets _pool_resource
    link = client.link()

    def check(args):
        properties, _ = args

        client_id = tid(f"{TP}users-web")

        assert properties == {
            "client_id": client_id,
            "user_pool_id": _expected_pool_id("users"),
        }

    pulumi.Output.all(link.properties, link.permissions).apply(check)


@pulumi.runtime.test
def test_client_link_with_secret(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client("api", generate_secret=True)
    _ = pool.resources
    link = client.link()

    def check(args):
        properties, _ = args

        client_name = f"{TP}users-api"
        client_id = tid(client_name)
        client_secret = f"{tid(client_name)}-client-secret"

        assert properties == {
            "client_id": client_id,
            "user_pool_id": _expected_pool_id("users"),
            "client_secret": client_secret,
        }

    pulumi.Output.all(link.properties, link.permissions).apply(check)


@pulumi.runtime.test
def test_client_link_without_secret(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client("web", generate_secret=False)
    _ = pool.resources
    link = client.link()

    def check(args):
        properties, _ = args
        assert "client_secret" not in properties

    pulumi.Output.all(link.properties, link.permissions).apply(check)


@pulumi.runtime.test
def test_client_link_permissions(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client("web")
    _ = pool.resources
    link = client.link()

    def check(args):
        _, permissions = args

        assert len(permissions) == 1

        permission = permissions[0]
        assert sorted(permission.actions) == sorted(
            [
                "cognito-idp:GetUser",
                "cognito-idp:AdminGetUser",
                "cognito-idp:ListUsers",
            ]
        )
        assert len(permission.resources) == 1
        return permission.resources[0]

    def verify_resource(resource):
        assert resource == _expected_pool_arn("users")

    pulumi.Output.all(link.properties, link.permissions).apply(check).apply(verify_resource)


# =========================================================================
# IdentityPool link tests
# =========================================================================


@pulumi.runtime.test
def test_identity_pool_link_properties(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client("web")

    identity = IdentityPool(
        "app-identity",
        user_pools=[IdentityPoolBinding(user_pool=pool, client=client)],
    )
    link = identity.link()

    def check(args):
        properties, _ = args
        assert "identity_pool_id" in properties
        assert "authenticated_role_arn" in properties

    pulumi.Output.all(link.properties, link.permissions).apply(check)


@pulumi.runtime.test
def test_identity_pool_link_no_unauthenticated_arn_by_default(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client("web")

    identity = IdentityPool(
        "app-identity",
        user_pools=[IdentityPoolBinding(user_pool=pool, client=client)],
    )
    link = identity.link()

    def check(args):
        properties, _ = args
        assert "unauthenticated_role_arn" not in properties

    pulumi.Output.all(link.properties, link.permissions).apply(check)


@pulumi.runtime.test
def test_identity_pool_link_with_unauthenticated_arn(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client("web")

    identity = IdentityPool(
        "app-identity",
        user_pools=[IdentityPoolBinding(user_pool=pool, client=client)],
        allow_unauthenticated=True,
    )
    link = identity.link()

    def check(args):
        properties, _ = args
        assert "identity_pool_id" in properties
        assert "authenticated_role_arn" in properties
        assert "unauthenticated_role_arn" in properties

    pulumi.Output.all(link.properties, link.permissions).apply(check)


@pulumi.runtime.test
def test_identity_pool_link_no_permissions(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client("web")

    identity = IdentityPool(
        "app-identity",
        user_pools=[IdentityPoolBinding(user_pool=pool, client=client)],
    )
    link = identity.link()

    def check(args):
        _, permissions = args
        assert len(permissions) == 0

    pulumi.Output.all(link.properties, link.permissions).apply(check)
