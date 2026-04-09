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
from ..pulumi_mocks import ACCOUNT_ID, tid, tn

# =========================================================================
# Config validation tests (no Pulumi mocks needed)
# =========================================================================


def test_empty_user_pools_rejection():
    with pytest.raises(ValueError, match="user_pools must contain at least one binding"):
        IdentityPoolConfig(user_pools=[])


def test_unauthenticated_permissions_without_flag_rejection():
    with pytest.raises(ValueError, match="allow_unauthenticated=True"):
        IdentityPoolConfig(
            user_pools=[IdentityPoolBinding(user_pool="pool-id", client="client-id")],
            permissions=IdentityPoolPermissions(
                unauthenticated=[
                    AwsPermission(actions=["s3:GetObject"], resources=["*"]),
                ]
            ),
        )


def test_unauthenticated_permissions_with_flag():
    config = IdentityPoolConfig(
        user_pools=[IdentityPoolBinding(user_pool="pool-id", client="client-id")],
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
    with pytest.raises(ValueError, match="cannot combine"):
        IdentityPool._parse_config(
            config=IdentityPoolConfig(
                user_pools=[IdentityPoolBinding(user_pool="pool-id", client="client-id")]
            ),
            opts={"user_pools": [{"user_pool": "pool-id", "client": "client-id"}]},
        )


def test_valid_config_from_dataclass():
    config = IdentityPoolConfig(
        user_pools=[IdentityPoolBinding(user_pool="pool-id", client="client-id")],
    )
    assert len(config.user_pools) == 1
    assert config.allow_unauthenticated is False
    assert config.permissions is None


def test_binding_normalization_from_dict():
    config = IdentityPoolConfig(
        user_pools=[{"user_pool": "pool-id", "client": "client-id"}],
    )
    assert isinstance(config.user_pools[0], IdentityPoolBinding)
    assert config.user_pools[0].user_pool == "pool-id"
    assert config.user_pools[0].client == "client-id"


def test_permissions_normalization_from_dict():
    config = IdentityPoolConfig(
        user_pools=[IdentityPoolBinding(user_pool="pool-id", client="client-id")],
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
                IdentityPoolBinding(user_pool="pool-id", client="client-id"),
                IdentityPoolBinding(user_pool="pool-id", client="client-id"),
            ],
        )


def test_duplicate_bindings_different_clients_allowed():
    config = IdentityPoolConfig(
        user_pools=[
            IdentityPoolBinding(user_pool="pool-id", client="client-a"),
            IdentityPoolBinding(user_pool="pool-id", client="client-b"),
        ],
    )
    assert len(config.user_pools) == 2


def test_default_permissions():
    config = IdentityPoolConfig(
        user_pools=[IdentityPoolBinding(user_pool="pool-id", client="client-id")],
    )
    assert config.permissions is None


def test_allow_unauthenticated_default():
    config = IdentityPoolConfig(
        user_pools=[IdentityPoolBinding(user_pool="pool-id", client="client-id")],
    )
    assert config.allow_unauthenticated is False


# =========================================================================
# Resource creation tests (with Pulumi mocks)
# =========================================================================


@pulumi.runtime.test
def test_basic_identity_pool_creation(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client("web")

    identity = IdentityPool(
        "app-identity",
        user_pools=[IdentityPoolBinding(user_pool=pool, client=client)],
    )

    def check(_):
        pools = pulumi_mocks.created_identity_pools()
        assert len(pools) == 1
        assert pools[0].typ == "aws:cognito/identityPool:IdentityPool"

    identity.id.apply(check)


@pulumi.runtime.test
def test_identity_pool_naming(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client("web")

    identity = IdentityPool(
        "app-identity",
        user_pools=[IdentityPoolBinding(user_pool=pool, client=client)],
    )

    def check(_):
        mock = pulumi_mocks.assert_identity_pool_created(TP + "app-identity")
        assert mock.inputs["identityPoolName"] == TP + "app-identity"

    identity.id.apply(check)


@pulumi.runtime.test
def test_cognito_identity_providers(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client("web")

    identity = IdentityPool(
        "app-identity",
        user_pools=[IdentityPoolBinding(user_pool=pool, client=client)],
    )

    def check(_):
        mock = pulumi_mocks.assert_identity_pool_created(TP + "app-identity")
        providers = mock.inputs["cognitoIdentityProviders"]
        assert len(providers) == 1
        provider = providers[0]
        assert "clientId" in provider
        assert "providerName" in provider
        assert provider["serverSideTokenCheck"] is False

    identity.authenticated_role_arn.apply(check)


@pulumi.runtime.test
def test_allow_unauthenticated_enabled(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client("web")

    identity = IdentityPool(
        "app-identity",
        user_pools=[IdentityPoolBinding(user_pool=pool, client=client)],
        allow_unauthenticated=True,
    )

    def check(_):
        mock = pulumi_mocks.assert_identity_pool_created(TP + "app-identity")
        assert mock.inputs["allowUnauthenticatedIdentities"] is True

    identity.id.apply(check)


@pulumi.runtime.test
def test_allow_unauthenticated_disabled_by_default(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client("web")

    identity = IdentityPool(
        "app-identity",
        user_pools=[IdentityPoolBinding(user_pool=pool, client=client)],
    )

    def check(_):
        mock = pulumi_mocks.assert_identity_pool_created(TP + "app-identity")
        assert mock.inputs["allowUnauthenticatedIdentities"] is False

    identity.id.apply(check)


@pulumi.runtime.test
def test_authenticated_role_created(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client("web")

    identity = IdentityPool(
        "app-identity",
        user_pools=[IdentityPoolBinding(user_pool=pool, client=client)],
    )

    def check(_):
        auth_role = pulumi_mocks.assert_role_created(TP + "app-identity-auth-role")
        assert auth_role.typ == "aws:iam/role:Role"

    identity.authenticated_role_arn.apply(check)


@pulumi.runtime.test
def test_authenticated_role_trust_policy(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client("web")

    identity = IdentityPool(
        "app-identity",
        user_pools=[IdentityPoolBinding(user_pool=pool, client=client)],
    )

    def check(_):
        auth_role = pulumi_mocks.assert_role_created(TP + "app-identity-auth-role")
        trust_policy = auth_role.inputs["assumeRolePolicy"]
        policy_doc = json.loads(trust_policy)
        assert policy_doc["Version"] == "2012-10-17"
        assert len(policy_doc["Statement"]) == 1
        stmt = policy_doc["Statement"][0]
        assert stmt["Action"] == "sts:AssumeRoleWithWebIdentity"
        assert stmt["Principal"] == {"Federated": "cognito-identity.amazonaws.com"}
        condition = stmt["Condition"]
        assert "StringEquals" in condition
        assert "cognito-identity.amazonaws.com:aud" in condition["StringEquals"]
        assert "ForAnyValue:StringLike" in condition
        assert (
            condition["ForAnyValue:StringLike"]["cognito-identity.amazonaws.com:amr"]
            == "authenticated"
        )

    identity.authenticated_role_arn.apply(check)


@pulumi.runtime.test
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

    def check(_):
        policy = pulumi_mocks.assert_role_policy_created(TP + "app-identity-auth-policy")
        assert policy.typ == "aws:iam/rolePolicy:RolePolicy"
        # Role should reference the auth role
        auth_role_name = TP + "app-identity-auth-role"
        assert policy.inputs["role"] == tid(auth_role_name)

    identity.resources.roles_attachment.identity_pool_id.apply(check)


@pulumi.runtime.test
def test_no_auth_role_policy_when_no_permissions(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client("web")

    identity = IdentityPool(
        "app-identity",
        user_pools=[IdentityPoolBinding(user_pool=pool, client=client)],
    )

    def check(_):
        policies = pulumi_mocks.created_role_policies()
        # No role policies should be created
        identity_policies = [p for p in policies if "app-identity" in p.name]
        assert len(identity_policies) == 0

    identity.authenticated_role_arn.apply(check)


@pulumi.runtime.test
def test_unauthenticated_role_created_when_enabled(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client("web")

    identity = IdentityPool(
        "app-identity",
        user_pools=[IdentityPoolBinding(user_pool=pool, client=client)],
        allow_unauthenticated=True,
    )

    def check(_):
        unauth_role = pulumi_mocks.assert_role_created(TP + "app-identity-unauth-role")
        assert unauth_role.typ == "aws:iam/role:Role"

    identity.unauthenticated_role_arn.apply(check)


@pulumi.runtime.test
def test_unauthenticated_role_skipped_when_disabled(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client("web")

    identity = IdentityPool(
        "app-identity",
        user_pools=[IdentityPoolBinding(user_pool=pool, client=client)],
    )

    def check(_):
        roles = pulumi_mocks.created_roles()
        unauth_roles = [r for r in roles if "unauth-role" in r.name]
        assert len(unauth_roles) == 0

    identity.authenticated_role_arn.apply(check)


@pulumi.runtime.test
def test_unauthenticated_role_trust_policy(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client("web")

    identity = IdentityPool(
        "app-identity",
        user_pools=[IdentityPoolBinding(user_pool=pool, client=client)],
        allow_unauthenticated=True,
    )

    def check(_):
        unauth_role = pulumi_mocks.assert_role_created(TP + "app-identity-unauth-role")
        trust_policy = unauth_role.inputs["assumeRolePolicy"]
        policy_doc = json.loads(trust_policy)
        stmt = policy_doc["Statement"][0]
        assert (
            stmt["Condition"]["ForAnyValue:StringLike"]["cognito-identity.amazonaws.com:amr"]
            == "unauthenticated"
        )

    identity.unauthenticated_role_arn.apply(check)


@pulumi.runtime.test
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

    def check(_):
        policy = pulumi_mocks.assert_role_policy_created(TP + "app-identity-unauth-policy")
        assert policy.typ == "aws:iam/rolePolicy:RolePolicy"
        unauth_role_name = TP + "app-identity-unauth-role"
        assert policy.inputs["role"] == tid(unauth_role_name)

    pulumi.Output.all(
        identity.resources.roles_attachment.identity_pool_id,
        identity.resources.unauthenticated_role_policy.name,
    ).apply(check)


@pulumi.runtime.test
def test_roles_attachment_created(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client("web")

    identity = IdentityPool(
        "app-identity",
        user_pools=[IdentityPoolBinding(user_pool=pool, client=client)],
    )

    def check(_):
        attachment = pulumi_mocks.assert_roles_attachment_created(TP + "app-identity-roles")
        assert (
            attachment.typ == "aws:cognito/identityPoolRoleAttachment:IdentityPoolRoleAttachment"
        )
        # Should reference the identity pool
        pool_name = TP + "app-identity"
        assert attachment.inputs["identityPoolId"] == tid(pool_name)

    identity.resources.roles_attachment.identity_pool_id.apply(check)


@pulumi.runtime.test
def test_roles_attachment_contains_authenticated_role(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client("web")

    identity = IdentityPool(
        "app-identity",
        user_pools=[IdentityPoolBinding(user_pool=pool, client=client)],
    )

    def check(_):
        attachment = pulumi_mocks.assert_roles_attachment_created(TP + "app-identity-roles")
        roles = attachment.inputs["roles"]
        assert "authenticated" in roles
        auth_role_name = TP + "app-identity-auth-role"
        assert roles["authenticated"] == f"arn:aws:iam::{ACCOUNT_ID}:role/{tn(auth_role_name)}"

    identity.resources.roles_attachment.identity_pool_id.apply(check)


@pulumi.runtime.test
def test_roles_attachment_with_both_roles(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client("web")

    identity = IdentityPool(
        "app-identity",
        user_pools=[IdentityPoolBinding(user_pool=pool, client=client)],
        allow_unauthenticated=True,
    )

    def check(_):
        attachment = pulumi_mocks.assert_roles_attachment_created(TP + "app-identity-roles")
        roles = attachment.inputs["roles"]
        assert "authenticated" in roles
        assert "unauthenticated" in roles

    identity.resources.roles_attachment.identity_pool_id.apply(check)


@pulumi.runtime.test
def test_string_bindings(pulumi_mocks):
    identity = IdentityPool(
        "app-identity",
        user_pools=[
            IdentityPoolBinding(user_pool="us-east-1_abc123", client="client-id-123"),
        ],
    )

    def check(_):
        mock = pulumi_mocks.assert_identity_pool_created(TP + "app-identity")
        providers = mock.inputs["cognitoIdentityProviders"]
        assert len(providers) == 1
        assert providers[0]["clientId"] == "client-id-123"

    identity.authenticated_role_arn.apply(check)


@pulumi.runtime.test
def test_identity_pool_customization(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client("web")

    identity = IdentityPool(
        "app-identity",
        user_pools=[IdentityPoolBinding(user_pool=pool, client=client)],
        customize={"identity_pool": {"developer_provider_name": "my-app"}},
    )

    def check(_):
        mock = pulumi_mocks.assert_identity_pool_created(TP + "app-identity")
        assert mock.inputs["developerProviderName"] == "my-app"

    identity.id.apply(check)


@pulumi.runtime.test
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

    def check(_):
        mock = pulumi_mocks.assert_identity_pool_created(TP + "app-identity")
        providers = mock.inputs["cognitoIdentityProviders"]
        assert len(providers) == 2

    identity.id.apply(check)


@pulumi.runtime.test
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

    def check(_):
        policy = pulumi_mocks.assert_role_policy_created(TP + "app-identity-auth-policy")
        policy_doc = json.loads(policy.inputs["policy"])
        assert policy_doc["Version"] == "2012-10-17"
        assert len(policy_doc["Statement"]) == 1
        stmt = policy_doc["Statement"][0]
        assert "s3:GetObject" in stmt["Action"]
        assert "s3:PutObject" in stmt["Action"]
        assert "arn:aws:s3:::my-bucket/*" in stmt["Resource"]

    identity.resources.roles_attachment.identity_pool_id.apply(check)


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


@pulumi.runtime.test
def test_config_from_opts(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])
    client = pool.add_client("web")

    identity = IdentityPool(
        "app-identity",
        user_pools=[IdentityPoolBinding(user_pool=pool, client=client)],
        allow_unauthenticated=True,
    )

    def check(_):
        pools = pulumi_mocks.created_identity_pools()
        assert len(pools) == 1
        # Verify both auth and unauth roles exist
        roles = pulumi_mocks.created_roles()
        identity_roles = [r for r in roles if "app-identity" in r.name]
        assert len(identity_roles) == 2

    identity.unauthenticated_role_arn.apply(check)


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
