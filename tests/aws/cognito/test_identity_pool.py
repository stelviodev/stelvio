import json

import pulumi
import pytest

from stelvio.aws.cognito.identity_pool import IdentityPool
from stelvio.aws.cognito.types import (
    IdentityPoolBinding,
    IdentityPoolConfig,
    IdentityPoolConfigDict,
    IdentityPoolPermissions,
    IdentityPoolPermissionsDict,
)
from stelvio.aws.cognito.user_pool import UserPool
from stelvio.aws.permission import AwsPermission

from ...conftest import TP
from ..conftest import assert_hash_truncated
from ..pulumi_mocks import ACCOUNT_ID, R, tid, tn

AUTH_ROLE_ARN = f"arn:aws:iam::{ACCOUNT_ID}:role/{tn(f'{TP}app-identity-auth-role')}"
UNAUTH_ROLE_ARN = f"arn:aws:iam::{ACCOUNT_ID}:role/{tn(f'{TP}app-identity-unauth-role')}"

# =========================================================================
# Config validation tests (no Pulumi mocks needed)
# =========================================================================


def test_empty_user_pools_rejection():
    with pytest.raises(ValueError, match="user_pools must contain at least one binding"):
        IdentityPoolConfig(user_pools=[])


def test_unauthenticated_permissions_without_flag_rejection():
    with pytest.raises(ValueError, match="allow_unauthenticated=True"):
        IdentityPoolConfig(
            user_pools=[IdentityPoolBinding(user_pool="us-east-1_pool123", client="client-id")],
            permissions=IdentityPoolPermissions(
                unauthenticated=[
                    AwsPermission(actions=["s3:GetObject"], resources=["*"]),
                ]
            ),
        )


def test_unauthenticated_permissions_with_flag():
    config = IdentityPoolConfig(
        user_pools=[IdentityPoolBinding(user_pool="us-east-1_pool123", client="client-id")],
        permissions=IdentityPoolPermissions(
            unauthenticated=[
                AwsPermission(actions=["s3:GetObject"], resources=["*"]),
            ]
        ),
        allow_unauthenticated=True,
    )
    assert config.allow_unauthenticated is True
    assert len(config.permissions.unauthenticated) == 1


def test_config_vs_opts_rejection():
    binding = IdentityPoolBinding(user_pool="us-east-1_pool123", client="client-id")
    with pytest.raises(ValueError, match="cannot combine"):
        IdentityPool("ip", config=IdentityPoolConfig(user_pools=[binding]), user_pools=[binding])


def test_valid_config_from_dataclass():
    config = IdentityPoolConfig(
        user_pools=[IdentityPoolBinding(user_pool="us-east-1_pool123", client="client-id")],
    )
    assert len(config.user_pools) == 1
    assert config.permissions is None


def test_binding_normalization_from_dict():
    config = IdentityPoolConfig(
        user_pools=[{"user_pool": "us-east-1_pool123", "client": "client-id"}],
    )
    assert isinstance(config.user_pools[0], IdentityPoolBinding)
    assert config.user_pools[0].user_pool == "us-east-1_pool123"
    assert config.user_pools[0].client == "client-id"


def test_permissions_normalization_from_dict():
    config = IdentityPoolConfig(
        user_pools=[IdentityPoolBinding(user_pool="us-east-1_pool123", client="client-id")],
        permissions={
            "authenticated": [
                AwsPermission(actions=["s3:GetObject"], resources=["*"]),
            ],
        },
    )
    assert isinstance(config.permissions, IdentityPoolPermissions)
    assert len(config.permissions.authenticated) == 1


def test_duplicate_bindings_rejection():
    with pytest.raises(ValueError, match="Duplicate binding"):
        IdentityPoolConfig(
            user_pools=[
                IdentityPoolBinding(user_pool="us-east-1_pool123", client="client-id"),
                IdentityPoolBinding(user_pool="us-east-1_pool123", client="client-id"),
            ],
        )


def test_duplicate_bindings_different_clients_allowed():
    config = IdentityPoolConfig(
        user_pools=[
            IdentityPoolBinding(user_pool="us-east-1_pool123", client="client-a"),
            IdentityPoolBinding(user_pool="us-east-1_pool123", client="client-b"),
        ],
    )
    assert len(config.user_pools) == 2


def test_string_pool_id_without_underscore_rejected():
    with pytest.raises(ValueError, match="must be a Cognito pool ID"):
        IdentityPoolBinding(user_pool="invalidpoolid", client="client-id")


def test_string_pool_id_with_valid_format_accepted():
    binding = IdentityPoolBinding(user_pool="eu-west-1_abc123", client="client-id")
    assert binding.user_pool == "eu-west-1_abc123"


def test_default_permissions():
    config = IdentityPoolConfig(
        user_pools=[IdentityPoolBinding(user_pool="us-east-1_pool123", client="client-id")],
    )
    assert config.permissions is None


# =========================================================================
# Resource creation tests (with Pulumi mocks)
# =========================================================================


def test_basic_identity_pool_creation(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client("web")

    identity = IdentityPool(
        "app-identity",
        user_pools=[IdentityPoolBinding(user_pool=pool, client=client)],
    )

    @pulumi.runtime.test
    def deploy():
        return identity.resources

    deploy()

    pulumi_mocks.assert_res(
        "app-identity",
        R.IDENTITY_POOL,
        {
            "identityPoolName": f"{TP}app-identity",
            "allowUnauthenticatedIdentities": False,
            "cognitoIdentityProviders": [
                {
                    "providerName": "cognito-idp.us-east-1.amazonaws.com/test-test-users-test-id",
                    "clientId": tid(f"{TP}users-web"),
                    "serverSideTokenCheck": False,
                }
            ],
        },
    )
    pulumi_mocks.assert_res_counts(
        {
            R.USER_POOL: 1,
            R.USER_POOL_CLIENT: 1,
            R.IDENTITY_POOL: 1,
            R.ROLE: 1,
            R.IDENTITY_POOL_ROLE_ATTACHMENT: 1,
        }
    )


def test_identity_pool_naming(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client("web")

    identity = IdentityPool(
        "app-identity",
        user_pools=[IdentityPoolBinding(user_pool=pool, client=client)],
    )

    @pulumi.runtime.test
    def deploy():
        return identity.resources

    deploy()

    pulumi_mocks.assert_res(
        "app-identity", R.IDENTITY_POOL, {"identityPoolName": TP + "app-identity"}, partial=True
    )


def test_identity_provider_name_uses_resolved_region(pulumi_mocks, no_region_context):
    """Provider name carries the chain-resolved region when config has none.

    Scenario (the default new-user setup): no `region=` in @app.config, but the AWS
    chain resolves one (here AWS_REGION=eu-central-1 via the fixture) — so Stelvio
    deploys fine. The issuer string must use that resolved region. The original bug
    interpolated the raw config value (None) into
    "cognito-idp.None.amazonaws.com/..." → hard deploy failure.
    """
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client("web")

    identity = IdentityPool(
        "app-identity",
        user_pools=[IdentityPoolBinding(user_pool=pool, client=client)],
    )

    @pulumi.runtime.test
    def deploy():
        return identity.resources

    deploy()

    pulumi_mocks.assert_res(
        "app-identity",
        R.IDENTITY_POOL,
        {
            "cognitoIdentityProviders": [
                {
                    "providerName": f"cognito-idp.eu-central-1.amazonaws.com/{tid(f'{TP}users')}",
                    "clientId": tid(f"{TP}users-web"),
                    "serverSideTokenCheck": False,
                }
            ]
        },
        partial=True,
    )


def test_allow_unauthenticated_enabled(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client("web")

    identity = IdentityPool(
        "app-identity",
        user_pools=[IdentityPoolBinding(user_pool=pool, client=client)],
        allow_unauthenticated=True,
    )

    @pulumi.runtime.test
    def deploy():
        return identity.resources

    deploy()

    pulumi_mocks.assert_res(
        "app-identity", R.IDENTITY_POOL, {"allowUnauthenticatedIdentities": True}, partial=True
    )


def test_allow_unauthenticated_disabled_by_default(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client("web")

    identity = IdentityPool(
        "app-identity",
        user_pools=[IdentityPoolBinding(user_pool=pool, client=client)],
    )

    @pulumi.runtime.test
    def deploy():
        return identity.resources

    deploy()

    pulumi_mocks.assert_res(
        "app-identity", R.IDENTITY_POOL, {"allowUnauthenticatedIdentities": False}, partial=True
    )


def test_authenticated_role_created(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client("web")

    identity = IdentityPool(
        "app-identity",
        user_pools=[IdentityPoolBinding(user_pool=pool, client=client)],
    )

    @pulumi.runtime.test
    def deploy():
        return identity.resources

    deploy()

    pulumi_mocks.assert_res("app-identity-auth-role", R.ROLE)


def test_authenticated_role_trust_policy(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client("web")

    identity = IdentityPool(
        "app-identity",
        user_pools=[IdentityPoolBinding(user_pool=pool, client=client)],
    )

    @pulumi.runtime.test
    def deploy():
        return identity.resources

    deploy()

    auth_role = pulumi_mocks.assert_res("app-identity-auth-role", R.ROLE)
    assert json.loads(auth_role.inputs["assumeRolePolicy"]) == {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": "sts:AssumeRoleWithWebIdentity",
                "Principal": {"Federated": "cognito-identity.amazonaws.com"},
                "Condition": {
                    "StringEquals": {
                        "cognito-identity.amazonaws.com:aud": tid(f"{TP}app-identity")
                    },
                    "ForAnyValue:StringLike": {
                        "cognito-identity.amazonaws.com:amr": "authenticated"
                    },
                },
            }
        ],
    }


def test_authenticated_role_policy_created_with_permissions(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client("web")

    identity = IdentityPool(
        "app-identity",
        user_pools=[IdentityPoolBinding(user_pool=pool, client=client)],
        permissions=IdentityPoolPermissions(
            authenticated=[
                AwsPermission(
                    actions=["s3:GetObject", "s3:PutObject"],
                    resources=["arn:aws:s3:::my-bucket/*"],
                ),
            ],
        ),
    )

    @pulumi.runtime.test
    def deploy():
        return identity.resources

    deploy()

    pulumi_mocks.assert_res(
        "app-identity-auth-policy",
        R.ROLE_POLICY,
        {"role": tid(f"{TP}app-identity-auth-role")},
        partial=True,
    )


def test_no_auth_role_policy_when_no_permissions(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client("web")

    identity = IdentityPool(
        "app-identity",
        user_pools=[IdentityPoolBinding(user_pool=pool, client=client)],
    )

    @pulumi.runtime.test
    def deploy():
        return identity.resources

    deploy()

    pulumi_mocks.assert_no_res(R.ROLE_POLICY)


def test_unauthenticated_role_created_when_enabled(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client("web")

    identity = IdentityPool(
        "app-identity",
        user_pools=[IdentityPoolBinding(user_pool=pool, client=client)],
        allow_unauthenticated=True,
    )

    @pulumi.runtime.test
    def deploy():
        return identity.resources

    deploy()

    pulumi_mocks.assert_res("app-identity-unauth-role", R.ROLE)


def test_unauthenticated_role_skipped_when_disabled(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client("web")

    identity = IdentityPool(
        "app-identity",
        user_pools=[IdentityPoolBinding(user_pool=pool, client=client)],
    )

    @pulumi.runtime.test
    def deploy():
        return identity.resources

    deploy()

    assert [r.name for r in pulumi_mocks.created(R.ROLE)] == [f"{TP}app-identity-auth-role"]


def test_unauthenticated_role_trust_policy(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client("web")

    identity = IdentityPool(
        "app-identity",
        user_pools=[IdentityPoolBinding(user_pool=pool, client=client)],
        allow_unauthenticated=True,
    )

    @pulumi.runtime.test
    def deploy():
        return identity.resources

    deploy()

    unauth_role = pulumi_mocks.assert_res("app-identity-unauth-role", R.ROLE)
    assert json.loads(unauth_role.inputs["assumeRolePolicy"]) == {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": "sts:AssumeRoleWithWebIdentity",
                "Principal": {"Federated": "cognito-identity.amazonaws.com"},
                "Condition": {
                    "StringEquals": {
                        "cognito-identity.amazonaws.com:aud": tid(f"{TP}app-identity")
                    },
                    "ForAnyValue:StringLike": {
                        "cognito-identity.amazonaws.com:amr": "unauthenticated"
                    },
                },
            }
        ],
    }


def test_unauthenticated_role_policy_created(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client("web")

    identity = IdentityPool(
        "app-identity",
        user_pools=[IdentityPoolBinding(user_pool=pool, client=client)],
        permissions=IdentityPoolPermissions(
            authenticated=[
                AwsPermission(actions=["s3:GetObject"], resources=["arn:aws:s3:::bucket/*"]),
            ],
            unauthenticated=[
                AwsPermission(actions=["s3:GetObject"], resources=["arn:aws:s3:::public/*"]),
            ],
        ),
        allow_unauthenticated=True,
    )

    @pulumi.runtime.test
    def deploy():
        return identity.resources

    deploy()

    pulumi_mocks.assert_res(
        "app-identity-unauth-policy",
        R.ROLE_POLICY,
        {"role": tid(f"{TP}app-identity-unauth-role")},
        partial=True,
    )


def test_roles_attachment_created(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client("web")

    identity = IdentityPool(
        "app-identity",
        user_pools=[IdentityPoolBinding(user_pool=pool, client=client)],
    )

    @pulumi.runtime.test
    def deploy():
        return identity.resources

    deploy()

    pulumi_mocks.assert_res(
        "app-identity-roles",
        R.IDENTITY_POOL_ROLE_ATTACHMENT,
        {"identityPoolId": tid(f"{TP}app-identity")},
        partial=True,
    )


def test_roles_attachment_contains_authenticated_role(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client("web")

    identity = IdentityPool(
        "app-identity",
        user_pools=[IdentityPoolBinding(user_pool=pool, client=client)],
    )

    @pulumi.runtime.test
    def deploy():
        return identity.resources

    deploy()

    pulumi_mocks.assert_res(
        "app-identity-roles",
        R.IDENTITY_POOL_ROLE_ATTACHMENT,
        {"roles": {"authenticated": AUTH_ROLE_ARN}},
        partial=True,
    )


def test_roles_attachment_with_both_roles(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client("web")

    identity = IdentityPool(
        "app-identity",
        user_pools=[IdentityPoolBinding(user_pool=pool, client=client)],
        allow_unauthenticated=True,
    )

    @pulumi.runtime.test
    def deploy():
        return identity.resources

    deploy()

    pulumi_mocks.assert_res(
        "app-identity-roles",
        R.IDENTITY_POOL_ROLE_ATTACHMENT,
        {"roles": {"authenticated": AUTH_ROLE_ARN, "unauthenticated": UNAUTH_ROLE_ARN}},
        partial=True,
    )


def test_string_bindings(pulumi_mocks):
    identity = IdentityPool(
        "app-identity",
        user_pools=[
            IdentityPoolBinding(user_pool="us-east-1_abc123", client="client-id-123"),
        ],
    )

    @pulumi.runtime.test
    def deploy():
        return identity.resources

    deploy()

    pulumi_mocks.assert_res(
        "app-identity",
        R.IDENTITY_POOL,
        {
            "cognitoIdentityProviders": [
                {
                    "providerName": "cognito-idp.us-east-1.amazonaws.com/us-east-1_abc123",
                    "clientId": "client-id-123",
                    "serverSideTokenCheck": False,
                }
            ]
        },
        partial=True,
    )


def test_string_binding_provider_name_uses_pool_id_region(pulumi_mocks):
    """When pool ID is from a different region than the app,
    provider_name uses the pool's region."""
    identity = IdentityPool(
        "app-identity",
        user_pools=[
            IdentityPoolBinding(user_pool="eu-west-1_abc123", client="client-id-123"),
        ],
    )

    @pulumi.runtime.test
    def deploy():
        return identity.resources

    deploy()

    pulumi_mocks.assert_res(
        "app-identity",
        R.IDENTITY_POOL,
        {
            "cognitoIdentityProviders": [
                {
                    "providerName": "cognito-idp.eu-west-1.amazonaws.com/eu-west-1_abc123",
                    "clientId": "client-id-123",
                    "serverSideTokenCheck": False,
                }
            ]
        },
        partial=True,
    )


def test_component_binding_provider_name_uses_app_region(pulumi_mocks):
    """UserPool component bindings use the app region for provider_name."""
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client("web")

    identity = IdentityPool(
        "app-identity",
        user_pools=[IdentityPoolBinding(user_pool=pool, client=client)],
    )

    @pulumi.runtime.test
    def deploy():
        return identity.resources

    deploy()

    pulumi_mocks.assert_res(
        "app-identity",
        R.IDENTITY_POOL,
        {
            "cognitoIdentityProviders": [
                {
                    "providerName": "cognito-idp.us-east-1.amazonaws.com/test-test-users-test-id",
                    "clientId": tid(f"{TP}users-web"),
                    "serverSideTokenCheck": False,
                }
            ]
        },
        partial=True,
    )


def test_mixed_component_and_string_bindings(pulumi_mocks):
    """Mix of UserPool component and string bindings resolves each correctly."""
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client("web")

    identity = IdentityPool(
        "app-identity",
        user_pools=[
            IdentityPoolBinding(user_pool=pool, client=client),
            IdentityPoolBinding(user_pool="ap-southeast-1_ext456", client="ext-client"),
        ],
    )

    @pulumi.runtime.test
    def deploy():
        return identity.resources

    deploy()

    pulumi_mocks.assert_res(
        "app-identity",
        R.IDENTITY_POOL,
        {
            "cognitoIdentityProviders": [
                # Component binding uses app region
                {
                    "providerName": "cognito-idp.us-east-1.amazonaws.com/test-test-users-test-id",
                    "clientId": tid(f"{TP}users-web"),
                    "serverSideTokenCheck": False,
                },
                # String binding uses parsed region from pool ID
                {
                    "providerName": (
                        "cognito-idp.ap-southeast-1.amazonaws.com/ap-southeast-1_ext456"
                    ),
                    "clientId": "ext-client",
                    "serverSideTokenCheck": False,
                },
            ]
        },
        partial=True,
    )


def test_identity_pool_customization(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client("web")

    identity = IdentityPool(
        "app-identity",
        user_pools=[IdentityPoolBinding(user_pool=pool, client=client)],
        customize={"identity_pool": {"developer_provider_name": "my-app"}},
    )

    @pulumi.runtime.test
    def deploy():
        return identity.resources

    deploy()

    pulumi_mocks.assert_res(
        "app-identity", R.IDENTITY_POOL, {"developerProviderName": "my-app"}, partial=True
    )


def test_multiple_bindings(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    web_client = pool.add_client("web")
    api_client = pool.add_client("api", generate_secret=True)

    identity = IdentityPool(
        "app-identity",
        user_pools=[
            IdentityPoolBinding(user_pool=pool, client=web_client),
            IdentityPoolBinding(user_pool=pool, client=api_client),
        ],
    )

    @pulumi.runtime.test
    def deploy():
        return identity.resources

    deploy()

    pulumi_mocks.assert_res(
        "app-identity",
        R.IDENTITY_POOL,
        {
            "cognitoIdentityProviders": [
                {
                    "providerName": "cognito-idp.us-east-1.amazonaws.com/test-test-users-test-id",
                    "clientId": tid(f"{TP}users-web"),
                    "serverSideTokenCheck": False,
                },
                {
                    "providerName": "cognito-idp.us-east-1.amazonaws.com/test-test-users-test-id",
                    "clientId": tid(f"{TP}users-api"),
                    "serverSideTokenCheck": False,
                },
            ]
        },
        partial=True,
    )


def test_auth_role_policy_contains_correct_actions(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client("web")

    identity = IdentityPool(
        "app-identity",
        user_pools=[IdentityPoolBinding(user_pool=pool, client=client)],
        permissions=IdentityPoolPermissions(
            authenticated=[
                AwsPermission(
                    actions=["s3:GetObject", "s3:PutObject"],
                    resources=["arn:aws:s3:::my-bucket/*"],
                ),
            ],
        ),
    )

    @pulumi.runtime.test
    def deploy():
        return identity.resources

    deploy()

    policy = pulumi_mocks.assert_res("app-identity-auth-policy", R.ROLE_POLICY)
    assert json.loads(policy.inputs["policy"]) == {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:PutObject"],
                "Resource": ["arn:aws:s3:::my-bucket/*"],
            }
        ],
    }


@pulumi.runtime.test
def test_identity_pool_id_property(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client("web")

    identity = IdentityPool(
        "app-identity",
        user_pools=[IdentityPoolBinding(user_pool=pool, client=client)],
    )

    def check(pool_id):
        expected_id = tid(TP + "app-identity")
        assert pool_id == expected_id

    identity.id.apply(check)


@pulumi.runtime.test
def test_authenticated_role_arn_property(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client("web")

    identity = IdentityPool(
        "app-identity",
        user_pools=[IdentityPoolBinding(user_pool=pool, client=client)],
    )

    def check(arn):
        auth_role_name = TP + "app-identity-auth-role"
        expected_arn = f"arn:aws:iam::{ACCOUNT_ID}:role/{tn(auth_role_name)}"
        assert arn == expected_arn

    identity.authenticated_role_arn.apply(check)


@pulumi.runtime.test
def test_unauthenticated_role_arn_property_when_enabled(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client("web")

    identity = IdentityPool(
        "app-identity",
        user_pools=[IdentityPoolBinding(user_pool=pool, client=client)],
        allow_unauthenticated=True,
    )

    def check(arn):
        unauth_role_name = TP + "app-identity-unauth-role"
        expected_arn = f"arn:aws:iam::{ACCOUNT_ID}:role/{tn(unauth_role_name)}"
        assert arn == expected_arn

    identity.unauthenticated_role_arn.apply(check)


def test_unauthenticated_role_arn_property_none_when_disabled(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client("web")

    identity = IdentityPool(
        "app-identity",
        user_pools=[IdentityPoolBinding(user_pool=pool, client=client)],
    )
    # unauthenticated_role_arn requires resources to be created first
    # but since allow_unauthenticated is False, it returns None
    assert identity.unauthenticated_role_arn is None


def test_config_from_opts(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client("web")

    identity = IdentityPool(
        "app-identity",
        user_pools=[IdentityPoolBinding(user_pool=pool, client=client)],
        allow_unauthenticated=True,
    )

    @pulumi.runtime.test
    def deploy():
        return identity.resources

    deploy()

    pulumi_mocks.assert_res("app-identity", R.IDENTITY_POOL)
    pulumi_mocks.assert_res("app-identity-auth-role", R.ROLE)
    pulumi_mocks.assert_res("app-identity-unauth-role", R.ROLE)
    pulumi_mocks.assert_res_counts(
        {
            R.USER_POOL: 1,
            R.USER_POOL_CLIENT: 1,
            R.IDENTITY_POOL: 1,
            R.ROLE: 2,
            R.IDENTITY_POOL_ROLE_ATTACHMENT: 1,
        }
    )


# =========================================================================
# Config dict parity tests
# =========================================================================


def test_identity_pool_permissions_dict_matches_dataclass():
    """IdentityPoolPermissionsDict has the same fields as IdentityPoolPermissions.

    Can't use assert_config_dict_matches_dataclass because AwsPermission
    forward reference cannot be resolved by get_type_hints() at test time.
    """
    from dataclasses import fields

    dataclass_fields = {f.name for f in fields(IdentityPoolPermissions)}
    typeddict_fields = set(IdentityPoolPermissionsDict.__annotations__.keys())

    assert dataclass_fields == typeddict_fields, (
        f"IdentityPoolPermissionsDict and IdentityPoolPermissions have different fields: "
        f"dataclass={dataclass_fields}, typeddict={typeddict_fields}"
    )


def test_identity_pool_config_dict_matches_dataclass():
    """IdentityPoolConfigDict has the same fields as IdentityPoolConfig.

    Can't use assert_config_dict_matches_dataclass because 'permissions' uses
    a union with IdentityPoolPermissionsDict that differs between dataclass
    (IdentityPoolPermissions | None) and TypedDict, and 'user_pools' uses
    IdentityPoolBindingDict union that get_type_hints() cannot normalize.
    """
    from dataclasses import fields

    dataclass_fields = {f.name for f in fields(IdentityPoolConfig)}
    typeddict_fields = set(IdentityPoolConfigDict.__annotations__.keys())

    assert dataclass_fields == typeddict_fields, (
        f"IdentityPoolConfigDict and IdentityPoolConfig have different fields: "
        f"dataclass={dataclass_fields}, typeddict={typeddict_fields}"
    )


def test_identity_pool_long_name_truncates_at_aws_limit(pulumi_mocks):
    @pulumi.runtime.test
    def deploy():
        pool = UserPool("users", usernames=["email"])
        client = pool.add_client("web")
        return IdentityPool(
            "i" * 150, user_pools=[IdentityPoolBinding(user_pool=pool, client=client)]
        ).resources

    deploy()

    [identity] = pulumi_mocks.created(R.IDENTITY_POOL)
    assert_hash_truncated(identity.inputs["identityPoolName"], 128)  # no Pulumi suffix reserved
    assert identity.name == identity.inputs["identityPoolName"]
