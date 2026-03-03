from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Unpack, final

import pulumi
import pulumi_aws
from pulumi import Input, Output

from stelvio import context
from stelvio.aws.cognito.types import (
    PROVIDER_TYPE_MAP,
    TRIGGER_CONFIG_MAP,
    IdentityProviderCustomizationDict,
    IdentityProviderType,
    TriggerHandler,
    UserPoolClientCustomizationDict,
    UserPoolConfig,
    UserPoolConfigDict,
    UserPoolCustomizationDict,
)
from stelvio.aws.permission import AwsPermission
from stelvio.component import Component, link_config_creator, safe_name
from stelvio.link import LinkableMixin, LinkConfig
from stelvio.pulumi import normalize_pulumi_args_to_dict

if TYPE_CHECKING:
    from stelvio.aws.function import Function

MAX_USER_POOL_NAME_LENGTH = 128
MAX_USER_POOL_CLIENT_NAME_LENGTH = 128
MAX_IDENTITY_PROVIDER_NAME_LENGTH = 128


@final
@dataclass(frozen=True)
class UserPoolResources:
    user_pool: pulumi_aws.cognito.UserPool
    trigger_functions: dict[str, Function]
    trigger_permissions: dict[str, pulumi_aws.lambda_.Permission]


@final
@dataclass(frozen=True)
class UserPoolClientResources:
    client: pulumi_aws.cognito.UserPoolClient


@final
@dataclass(frozen=True)
class IdentityProviderResources:
    identity_provider: pulumi_aws.cognito.IdentityProvider


@final
class UserPool(
    Component[UserPoolResources, UserPoolCustomizationDict],
    LinkableMixin,
):
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
                "Invalid configuration: cannot combine 'config' parameter "
                "with additional options — provide all settings either in "
                "'config' or as separate options"
            )

        if config is None:
            return UserPoolConfig(**opts)
        if isinstance(config, UserPoolConfig):
            return config
        if isinstance(config, dict):
            return UserPoolConfig(**config)

        raise TypeError(
            f"Invalid config type: expected UserPoolConfig or "
            f"UserPoolConfigDict, got {type(config).__name__}"
        )

    @property
    def arn(self) -> Output[str]:
        return self.resources.user_pool.arn

    @property
    def id(self) -> Output[str]:
        return self.resources.user_pool.id

    @property
    def name_in_aws(self) -> Output[str]:
        return self.resources.user_pool.name

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
        for existing in self._identity_providers:
            if existing._user_name == name:  # noqa: SLF001
                raise ValueError(
                    f"Duplicate identity provider name '{name}' on UserPool '{self.name}'. "
                    "Provider names must be unique within a user pool."
                )

        # Social providers use AWS-standard names; oidc/saml use user-provided name
        cognito_provider_name = name if type in ("oidc", "saml") else PROVIDER_TYPE_MAP[type]

        result = IdentityProviderResult(
            pool=self,
            user_name=name,
            provider_name=cognito_provider_name,
            provider_type=PROVIDER_TYPE_MAP[type],
            details=details,
            attributes=attributes,
            customize=customize,
        )
        self._identity_providers.append(result)
        return result

    def add_client(  # noqa: PLR0913
        self,
        name: str,
        /,
        *,
        callback_urls: list[str] | None = None,
        logout_urls: list[str] | None = None,
        providers: list[Input[str]] | None = None,
        generate_secret: bool = False,
        customize: UserPoolClientCustomizationDict | None = None,
    ) -> UserPoolClient:
        self._check_not_created()
        for existing in self._clients:
            if existing._client_name == name:  # noqa: SLF001
                raise ValueError(
                    f"Duplicate client name '{name}' on UserPool '{self.name}'. "
                    "Client names must be unique within a user pool."
                )
        client = UserPoolClient(
            f"{self.name}-{name}",
            pool=self,
            client_name=name,
            callback_urls=callback_urls,
            logout_urls=logout_urls,
            providers=providers,
            generate_secret=generate_secret,
            customize=customize,
        )
        self._clients.append(client)
        return client

    def _check_not_created(self) -> None:
        if self._resources is not None:
            raise RuntimeError(
                f"Cannot modify UserPool '{self.name}' after resources "
                "have been created. Add all clients and identity providers "
                "before accessing the .resources property."
            )

    def _auto_verified_attributes(self) -> list[str] | None:
        attrs = []
        identifiers = self._config.usernames or self._config.aliases
        if "email" in identifiers:
            attrs.append("email")
        if "phone" in identifiers:
            attrs.append("phone_number")
        return attrs or None

    def _mfa_mode_to_aws(self) -> str:
        return self._config.mfa.upper()

    def _build_password_policy(self) -> dict[str, Any] | None:
        pw = self._config.password
        if pw is None:
            return None
        return {
            "minimum_length": pw.min_length,
            "require_lowercase": pw.require_lowercase,
            "require_uppercase": pw.require_uppercase,
            "require_numbers": pw.require_numbers,
            "require_symbols": pw.require_symbols,
            "temporary_password_validity_days": pw.temporary_password_validity_days,
        }

    def _build_email_config(self) -> dict[str, Any] | None:
        email = self._config.email
        if email is None:
            return None
        identity = email.resources.identity
        return {
            "email_sending_account": "DEVELOPER",
            "source_arn": identity.arn,
            "from_email_address": identity.email_identity,
        }

    def _create_resources(self) -> UserPoolResources:
        prefix = context().prefix()

        # Build username/alias attributes
        username_attributes = self._config.usernames or None
        alias_attributes = self._config.aliases or None
        auto_verified = self._auto_verified_attributes()

        # Build optional configurations
        password_policy = self._build_password_policy()
        email_config = self._build_email_config()

        # MFA configuration
        mfa_configuration = self._mfa_mode_to_aws()
        software_token_mfa = None
        if self._config.software_token:
            software_token_mfa = {"enabled": True}

        # Trigger functions — populated in Step 5
        trigger_functions: dict[str, Function] = {}
        lambda_config: dict[str, Any] | None = None
        if self._config.triggers:
            lambda_config = {}
            for trigger_name, handler in self._config.triggers.items():
                fn = self._create_trigger_function(trigger_name, handler)
                trigger_functions[trigger_name] = fn
                config_key = TRIGGER_CONFIG_MAP[trigger_name]
                lambda_config[config_key] = fn.resources.function.arn

        # Deletion protection
        deletion_protection = "ACTIVE" if self._config.deletion_protection else "INACTIVE"

        pool = pulumi_aws.cognito.UserPool(
            safe_name(prefix, self.name, MAX_USER_POOL_NAME_LENGTH),
            **self._customizer(
                "user_pool",
                {
                    "name": safe_name(prefix, self.name, MAX_USER_POOL_NAME_LENGTH),
                    "username_attributes": username_attributes,
                    "alias_attributes": alias_attributes,
                    "auto_verified_attributes": auto_verified,
                    "mfa_configuration": mfa_configuration,
                    "software_token_mfa_configuration": software_token_mfa,
                    "password_policy": password_policy,
                    "email_configuration": email_config,
                    "lambda_config": lambda_config,
                    "deletion_protection": deletion_protection,
                    "user_pool_tier": self._config.tier.upper(),
                },
            ),
            opts=self._resource_opts(),
        )

        # Create trigger permissions — needs both pool ARN and function
        trigger_permissions: dict[str, pulumi_aws.lambda_.Permission] = {}
        for trigger_name, fn in trigger_functions.items():
            trigger_permissions[trigger_name] = self._create_trigger_permission(
                trigger_name, fn, pool
            )

        # Create identity providers
        for idp_result in self._identity_providers:
            idp_result._create_resource(pool)  # noqa: SLF001

        # Store pool reference on clients so they can build their own resources
        idp_depends: list[pulumi.Resource] = [
            p.resources.identity_provider for p in self._identity_providers
        ]
        for client in self._clients:
            client._pool_resource = pool  # noqa: SLF001
            client._idp_depends = idp_depends  # noqa: SLF001

        pulumi.export(f"user_pool_{self.name}_id", pool.id)
        pulumi.export(f"user_pool_{self.name}_arn", pool.arn)

        self.register_outputs({"id": pool.id, "arn": pool.arn})
        return UserPoolResources(
            user_pool=pool,
            trigger_functions=trigger_functions,
            trigger_permissions=trigger_permissions,
        )

    def _create_trigger_function(self, trigger_name: str, handler: TriggerHandler) -> Function:
        from stelvio.aws.function import Function, FunctionConfig  # noqa: PLC0415

        fn_name = f"{self.name}-trigger-{trigger_name}"
        if isinstance(handler, Function):
            return handler
        if isinstance(handler, str):
            return Function(fn_name, handler=handler)
        if isinstance(handler, FunctionConfig):
            return Function(fn_name, config=handler)
        # dict form (FunctionConfigDict)
        return Function(fn_name, config=handler)

    def _create_trigger_permission(
        self,
        trigger_name: str,
        fn: Function,
        pool: pulumi_aws.cognito.UserPool,
    ) -> pulumi_aws.lambda_.Permission:
        prefix = context().prefix()
        return pulumi_aws.lambda_.Permission(
            safe_name(
                prefix,
                f"{self.name}-trigger-{trigger_name}-perm",
                MAX_USER_POOL_NAME_LENGTH,
            ),
            action="lambda:InvokeFunction",
            function=fn.function_name,
            principal="cognito-idp.amazonaws.com",
            source_arn=pool.arn,
            opts=self._resource_opts(depends_on=[fn.resources.function]),
        )


@final
class UserPoolClient(
    Component[UserPoolClientResources, UserPoolClientCustomizationDict],
    LinkableMixin,
):
    _pool: UserPool
    _client_name: str
    _callback_urls: list[str] | None
    _logout_urls: list[str] | None
    _identity_providers: list[Input[str]] | None
    _generate_secret: bool
    _pool_resource: pulumi_aws.cognito.UserPool | None
    _idp_depends: list[pulumi.Resource]

    def __init__(  # noqa: PLR0913
        self,
        name: str,
        /,
        *,
        pool: UserPool,
        client_name: str,
        callback_urls: list[str] | None = None,
        logout_urls: list[str] | None = None,
        providers: list[Input[str]] | None = None,
        generate_secret: bool = False,
        customize: UserPoolClientCustomizationDict | None = None,
    ) -> None:
        super().__init__("stelvio:aws:UserPoolClient", name, customize=customize)
        self._pool = pool
        self._client_name = client_name
        self._callback_urls = callback_urls
        self._logout_urls = logout_urls
        self._identity_providers = providers
        self._generate_secret = generate_secret
        self._pool_resource = None
        self._idp_depends = []

    @property
    def client_id(self) -> Output[str]:
        return self.resources.client.id

    @property
    def client_secret(self) -> Output[str] | None:
        if not self._generate_secret:
            return None
        return self.resources.client.client_secret

    def _create_resources(self) -> UserPoolClientResources:
        if self._pool_resource is None:
            raise RuntimeError(
                f"UserPoolClient '{self.name}' cannot create resources: "
                "parent UserPool has not been built yet. Ensure the parent "
                "pool's .resources are accessed before the client's."
            )

        prefix = context().prefix()
        pool = self._pool_resource
        supported_providers = self._identity_providers or ["COGNITO"]

        client_args: dict[str, Any] = {
            "name": safe_name(prefix, self.name, MAX_USER_POOL_CLIENT_NAME_LENGTH),
            "user_pool_id": pool.id,
            "generate_secret": self._generate_secret,
            "supported_identity_providers": supported_providers,
        }

        # Configure OAuth when callback or logout URLs are present
        if self._callback_urls or self._logout_urls:
            client_args["callback_urls"] = self._callback_urls
            client_args["logout_urls"] = self._logout_urls
            client_args["allowed_oauth_flows_user_pool_client"] = True
            client_args["allowed_oauth_flows"] = ["code"]
            client_args["allowed_oauth_scopes"] = ["openid", "email", "profile"]

        client = pulumi_aws.cognito.UserPoolClient(
            safe_name(prefix, self.name, MAX_USER_POOL_CLIENT_NAME_LENGTH),
            **self._customizer("client", client_args),
            opts=self._resource_opts(depends_on=self._idp_depends or None),
        )

        self.register_outputs({"id": client.id})
        return UserPoolClientResources(client=client)


class IdentityProviderResult:
    """Lightweight wrapper around a Cognito identity provider.

    Not a full Component — identity providers are only referenced by
    ``provider_name`` when wiring to ``add_client(providers=[...])``.
    """

    _pool: UserPool
    _user_name: str
    _provider_name: str
    _provider_type: str
    _details: dict[str, str]
    _attributes: dict[str, str] | None
    _customize: IdentityProviderCustomizationDict | None
    _resources: IdentityProviderResources | None

    def __init__(  # noqa: PLR0913
        self,
        *,
        pool: UserPool,
        user_name: str,
        provider_name: str,
        provider_type: str,
        details: dict[str, str],
        attributes: dict[str, str] | None = None,
        customize: IdentityProviderCustomizationDict | None = None,
    ) -> None:
        self._pool = pool
        self._user_name = user_name
        self._provider_name = provider_name
        self._provider_type = provider_type
        self._details = details
        self._attributes = attributes
        self._customize = customize
        self._resources = None

    @property
    def provider_name(self) -> Output[str]:
        if self._resources is not None:
            return self._resources.identity_provider.provider_name
        return Output.from_input(self._provider_name)

    @property
    def resources(self) -> IdentityProviderResources:
        if self._resources is None:
            raise RuntimeError(
                "IdentityProviderResult resources are not available yet. "
                "They are created when the parent UserPool's .resources are accessed."
            )
        return self._resources

    def _create_resource(self, pool: pulumi_aws.cognito.UserPool) -> None:
        prefix = context().prefix()
        idp_name = safe_name(
            prefix,
            f"{self._pool.name}-idp-{self._provider_name}",
            MAX_IDENTITY_PROVIDER_NAME_LENGTH,
        )

        default_args: dict[str, Any] = {
            "user_pool_id": pool.id,
            "provider_name": self._provider_name,
            "provider_type": self._provider_type,
            "provider_details": self._details,
        }
        if self._attributes is not None:
            default_args["attribute_mapping"] = self._attributes

        # Apply customization (shallow merge, same pattern as Component._customizer)
        customize_overrides = normalize_pulumi_args_to_dict(
            (self._customize or {}).get("identity_provider")
        )
        final_args = {**default_args, **customize_overrides}

        identity_provider = pulumi_aws.cognito.IdentityProvider(
            idp_name,
            **final_args,
            opts=pulumi.ResourceOptions(
                parent=self._pool,
                aliases=[pulumi.Alias(parent=pulumi.ROOT_STACK_RESOURCE)],
            ),
        )
        self._resources = IdentityProviderResources(identity_provider=identity_provider)


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
    pool = client._pool  # noqa: SLF001

    properties: dict[str, Input[str]] = {
        "client_id": client_resource.id,
        "user_pool_id": pool.id,
    }

    if client._generate_secret:  # noqa: SLF001
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
