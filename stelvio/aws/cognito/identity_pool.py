from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Unpack, final

import pulumi
import pulumi_aws
from pulumi import Output
from pulumi_aws.iam import RolePolicy

from stelvio import context
from stelvio.aws.cognito.types import (
    IdentityPoolBinding,
    IdentityPoolConfig,
    IdentityPoolConfigDict,
    IdentityPoolCustomizationDict,
)
from stelvio.aws.cognito.user_pool import UserPool
from stelvio.aws.cognito.user_pool_client import UserPoolClient
from stelvio.component import Component, link_config_creator, safe_name
from stelvio.link import LinkableMixin, LinkConfig

if TYPE_CHECKING:
    from stelvio.aws.permission import AwsPermission

MAX_IDENTITY_POOL_NAME_LENGTH = 128
MAX_ROLE_NAME_LENGTH = 64


def _resolve_binding(binding: IdentityPoolBinding) -> dict[str, Any]:
    """Resolve a binding to a Cognito identity provider dict.

    Returns a dict with 'client_id' and 'provider_name' suitable for
    the identity pool's cognito_identity_providers argument.
    """
    if isinstance(binding.client, UserPoolClient):
        client_id = binding.client.client_id
    else:
        client_id = binding.client

    if isinstance(binding.user_pool, UserPool):
        pool_id = binding.user_pool.id
    else:
        pool_id = binding.user_pool

    region = context().aws.region
    provider_name = pulumi.Output.all(region=region, pool_id=pool_id).apply(
        lambda args: f"cognito-idp.{args['region']}.amazonaws.com/{args['pool_id']}"
    )

    return {
        "client_id": client_id,
        "provider_name": provider_name,
        "server_side_token_check": False,
    }


def _build_trust_policy(identity_pool_id: Output[str], *, authenticated: bool) -> Output[str]:
    """Build a Cognito identity trust policy for assuming a role."""
    amr_value = "authenticated" if authenticated else "unauthenticated"

    return identity_pool_id.apply(
        lambda pool_id: json.dumps({
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": "sts:AssumeRoleWithWebIdentity",
                    "Principal": {"Federated": "cognito-identity.amazonaws.com"},
                    "Condition": {
                        "StringEquals": {
                            "cognito-identity.amazonaws.com:aud": pool_id,
                        },
                        "ForAnyValue:StringLike": {
                            "cognito-identity.amazonaws.com:amr": amr_value,
                        },
                    },
                }
            ],
        })
    )


def _build_inline_policy(permissions: list[AwsPermission]) -> Output[str]:
    """Build an inline policy document from a list of AwsPermission."""
    all_resources = []
    resource_counts = []
    for perm in permissions:
        all_resources.extend(perm.resources)
        resource_counts.append(len(perm.resources))

    def _build(resolved: list[str]) -> str:
        statements = []
        offset = 0
        for i, perm in enumerate(permissions):
            count = resource_counts[i]
            statements.append({
                "Effect": "Allow",
                "Action": list(perm.actions),
                "Resource": list(resolved[offset : offset + count]),
            })
            offset += count
        return json.dumps({"Version": "2012-10-17", "Statement": statements})

    return Output.all(*all_resources).apply(_build)


@final
@dataclass(frozen=True)
class IdentityPoolResources:
    identity_pool: pulumi_aws.cognito.IdentityPool
    authenticated_role: pulumi_aws.iam.Role
    authenticated_role_policy: RolePolicy | None
    unauthenticated_role: pulumi_aws.iam.Role | None
    unauthenticated_role_policy: RolePolicy | None
    roles_attachment: pulumi_aws.cognito.IdentityPoolRoleAttachment


@final
class IdentityPool(
    Component[IdentityPoolResources, IdentityPoolCustomizationDict],
    LinkableMixin,
):
    _config: IdentityPoolConfig

    def __init__(
        self,
        name: str,
        /,
        *,
        config: IdentityPoolConfig | IdentityPoolConfigDict | None = None,
        tags: dict[str, str] | None = None,
        customize: IdentityPoolCustomizationDict | None = None,
        **opts: Unpack[IdentityPoolConfigDict],
    ) -> None:
        super().__init__("stelvio:aws:IdentityPool", name, tags=tags, customize=customize)
        self._config = self._parse_config(config, opts)

    @staticmethod
    def _parse_config(
        config: IdentityPoolConfig | IdentityPoolConfigDict | None,
        opts: IdentityPoolConfigDict,
    ) -> IdentityPoolConfig:
        if config and opts:
            raise ValueError(
                "Invalid configuration: cannot combine 'config' parameter "
                "with additional options - provide all settings either in "
                "'config' or as separate options"
            )

        if config is None:
            return IdentityPoolConfig(**opts)
        if isinstance(config, IdentityPoolConfig):
            return config
        if isinstance(config, dict):
            return IdentityPoolConfig(**config)

        raise TypeError(
            f"Invalid config type: expected IdentityPoolConfig or "
            f"IdentityPoolConfigDict, got {type(config).__name__}"
        )

    @property
    def id(self) -> Output[str]:
        return self.resources.identity_pool.id

    @property
    def authenticated_role_arn(self) -> Output[str]:
        return self.resources.authenticated_role.arn

    @property
    def unauthenticated_role_arn(self) -> Output[str] | None:
        if self.resources.unauthenticated_role is None:
            return None
        return self.resources.unauthenticated_role.arn

    def _create_resources(self) -> IdentityPoolResources:
        prefix = context().prefix()

        # 1. Resolve user pool bindings to Cognito provider format
        cognito_providers = [_resolve_binding(binding) for binding in self._config.user_pools]

        # 2. Create the Identity Pool
        identity_pool = pulumi_aws.cognito.IdentityPool(
            safe_name(prefix, self.name, MAX_IDENTITY_POOL_NAME_LENGTH),
            **self._customizer(
                "identity_pool",
                {
                    "identity_pool_name": safe_name(
                        prefix, self.name, MAX_IDENTITY_POOL_NAME_LENGTH
                    ),
                    "allow_unauthenticated_identities": self._config.allow_unauthenticated,
                    "cognito_identity_providers": cognito_providers,
                },
                inject_tags=True,
            ),
            opts=self._resource_opts(),
        )

        # 3. Create authenticated IAM role
        auth_trust_policy = _build_trust_policy(identity_pool.id, authenticated=True)
        authenticated_role = pulumi_aws.iam.Role(
            safe_name(prefix, f"{self.name}-auth-role", MAX_ROLE_NAME_LENGTH),
            **self._customizer(
                "authenticated_role",
                {"assume_role_policy": auth_trust_policy},
            ),
            opts=self._resource_opts(),
        )

        # 4. Create authenticated role policy if permissions specified
        authenticated_role_policy: RolePolicy | None = None
        if self._config.permissions and self._config.permissions.authenticated:
            policy_doc = _build_inline_policy(self._config.permissions.authenticated)
            authenticated_role_policy = RolePolicy(
                safe_name(prefix, f"{self.name}-auth-policy", MAX_ROLE_NAME_LENGTH),
                **self._customizer(
                    "authenticated_role_policy",
                    {
                        "role": authenticated_role.id,
                        "policy": policy_doc,
                    },
                ),
                opts=self._resource_opts(),
            )

        # 5. Create unauthenticated role and policy if enabled
        unauthenticated_role: pulumi_aws.iam.Role | None = None
        unauthenticated_role_policy: RolePolicy | None = None
        if self._config.allow_unauthenticated:
            unauth_trust_policy = _build_trust_policy(identity_pool.id, authenticated=False)
            unauthenticated_role = pulumi_aws.iam.Role(
                safe_name(prefix, f"{self.name}-unauth-role", MAX_ROLE_NAME_LENGTH),
                **self._customizer(
                    "unauthenticated_role",
                    {"assume_role_policy": unauth_trust_policy},
                ),
                opts=self._resource_opts(),
            )

            if self._config.permissions and self._config.permissions.unauthenticated:
                policy_doc = _build_inline_policy(self._config.permissions.unauthenticated)
                unauthenticated_role_policy = RolePolicy(
                    safe_name(prefix, f"{self.name}-unauth-policy", MAX_ROLE_NAME_LENGTH),
                    **self._customizer(
                        "unauthenticated_role_policy",
                        {
                            "role": unauthenticated_role.id,
                            "policy": policy_doc,
                        },
                    ),
                    opts=self._resource_opts(),
                )

        # 6. Build roles mapping and create roles attachment
        roles: dict[str, Output[str]] = {"authenticated": authenticated_role.arn}
        if unauthenticated_role is not None:
            roles["unauthenticated"] = unauthenticated_role.arn

        roles_attachment = pulumi_aws.cognito.IdentityPoolRoleAttachment(
            safe_name(prefix, f"{self.name}-roles", MAX_IDENTITY_POOL_NAME_LENGTH),
            **self._customizer(
                "roles_attachment",
                {
                    "identity_pool_id": identity_pool.id,
                    "roles": roles,
                },
            ),
            opts=self._resource_opts(),
        )

        self.register_outputs({"id": identity_pool.id})
        return IdentityPoolResources(
            identity_pool=identity_pool,
            authenticated_role=authenticated_role,
            authenticated_role_policy=authenticated_role_policy,
            unauthenticated_role=unauthenticated_role,
            unauthenticated_role_policy=unauthenticated_role_policy,
            roles_attachment=roles_attachment,
        )


@link_config_creator(IdentityPool)
def _identity_pool_link_creator(pool: IdentityPool) -> LinkConfig:
    """Link creator for IdentityPool.

    Provides identity pool ID and role ARNs as environment variables.
    No default IAM permissions — Identity Pool is an authorization layer,
    not something Lambdas typically call directly.
    """
    properties: dict[str, Any] = {
        "identity_pool_id": pool.id,
        "authenticated_role_arn": pool.authenticated_role_arn,
    }

    unauth_arn = pool.unauthenticated_role_arn
    if unauth_arn is not None:
        properties["unauthenticated_role_arn"] = unauth_arn

    return LinkConfig(
        properties=properties,
        permissions=[],
    )
