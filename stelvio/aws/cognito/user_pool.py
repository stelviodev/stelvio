from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Unpack, final

import pulumi
from pulumi import Input, Output
from pulumi_aws import cognito, lambda_

from stelvio import context
from stelvio.aws.permission import AwsPermission
from stelvio.component import Component, link_config_creator, safe_name
from stelvio.link import LinkableMixin, LinkConfig
from stelvio.pulumi import normalize_pulumi_args_to_dict

from .types import (
    TRIGGER_CONFIG_MAP,
    IdentityProviderCustomizationDict,
    IdentityProviderType,
    PasswordPolicy,
    TriggerHandler,
    UserPoolClientCustomizationDict,
    UserPoolConfig,
    UserPoolConfigDict,
    UserPoolCustomizationDict,
)

if TYPE_CHECKING:
    from stelvio.aws.function import Function


PROVIDER_TYPE_MAP: dict[IdentityProviderType, str] = {
    "google": "Google",
    "facebook": "Facebook",
    "apple": "SignInWithApple",
    "amazon": "LoginWithAmazon",
    "oidc": "OIDC",
    "saml": "SAML",
}


@final
@dataclass(frozen=True)
class UserPoolResources:
    user_pool: cognito.UserPool
    trigger_functions: dict[str, Function]
    trigger_permissions: dict[str, lambda_.Permission]


@final
@dataclass(frozen=True)
class UserPoolClientResources:
    client: cognito.UserPoolClient


@final
class UserPoolClient(
    Component[UserPoolClientResources, UserPoolClientCustomizationDict], LinkableMixin
):
    _pool: UserPool
    _client_name: str
    _generate_secret: bool
    _callback_urls: list[str] | None
    _logout_urls: list[str] | None
    _supported_identity_providers: list[Input[str]] | None

    def __init__(  # noqa: PLR0913
        self,
        pool: UserPool,
        client_name: str,
        *,
        generate_secret: bool = False,
        callback_urls: list[str] | None = None,
        logout_urls: list[str] | None = None,
        providers: list[Input[str]] | None = None,
        customize: UserPoolClientCustomizationDict | None = None,
    ) -> None:
        super().__init__(
            "stelvio:aws:UserPoolClient",
            f"{pool.name}-{client_name}",
            customize=customize,
        )
        self._pool = pool
        self._client_name = client_name
        self._generate_secret = generate_secret
        self._callback_urls = callback_urls
        self._logout_urls = logout_urls
        self._supported_identity_providers = providers

    def _create_resources(self) -> UserPoolClientResources:
        raise RuntimeError(
            "UserPoolClient resources are created by UserPool during deployment. "
            "Do not access UserPoolClient.resources before UserPool.resources has been created."
        )

    def _create_resource(
        self,
        pool: cognito.UserPool,
        depends_on: list[pulumi.Resource] | None = None,
    ) -> None:
        has_oauth = bool(self._callback_urls or self._logout_urls)
        client = cognito.UserPoolClient(
            context().prefix(self.name),
            **self._customizer(
                "client",
                {
                    "name": self._client_name,
                    "user_pool_id": pool.id,
                    "generate_secret": self._generate_secret,
                    "callback_urls": self._callback_urls,
                    "logout_urls": self._logout_urls,
                    "supported_identity_providers": self._supported_identity_providers
                    or ["COGNITO"],
                    "allowed_oauth_flows_user_pool_client": True if has_oauth else None,
                    "allowed_oauth_flows": ["code"] if has_oauth else None,
                    "allowed_oauth_scopes": ["openid", "email", "profile"] if has_oauth else None,
                },
            ),
            opts=self._resource_opts(depends_on=depends_on),
        )
        self._resources = UserPoolClientResources(client=client)

    def create_resource(
        self,
        pool: cognito.UserPool,
        depends_on: list[pulumi.Resource] | None = None,
    ) -> None:
        self._create_resource(pool, depends_on=depends_on)

    @property
    def client_name(self) -> str:
        return self._client_name

    @property
    def pool(self) -> UserPool:
        return self._pool

    @property
    def generate_secret(self) -> bool:
        return self._generate_secret

    @property
    def client_id(self) -> Output[str]:
        return self.resources.client.id

    @property
    def client_secret(self) -> Output[str] | None:
        return self.resources.client.client_secret if self._generate_secret else None


@dataclass
class IdentityProviderResult:
    _pool: UserPool
    _provider_name: str
    _type: IdentityProviderType
    _details: dict[str, str]
    _attributes: dict[str, str] | None
    _customize: IdentityProviderCustomizationDict | None
    _resource: cognito.IdentityProvider | None = field(default=None, init=False)

    @property
    def provider_name(self) -> Output[str]:
        if self._resource:
            return self._resource.provider_name
        return Output.from_input(self._resolved_provider_name())

    @property
    def name(self) -> str:
        return self._provider_name

    @property
    def resource(self) -> cognito.IdentityProvider | None:
        return self._resource

    def _resolved_provider_name(self) -> str:
        if self._type in {"oidc", "saml"}:
            return self._provider_name
        return PROVIDER_TYPE_MAP[self._type]

    def create_resource(self, pool: cognito.UserPool) -> None:
        customization = {}
        if self._customize:
            customization = normalize_pulumi_args_to_dict(self._customize.get("identity_provider"))

        self._resource = cognito.IdentityProvider(
            context().prefix(f"{self._pool.name}-{self._provider_name}"),
            **{
                "user_pool_id": pool.id,
                "provider_name": self._resolved_provider_name(),
                "provider_type": PROVIDER_TYPE_MAP[self._type],
                "provider_details": self._details,
                "attribute_mapping": self._attributes,
                **customization,
            },
            opts=pulumi.ResourceOptions(
                parent=self._pool,
                aliases=[pulumi.Alias(parent=pulumi.ROOT_STACK_RESOURCE)],
            ),
        )


@final
class UserPool(Component[UserPoolResources, UserPoolCustomizationDict], LinkableMixin):
    _config: UserPoolConfig
    _clients: list[UserPoolClient]
    _identity_providers: list[IdentityProviderResult]

    def __init__(
        self,
        name: str,
        /,
        *,
        config: UserPoolConfig | UserPoolConfigDict | None = None,
        customize: UserPoolCustomizationDict | None = None,
        **opts: Unpack[UserPoolConfigDict],
    ) -> None:
        super().__init__("stelvio:aws:UserPool", name, customize=customize)
        self._config = self._parse_config(config, opts)
        self._clients = []
        self._identity_providers = []

    @staticmethod
    def _parse_config(
        config: UserPoolConfig | UserPoolConfigDict | None,
        opts: UserPoolConfigDict,
    ) -> UserPoolConfig:
        if config and opts:
            raise ValueError(
                "Invalid configuration: cannot combine 'config' parameter with additional "
                "options - provide all settings either in 'config' or as separate options"
            )

        if config is None:
            return UserPoolConfig(**opts)

        if isinstance(config, UserPoolConfig):
            return config

        if isinstance(config, dict):
            return UserPoolConfig(**config)

        raise TypeError(
            f"Invalid config type: expected UserPoolConfig or UserPoolConfigDict, "
            f"got {type(config).__name__}"
        )

    def _check_not_created(self) -> None:
        if self._resources is not None:
            raise RuntimeError(
                f"Cannot modify UserPool '{self.name}' after resources have been created. "
                "Configure clients and identity providers before accessing the .resources "
                "property."
            )

    def _create_resources(self) -> UserPoolResources:
        password_policy = self._build_password_policy()
        email_configuration = self._build_email_config()
        pool_name = context().prefix(self.name)

        trigger_functions: dict[str, Function] = {}
        lambda_config_args: dict[str, Input[str]] = {}
        if self._config.triggers:
            for trigger_name, handler in self._config.triggers.items():
                fn = self._create_trigger_function(trigger_name, handler)
                trigger_functions[trigger_name] = fn
                config_key = TRIGGER_CONFIG_MAP[trigger_name]
                lambda_config_args[config_key] = fn.resources.function.arn

        user_pool = cognito.UserPool(
            pool_name,
            **self._customizer(
                "user_pool",
                {
                    "name": pool_name,
                    "username_attributes": self._config.usernames or None,
                    "alias_attributes": self._config.aliases or None,
                    "auto_verified_attributes": self._auto_verified_attributes(),
                    "mfa_configuration": self._mfa_mode_to_aws(),
                    "software_token_mfa_configuration": {
                        "enabled": self._config.software_token,
                    },
                    "password_policy": password_policy,
                    "email_configuration": email_configuration,
                    "lambda_config": lambda_config_args or None,
                    "deletion_protection": (
                        "ACTIVE" if self._config.deletion_protection else "INACTIVE"
                    ),
                    "user_pool_tier": self._config.tier.upper(),
                },
            ),
            opts=self._resource_opts(),
        )

        trigger_permissions: dict[str, lambda_.Permission] = {}
        for trigger_name, fn in trigger_functions.items():
            trigger_permissions[trigger_name] = self._create_trigger_permission(
                trigger_name,
                fn,
                user_pool,
            )

        for provider_result in self._identity_providers:
            provider_result.create_resource(user_pool)

        for client in self._clients:
            depends_on = [
                provider.resource for provider in self._identity_providers if provider.resource
            ]
            client.create_resource(user_pool, depends_on=depends_on)

        self.register_outputs(
            {
                "user_pool_arn": user_pool.arn,
                "user_pool_id": user_pool.id,
                "user_pool_name": user_pool.name,
            }
        )
        pulumi.export(f"userpool_{self.name}_arn", user_pool.arn)
        pulumi.export(f"userpool_{self.name}_id", user_pool.id)
        pulumi.export(f"userpool_{self.name}_name", user_pool.name)

        return UserPoolResources(
            user_pool=user_pool,
            trigger_functions=trigger_functions,
            trigger_permissions=trigger_permissions,
        )

    def _create_trigger_function(self, trigger_name: str, handler: TriggerHandler) -> Function:
        from stelvio.aws.function import Function, FunctionConfig  # noqa: PLC0415

        if isinstance(handler, Function):
            return handler
        if isinstance(handler, str):
            return Function(f"{self.name}-trigger-{trigger_name}", handler=handler)
        if isinstance(handler, FunctionConfig):
            return Function(f"{self.name}-trigger-{trigger_name}", config=handler)
        return Function(f"{self.name}-trigger-{trigger_name}", config=handler)

    def _create_trigger_permission(
        self,
        trigger_name: str,
        fn: Function,
        pool: cognito.UserPool,
    ) -> lambda_.Permission:
        return lambda_.Permission(
            safe_name(context().prefix(), f"{self.name}-trigger-{trigger_name}-perm", 128),
            action="lambda:InvokeFunction",
            function=fn.function_name,
            principal="cognito-idp.amazonaws.com",
            source_arn=pool.arn,
            opts=self._resource_opts(depends_on=[fn.resources.function]),
        )

    def _build_password_policy(self) -> dict[str, Input[int | bool]]:
        policy = (
            self._config.password if isinstance(self._config.password, PasswordPolicy) else None
        )
        resolved = policy or PasswordPolicy()
        return {
            "minimum_length": resolved.min_length,
            "require_lowercase": resolved.require_lowercase,
            "require_uppercase": resolved.require_uppercase,
            "require_numbers": resolved.require_numbers,
            "require_symbols": resolved.require_symbols,
            "temporary_password_validity_days": resolved.temporary_password_validity_days,
        }

    def _build_email_config(self) -> dict[str, Input[str]] | None:
        if not self._config.email:
            return None

        return {
            "email_sending_account": "DEVELOPER",
            "source_arn": self._config.email.resources.identity.arn,
        }

    def _auto_verified_attributes(self) -> list[str] | None:
        verified_attrs: list[str] = []
        combined = {*self._config.usernames, *self._config.aliases}

        if "email" in combined:
            verified_attrs.append("email")
        if "phone" in combined:
            verified_attrs.append("phone_number")

        return verified_attrs or None

    def _mfa_mode_to_aws(self) -> str:
        mode_map = {
            "off": "OFF",
            "optional": "OPTIONAL",
            "on": "ON",
        }
        return mode_map[self._config.mfa]

    def add_client(  # noqa: PLR0913
        self,
        name: str,
        *,
        generate_secret: bool = False,
        callback_urls: list[str] | None = None,
        logout_urls: list[str] | None = None,
        providers: list[Input[str]] | None = None,
        customize: UserPoolClientCustomizationDict | None = None,
    ) -> UserPoolClient:
        self._check_not_created()

        if any(client.client_name == name for client in self._clients):
            raise ValueError(f"A client named '{name}' already exists in user pool '{self.name}'")

        client = UserPoolClient(
            self,
            name,
            generate_secret=generate_secret,
            callback_urls=callback_urls,
            logout_urls=logout_urls,
            providers=providers,
            customize=customize,
        )
        self._clients.append(client)
        return client

    def add_identity_provider(
        self,
        name: str,
        /,
        *,
        type: IdentityProviderType,  # noqa: A002
        details: dict[str, str],
        attributes: dict[str, str] | None = None,
        customize: IdentityProviderCustomizationDict | None = None,
    ) -> IdentityProviderResult:
        self._check_not_created()

        if any(provider.name == name for provider in self._identity_providers):
            raise ValueError(
                f"An identity provider named '{name}' already exists in user pool '{self.name}'"
            )

        provider_result = IdentityProviderResult(
            _pool=self,
            _provider_name=name,
            _type=type,
            _details=details,
            _attributes=attributes,
            _customize=customize,
        )
        self._identity_providers.append(provider_result)
        return provider_result

    @property
    def arn(self) -> Output[str]:
        return self.resources.user_pool.arn

    @property
    def id(self) -> Output[str]:
        return self.resources.user_pool.id

    @property
    def name_in_aws(self) -> Output[str]:
        return self.resources.user_pool.name


@link_config_creator(UserPool)
def _user_pool_link_creator(pool: UserPool) -> LinkConfig:
    user_pool = pool.resources.user_pool
    return LinkConfig(
        properties={
            "user_pool_id": user_pool.id,
            "user_pool_arn": user_pool.arn,
        },
        permissions=[
            AwsPermission(
                actions=[
                    "cognito-idp:GetUser",
                    "cognito-idp:AdminGetUser",
                    "cognito-idp:ListUsers",
                ],
                resources=[user_pool.arn],
            ),
        ],
    )


@link_config_creator(UserPoolClient)
def _user_pool_client_link_creator(client: UserPoolClient) -> LinkConfig:
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
