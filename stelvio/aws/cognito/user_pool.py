from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Unpack, final

import pulumi_aws

from stelvio import context
from stelvio.aws.acm import AcmValidatedDomain
from stelvio.aws.cognito.types import (
    PROVIDER_TYPE_MAP,
    IdentityProviderConfig,
    IdentityProviderCustomizationDict,
    IdentityProviderType,
    TriggerHandler,
    UserPoolClientConfig,
    UserPoolClientConfigDict,
    UserPoolClientCustomizationDict,
    UserPoolConfig,
    UserPoolConfigDict,
    UserPoolCustomizationDict,
)
from stelvio.aws.permission import AwsPermission
from stelvio.component import Component, link_config_creator, safe_name
from stelvio.dns import DnsProviderNotConfiguredError, Record
from stelvio.link import LinkableMixin, LinkConfig

if TYPE_CHECKING:
    from pulumi import Output

    from stelvio.aws.cognito.identity_provider import IdentityProvider
    from stelvio.aws.cognito.user_pool_client import UserPoolClient
    from stelvio.aws.function import Function

MAX_USER_POOL_NAME_LENGTH = 128


def _auto_verified_attributes(config: UserPoolConfig) -> list[str] | None:
    attrs = []
    identifiers = config.usernames or config.aliases
    if "email" in identifiers:
        attrs.append("email")
    if "phone" in identifiers:
        attrs.append("phone_number")
    return attrs or None


def _build_password_policy(config: UserPoolConfig) -> dict[str, Any] | None:
    pw = config.password
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


def _build_email_config(config: UserPoolConfig) -> dict[str, Any] | None:
    email = config.email
    if email is None:
        return None
    identity = email.resources.identity
    return {
        "email_sending_account": "DEVELOPER",
        "source_arn": identity.arn,
        "from_email_address": identity.email_identity,
    }


@final
@dataclass(frozen=True)
class UserPoolResources:
    user_pool: pulumi_aws.cognito.UserPool
    trigger_functions: dict[str, Function]
    trigger_permissions: dict[str, pulumi_aws.lambda_.Permission]
    user_pool_domain: pulumi_aws.cognito.UserPoolDomain | None = None
    acm_validated_domain: AcmValidatedDomain | None = None
    domain_record: Record | None = None


@final
class UserPool(
    Component[UserPoolResources, UserPoolCustomizationDict],
    LinkableMixin,
):
    _config: UserPoolConfig

    def __init__(
        self,
        name: str,
        /,
        *,
        config: UserPoolConfig | UserPoolConfigDict | None = None,
        tags: dict[str, str] | None = None,
        customize: UserPoolCustomizationDict | None = None,
        **opts: Unpack[UserPoolConfigDict],
    ) -> None:
        super().__init__("stelvio:aws:UserPool", name, tags=tags, customize=customize)
        self._config = self._parse_config(config, opts)
        self._clients: list[UserPoolClient] = []
        self._identity_providers: list[IdentityProvider] = []

    @staticmethod
    def _parse_config(
        config: UserPoolConfig | UserPoolConfigDict | None,
        opts: UserPoolConfigDict,
    ) -> UserPoolConfig:
        if config and opts:
            raise ValueError(
                "Invalid configuration: cannot combine 'config' parameter "
                "with additional options - provide all settings either in "
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
    def config(self) -> UserPoolConfig:
        """Get the component configuration."""
        return self._config

    @property
    def arn(self) -> Output[str]:
        return self.resources.user_pool.arn

    @property
    def id(self) -> Output[str]:
        return self.resources.user_pool.id

    @property
    def identity_providers(self) -> list[IdentityProvider]:
        return self._identity_providers

    @property
    def name_in_aws(self) -> Output[str]:
        return self.resources.user_pool.name

    def add_identity_provider(
        self,
        name: str,
        /,
        *,
        provider_type: IdentityProviderType,
        details: dict[str, str],
        attributes: dict[str, str] | None = None,
        customize: IdentityProviderCustomizationDict | None = None,
    ) -> IdentityProvider:
        from stelvio.aws.cognito.identity_provider import IdentityProvider  # noqa: PLC0415

        self._check_not_created()
        # Social providers use AWS-standard names; oidc/saml use user-provided name
        if provider_type in ("oidc", "saml"):
            cognito_provider_name = name
        else:
            cognito_provider_name = PROVIDER_TYPE_MAP[provider_type]
        expected_idp_name = f"{self.name}-idp-{cognito_provider_name}"

        for existing in self._identity_providers:
            if existing.name == expected_idp_name:
                raise ValueError(
                    f"Duplicate identity provider name '{name}' on UserPool '{self.name}'. "
                    "Provider names must be unique within a user pool."
                )

        result = IdentityProvider(
            expected_idp_name,
            user_pool=self,
            config=IdentityProviderConfig(
                provider_name=cognito_provider_name,
                provider_type=PROVIDER_TYPE_MAP[provider_type],
                details=details,
                attributes=attributes,
            ),
            customize=customize,
        )
        self._identity_providers.append(result)
        return result

    def add_client(
        self,
        name: str,
        /,
        *,
        config: UserPoolClientConfig | UserPoolClientConfigDict | None = None,
        customize: UserPoolClientCustomizationDict | None = None,
        **opts: Unpack[UserPoolClientConfigDict],
    ) -> UserPoolClient:
        from stelvio.aws.cognito.user_pool_client import UserPoolClient  # noqa: PLC0415

        self._check_not_created()
        expected_name = f"{self.name}-{name}"
        for existing in self._clients:
            if existing.name == expected_name:
                raise ValueError(
                    f"Duplicate client name '{name}' on UserPool '{self.name}'. "
                    "Client names must be unique within a user pool."
                )
        client = UserPoolClient(
            expected_name,
            pool=self,
            config=config,
            customize=customize,
            **opts,
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

    def _build_trigger_configuration(
        self,
    ) -> tuple[dict[str, Function], dict[str, Any] | None]:
        trigger_functions: dict[str, Function] = {}
        lambda_config: dict[str, Any] | None = None
        if self._config.triggers:
            lambda_config = {}
            for trigger_name, handler in self._config.triggers.items():
                fn = self._create_trigger_function(trigger_name, handler)
                trigger_functions[trigger_name] = fn
                lambda_config[trigger_name] = fn.resources.function.arn
        return trigger_functions, lambda_config

    def _create_pool_trigger_permissions(
        self,
        trigger_functions: dict[str, Function],
        pool: pulumi_aws.cognito.UserPool,
    ) -> dict[str, pulumi_aws.lambda_.Permission]:
        trigger_permissions: dict[str, pulumi_aws.lambda_.Permission] = {}
        for trigger_name, fn in trigger_functions.items():
            trigger_permissions[trigger_name] = self._create_trigger_permission(
                trigger_name, fn, pool
            )
        return trigger_permissions

    def _prepare_children(
        self,
        pool: pulumi_aws.cognito.UserPool,
    ) -> None:
        """Performance optimization: pre-populate pool resource on children.

        Avoids redundant lazy lookups when children are created as a batch.
        Children can still create the pool lazily if accessed independently.
        """
        for idp in self._identity_providers:
            idp._pool_resource = pool  # noqa: SLF001
        for client in self._clients:
            client._pool_resource = pool  # noqa: SLF001

    def _create_domain(
        self,
        pool: pulumi_aws.cognito.UserPool,
    ) -> tuple[
        pulumi_aws.cognito.UserPoolDomain | None,
        AcmValidatedDomain | None,
        Record | None,
    ]:
        domain = self._config.domain
        if domain is None:
            return None, None, None

        prefix = context().prefix()
        is_custom = "." in domain

        acm_validated_domain: AcmValidatedDomain | None = None
        domain_record: Record | None = None
        certificate_arn = None

        if is_custom:
            dns = context().dns
            if dns is None:
                raise DnsProviderNotConfiguredError(
                    f"Custom domain '{domain}' requires a DNS provider. "
                    "Configure dns in your StelvioApp."
                )

            # Cognito custom domains use CloudFront internally → ACM must be us-east-1
            acm_validated_domain = AcmValidatedDomain(
                f"{self.name}-acm-validated-domain",
                domain_name=domain,
                tags=self._tags,
                customize=self._customize.get("acm_validated_domain"),
                region="us-east-1",
            )
            certificate_arn = acm_validated_domain.resources.cert_validation.certificate_arn

        user_pool_domain = pulumi_aws.cognito.UserPoolDomain(
            safe_name(prefix, f"{self.name}-domain", MAX_USER_POOL_NAME_LENGTH),
            **self._customizer(
                "user_pool_domain",
                {
                    "domain": domain,
                    "user_pool_id": pool.id,
                    "certificate_arn": certificate_arn,
                },
            ),
            opts=self._resource_opts(),
        )

        if is_custom:
            domain_record = context().dns.create_record(
                resource_name=context().prefix(f"{self.name}-domain-record"),
                name=domain,
                record_type="CNAME",
                value=user_pool_domain.cloudfront_distribution,
                ttl=3600,
            )

        return user_pool_domain, acm_validated_domain, domain_record

    def _create_resources(self) -> UserPoolResources:
        prefix = context().prefix()

        # Build username/alias attributes
        username_attributes = self._config.usernames or None
        alias_attributes = self._config.aliases or None
        auto_verified = _auto_verified_attributes(self._config)

        # Build optional configurations
        password_policy = _build_password_policy(self._config)
        email_config = _build_email_config(self._config)

        # MFA configuration
        mfa_configuration = self._config.mfa.upper()
        software_token_mfa = {"enabled": True} if self._config.software_token else None

        trigger_functions, lambda_config = self._build_trigger_configuration()

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
                inject_tags=True,
            ),
            opts=self._resource_opts(),
        )

        trigger_permissions = self._create_pool_trigger_permissions(trigger_functions, pool)
        self._prepare_children(pool)

        # Create domain if configured
        domain_result = self._create_domain(pool)

        outputs = {"id": pool.id, "arn": pool.arn}
        if domain_result[0] is not None:
            outputs["domain"] = domain_result[0].domain
        self.register_outputs(outputs)
        return UserPoolResources(
            user_pool=pool,
            trigger_functions=trigger_functions,
            trigger_permissions=trigger_permissions,
            user_pool_domain=domain_result[0],
            acm_validated_domain=domain_result[1],
            domain_record=domain_result[2],
        )

    def _create_trigger_function(self, trigger_name: str, handler: TriggerHandler) -> Function:
        from stelvio.aws.function import Function, FunctionConfig  # noqa: PLC0415

        fn_name = f"{self.name}-trigger-{trigger_name}"
        if isinstance(handler, Function):
            return handler
        if isinstance(handler, str):
            return Function(fn_name, handler=handler, tags=self._tags, parent=self)
        if isinstance(handler, FunctionConfig):
            return Function(fn_name, config=handler, tags=self._tags, parent=self)
        # dict form (FunctionConfigDict)
        return Function(fn_name, config=handler, tags=self._tags, parent=self)

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
