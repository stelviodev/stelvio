from __future__ import annotations

from dataclasses import dataclass
from typing import Any, final

import pulumi_aws
from pulumi import Output

from stelvio import context
from stelvio.aws.cognito.types import IdentityProviderConfig, IdentityProviderCustomizationDict
from stelvio.aws.cognito.user_pool import UserPool  # noqa: TC001
from stelvio.component import Component, safe_name

MAX_IDENTITY_PROVIDER_NAME_LENGTH = 128


@final
@dataclass(frozen=True)
class IdentityProviderResources:
    identity_provider: pulumi_aws.cognito.IdentityProvider


@final
class IdentityProvider(Component[IdentityProviderResources, IdentityProviderCustomizationDict]):
    """Cognito identity provider for federated authentication.

    Represents a configured identity provider (Google, Facebook, OIDC, SAML, etc.)
    attached to a UserPool. Created via UserPool.add_identity_provider().
    """

    _user_pool: UserPool
    _config: IdentityProviderConfig
    _pool_resource: pulumi_aws.cognito.UserPool | None

    def __init__(
        self,
        name: str,
        /,
        *,
        user_pool: UserPool,
        config: IdentityProviderConfig,
        customize: IdentityProviderCustomizationDict | None = None,
    ) -> None:
        super().__init__("stelvio:aws:IdentityProvider", name, customize=customize)
        self._user_pool = user_pool
        self._config = config
        self._pool_resource = None

    @property
    def provider_name(self) -> Output[str]:
        return Output.from_input(self._config.provider_name)

    def _create_resources(self) -> IdentityProviderResources:
        pool = self._pool_resource or self._user_pool.resources.user_pool
        prefix = context().prefix()

        idp_args: dict[str, Any] = {
            "user_pool_id": pool.id,
            "provider_name": self._config.provider_name,
            "provider_type": self._config.provider_type,
            "provider_details": self._config.details,
        }
        if self._config.attributes is not None:
            idp_args["attribute_mapping"] = self._config.attributes

        identity_provider = pulumi_aws.cognito.IdentityProvider(
            safe_name(prefix, self.name, MAX_IDENTITY_PROVIDER_NAME_LENGTH),
            **self._customizer("identity_provider", idp_args),
            opts=self._resource_opts(),
        )

        self.register_outputs({"provider_name": identity_provider.provider_name})
        return IdentityProviderResources(identity_provider=identity_provider)
