from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Unpack, final

import pulumi
import pulumi_aws
from pulumi import Input, Output

from stelvio import context
from stelvio.aws.cognito.types import (
    UserPoolClientConfig,
    UserPoolClientConfigDict,
    UserPoolClientCustomizationDict,
)
from stelvio.aws.cognito.user_pool import UserPool  # noqa: TC001
from stelvio.aws.permission import AwsPermission
from stelvio.component import Component, link_config_creator, safe_name
from stelvio.link import LinkableMixin, LinkConfig

MAX_USER_POOL_CLIENT_NAME_LENGTH = 128


@final
@dataclass(frozen=True)
class UserPoolClientResources:
    client: pulumi_aws.cognito.UserPoolClient


@final
class UserPoolClient(
    Component[UserPoolClientResources, UserPoolClientCustomizationDict],
    LinkableMixin,
):
    _pool: UserPool
    _config: UserPoolClientConfig
    # Set by UserPool._prepare_children() to avoid redundant lazy resource lookups
    # when children are created as a batch. Falls back to self._pool.resources.user_pool.
    _pool_resource: pulumi_aws.cognito.UserPool | None

    def __init__(
        self,
        name: str,
        /,
        *,
        pool: UserPool,
        config: UserPoolClientConfig | UserPoolClientConfigDict | None = None,
        customize: UserPoolClientCustomizationDict | None = None,
        **opts: Unpack[UserPoolClientConfigDict],
    ) -> None:
        super().__init__("stelvio:aws:UserPoolClient", name, customize=customize)
        self._pool = pool
        self._config = self._parse_config(config, opts)
        self._pool_resource = None

    @staticmethod
    def _parse_config(
        config: UserPoolClientConfig | UserPoolClientConfigDict | None,
        opts: UserPoolClientConfigDict,
    ) -> UserPoolClientConfig:
        if config and opts:
            raise ValueError(
                "Invalid configuration: cannot combine 'config' parameter "
                "with additional options - provide all settings either in "
                "'config' or as separate options"
            )

        if config is None:
            return UserPoolClientConfig(**opts)
        if isinstance(config, UserPoolClientConfig):
            return config
        if isinstance(config, dict):
            return UserPoolClientConfig(**config)

        raise TypeError(
            f"Invalid config type: expected UserPoolClientConfig or "
            f"UserPoolClientConfigDict, got {type(config).__name__}"
        )

    @property
    def client_id(self) -> Output[str]:
        return self.resources.client.id

    @property
    def client_secret(self) -> Output[str] | None:
        if not self._config.generate_secret:
            return None
        return self.resources.client.client_secret

    @property
    def pool(self) -> UserPool:
        return self._pool

    @property
    def generate_secret(self) -> bool:
        return self._config.generate_secret

    def _create_resources(self) -> UserPoolClientResources:
        pool = self._pool_resource or self._pool.resources.user_pool
        prefix = context().prefix()
        supported_providers = self._config.providers or ["COGNITO"]

        # Client depends on all identity providers being created first
        idp_depends: list[pulumi.Resource] = [
            idp.resources.identity_provider for idp in self._pool.identity_providers
        ]

        client_args: dict[str, Any] = {
            "name": safe_name(prefix, self.name, MAX_USER_POOL_CLIENT_NAME_LENGTH),
            "user_pool_id": pool.id,
            "generate_secret": self._config.generate_secret,
            "supported_identity_providers": supported_providers,
        }

        # Configure OAuth when callback or logout URLs are present
        if self._config.callback_urls or self._config.logout_urls:
            client_args["callback_urls"] = self._config.callback_urls
            client_args["logout_urls"] = self._config.logout_urls
            client_args["allowed_oauth_flows_user_pool_client"] = True
            client_args["allowed_oauth_flows"] = ["code"]
            client_args["allowed_oauth_scopes"] = ["openid", "email", "profile"]

        client = pulumi_aws.cognito.UserPoolClient(
            safe_name(prefix, self.name, MAX_USER_POOL_CLIENT_NAME_LENGTH),
            **self._customizer("client", client_args),
            opts=self._resource_opts(depends_on=idp_depends or None),
        )

        pulumi.export(f"user_pool_client_{self.name}_id", client.id)
        pulumi.export(f"user_pool_client_{self.name}_user_pool_id", pool.id)

        self.register_outputs({"id": client.id})
        return UserPoolClientResources(client=client)


@link_config_creator(UserPoolClient)
def _user_pool_client_link_creator(client: UserPoolClient) -> LinkConfig:
    """Link creator for UserPoolClient.

    Grants the same user pool permissions as linking the UserPool itself, since client
    operations require access to the parent pool's users. Additionally provides client-specific
    properties like client_id and optional client_secret for authentication flows.
    """
    client_resource = client.resources.client
    pool = client.pool

    properties: dict[str, Input[str]] = {
        "client_id": client_resource.id,
        "user_pool_id": pool.id,
    }

    if client.generate_secret:
        properties["client_secret"] = client_resource.client_secret

    return LinkConfig(
        properties=properties,
        permissions=[
            # Same pool permissions as UserPool link creator - client operations
            # require user pool access (GetUser, AdminGetUser, ListUsers)
            AwsPermission(
                actions=[
                    "cognito-idp:GetUser",
                    "cognito-idp:AdminGetUser",
                    "cognito-idp:ListUsers",
                ],
                resources=[pool.arn],
            ),
        ],
    )
