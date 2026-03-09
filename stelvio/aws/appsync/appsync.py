from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Unpack, final

import pulumi
from pulumi import Output
from pulumi_aws import appsync, lambda_

from stelvio import context
from stelvio.aws import acm
from stelvio.aws.appsync.config import (
    ApiKeyAuth,
    AppSyncConfig,
    AppSyncConfigDict,
    AppSyncCustomizationDict,
    AppSyncDataSourceCustomizationDict,
    AppSyncPipeFunctionCustomizationDict,
    AppSyncResolverCustomizationDict,
    AuthConfig,
    CognitoAuth,
    LambdaAuth,
    OidcAuth,
    _auth_type_string,
)
from stelvio.aws.appsync.constants import (
    DS_TYPE_DYNAMO,
    DS_TYPE_HTTP,
    DS_TYPE_LAMBDA,
    DS_TYPE_NONE,
    DS_TYPE_OPENSEARCH,
    DS_TYPE_RDS,
)
from stelvio.aws.appsync.data_source import (
    AppSyncDataSource,
    AppSyncDataSourceTypeConfig,
    AppSyncRdsSourceConfig,
    _opensearch_arn_from_endpoint,
)
from stelvio.aws.appsync.file_inputs import read_schema_input
from stelvio.aws.appsync.resolver import AppSyncResolver, AppsyncResolverConfig, PipeFunction
from stelvio.aws.dynamo_db import DynamoTable
from stelvio.aws.function import Function, FunctionConfig, FunctionConfigDict, parse_handler_config
from stelvio.aws.permission import AwsPermission
from stelvio.component import Component, link_config_creator, safe_name
from stelvio.dns import DnsProviderNotConfiguredError, Record
from stelvio.link import LinkableMixin, LinkConfig

_DS_TYPES_REQUIRING_CODE = {DS_TYPE_DYNAMO, DS_TYPE_HTTP, DS_TYPE_RDS, DS_TYPE_OPENSEARCH}


def _build_additional_auth_provider(
    auth: AuthConfig,
    *,
    lambda_authorizer_invoke_arn: Output[str] | None = None,
) -> dict[str, Any]:
    provider: dict[str, Any] = {"authentication_type": _auth_type_string(auth)}

    if isinstance(auth, CognitoAuth):
        provider["user_pool_config"] = auth.to_provider_config()
    elif isinstance(auth, OidcAuth):
        provider["openid_connect_config"] = auth.to_provider_config()
    elif isinstance(auth, LambdaAuth):
        if lambda_authorizer_invoke_arn is None:
            raise ValueError("Missing lambda authorizer invoke ARN for LambdaAuth provider")
        provider["lambda_authorizer_config"] = auth.to_authorizer_config(
            lambda_authorizer_invoke_arn,
        )

    return provider


@final
@dataclass(frozen=True)
class AppSyncResources:
    api: appsync.GraphQLApi
    api_key: appsync.ApiKey | None
    none_data_source: appsync.DataSource
    auth_permissions: list[lambda_.Permission] | None = None
    acm_validated_domain: acm.AcmValidatedDomain | None = None
    domain_association: appsync.DomainNameApiAssociation | None = None
    domain_dns_record: Record | None = None


@final
class AppSync(Component[AppSyncResources, AppSyncCustomizationDict], LinkableMixin):
    def __init__(
        self,
        name: str,
        config: AppSyncConfig | AppSyncConfigDict | None = None,
        *,
        tags: dict[str, str] | None = None,
        customize: AppSyncCustomizationDict | None = None,
        **opts: Unpack[AppSyncConfigDict],
    ) -> None:
        super().__init__("stelvio:aws:AppSync", name, tags=tags, customize=customize)

        self._config = self._parse_config(config, opts)
        self._schema = read_schema_input(self._config.schema)

        self._data_sources: dict[str, AppSyncDataSource] = {}
        self._resolvers: list[AppSyncResolver] = []
        self._resolver_keys: set[tuple[str, str]] = set()
        self._pipe_functions: dict[str, PipeFunction] = {}

    @staticmethod
    def _parse_config(
        config: AppSyncConfig | AppSyncConfigDict | None,
        opts: AppSyncConfigDict,
    ) -> AppSyncConfig:
        if config and opts:
            raise ValueError(
                "Invalid configuration: cannot combine 'config' parameter with additional options "
                "- provide all settings either in 'config' or as separate options"
            )
        if config is None:
            return AppSyncConfig(**opts)
        if isinstance(config, AppSyncConfig):
            return config
        if isinstance(config, dict):
            return AppSyncConfig(**config)

        raise TypeError(
            f"Invalid config type: expected AppSyncConfig or dict, got {type(config).__name__}"
        )

    @property
    def config(self) -> AppSyncConfig:
        return self._config

    @property
    def none_data_source(self) -> appsync.DataSource:
        return self.resources.none_data_source

    @property
    def url(self) -> Output[str]:
        return self.resources.api.uris["GRAPHQL"]

    @property
    def arn(self) -> Output[str]:
        return self.resources.api.arn

    @property
    def api_id(self) -> Output[str]:
        return self.resources.api.id

    @property
    def api_key(self) -> Output[str] | None:
        if self.resources.api_key is None:
            return None
        return self.resources.api_key.key

    def data_source_lambda(
        self,
        name: str,
        handler: str | FunctionConfig | Function,
        *,
        customize: AppSyncDataSourceCustomizationDict | None = None,
        **fn_opts: Unpack[FunctionConfigDict],
    ) -> AppSyncDataSource:
        self._validate_data_source_name(name)

        if isinstance(handler, Function):
            if fn_opts:
                raise ValueError(
                    "Cannot specify function options when handler is a Function "
                    "instance. Configure these on the Function directly."
                )
            function_handler: Function | FunctionConfig = handler
        else:
            function_handler = parse_handler_config(handler, fn_opts)

        data_source = AppSyncDataSource(
            name,
            api=self,
            config=AppSyncDataSourceTypeConfig(ds_type=DS_TYPE_LAMBDA, handler=function_handler),
            tags=self.tags,
            customize=customize,
        )
        self._data_sources[name] = data_source
        return data_source

    def data_source_dynamo(
        self,
        name: str,
        *,
        table: DynamoTable,
        customize: AppSyncDataSourceCustomizationDict | None = None,
    ) -> AppSyncDataSource:
        self._validate_data_source_name(name)

        if not isinstance(table, DynamoTable):
            raise TypeError(
                "table must be a DynamoTable component instance created with "
                "stelvio.aws.dynamo_db.DynamoTable"
            )

        data_source = AppSyncDataSource(
            name,
            api=self,
            config=AppSyncDataSourceTypeConfig(ds_type=DS_TYPE_DYNAMO, table=table),
            tags=self.tags,
            customize=customize,
        )
        self._data_sources[name] = data_source
        return data_source

    def data_source_http(
        self,
        name: str,
        *,
        url: str,
        customize: AppSyncDataSourceCustomizationDict | None = None,
    ) -> AppSyncDataSource:
        self._validate_data_source_name(name)

        if not url:
            raise ValueError("url cannot be empty")

        data_source = AppSyncDataSource(
            name,
            api=self,
            config=AppSyncDataSourceTypeConfig(ds_type=DS_TYPE_HTTP, url=url),
            tags=self.tags,
            customize=customize,
        )
        self._data_sources[name] = data_source
        return data_source

    def data_source_rds(
        self,
        name: str,
        *,
        cluster_arn: str,
        secret_arn: str,
        database: str,
        customize: AppSyncDataSourceCustomizationDict | None = None,
    ) -> AppSyncDataSource:
        self._validate_data_source_name(name)

        if not cluster_arn:
            raise ValueError("cluster_arn cannot be empty")
        if not secret_arn:
            raise ValueError("secret_arn cannot be empty")
        if not database:
            raise ValueError("database cannot be empty")

        data_source = AppSyncDataSource(
            name,
            api=self,
            config=AppSyncDataSourceTypeConfig(
                ds_type=DS_TYPE_RDS,
                rds=AppSyncRdsSourceConfig(
                    cluster_arn=cluster_arn,
                    secret_arn=secret_arn,
                    database=database,
                ),
            ),
            tags=self.tags,
            customize=customize,
        )
        self._data_sources[name] = data_source
        return data_source

    def data_source_opensearch(
        self,
        name: str,
        *,
        endpoint: str,
        customize: AppSyncDataSourceCustomizationDict | None = None,
    ) -> AppSyncDataSource:
        self._validate_data_source_name(name)

        if not endpoint:
            raise ValueError("endpoint cannot be empty")
        _opensearch_arn_from_endpoint(endpoint)

        data_source = AppSyncDataSource(
            name,
            api=self,
            config=AppSyncDataSourceTypeConfig(ds_type=DS_TYPE_OPENSEARCH, endpoint=endpoint),
            tags=self.tags,
            customize=customize,
        )
        self._data_sources[name] = data_source
        return data_source

    def query(
        self,
        field: str,
        data_source: AppSyncDataSource | list[PipeFunction] | None,
        *,
        code: str | None = None,
        customize: AppSyncResolverCustomizationDict | None = None,
    ) -> AppSyncResolver:
        return self._add_resolver("Query", field, data_source, code=code, customize=customize)

    def mutation(
        self,
        field: str,
        data_source: AppSyncDataSource | list[PipeFunction] | None,
        *,
        code: str | None = None,
        customize: AppSyncResolverCustomizationDict | None = None,
    ) -> AppSyncResolver:
        return self._add_resolver("Mutation", field, data_source, code=code, customize=customize)

    def subscription(
        self,
        field: str,
        data_source: AppSyncDataSource | list[PipeFunction] | None,
        *,
        code: str | None = None,
        customize: AppSyncResolverCustomizationDict | None = None,
    ) -> AppSyncResolver:
        return self._add_resolver(
            "Subscription", field, data_source, code=code, customize=customize
        )

    def resolver(
        self,
        type_name: str,
        field: str,
        data_source: AppSyncDataSource | list[PipeFunction] | None,
        *,
        code: str | None = None,
        customize: AppSyncResolverCustomizationDict | None = None,
    ) -> AppSyncResolver:
        return self._add_resolver(type_name, field, data_source, code=code, customize=customize)

    def pipe_function(
        self,
        name: str,
        data_source: AppSyncDataSource | None,
        *,
        code: str,
        customize: AppSyncPipeFunctionCustomizationDict | None = None,
    ) -> PipeFunction:
        if not name:
            raise ValueError("Pipe function name cannot be empty")
        if name in self._pipe_functions:
            raise ValueError(f"Duplicate pipe function name '{name}' in AppSync '{self.name}'")
        if not code:
            raise ValueError("code is required for pipe_function")

        self._validate_ownership(data_source)

        pipe_function = PipeFunction(self, name, data_source, code=code, customize=customize)
        self._pipe_functions[name] = pipe_function
        return pipe_function

    def _validate_data_source_name(self, name: str) -> None:
        if not name:
            raise ValueError("Data source name cannot be empty")
        if name == "NONE":
            raise ValueError(
                "Data source name 'NONE' is reserved for the internal NONE data source. "
                "Choose a different name."
            )
        if name in self._data_sources:
            raise ValueError(f"Duplicate data source name '{name}' in AppSync '{self.name}'")

    def _validate_ownership(
        self,
        data_source: AppSyncDataSource | list[PipeFunction] | None,
    ) -> None:
        if isinstance(data_source, AppSyncDataSource):
            if data_source.api is not self:
                raise ValueError(
                    f"Data source '{data_source.name}' belongs to "
                    f"AppSync '{data_source.api_name}', not '{self.name}'. "
                    "Data sources cannot be shared across AppSync APIs."
                )
        elif isinstance(data_source, list):
            for pipe_function in data_source:
                if pipe_function.api_name != self.name:
                    raise ValueError(
                        f"Pipe function '{pipe_function.name}' belongs to "
                        f"AppSync '{pipe_function.api_name}', not '{self.name}'. "
                        "Pipe functions cannot be shared across AppSync APIs."
                    )

    def _add_resolver(
        self,
        type_name: str,
        field: str,
        data_source: AppSyncDataSource | list[PipeFunction] | None,
        *,
        code: str | None = None,
        customize: AppSyncResolverCustomizationDict | None = None,
    ) -> AppSyncResolver:
        if not type_name:
            raise ValueError("type_name cannot be empty")
        if not field:
            raise ValueError("field cannot be empty")

        if isinstance(data_source, list) and not data_source:
            raise ValueError(
                "Pipeline function list cannot be empty. Provide at least one PipeFunction."
            )

        if isinstance(data_source, list):
            for item in data_source:
                if not isinstance(item, PipeFunction):
                    raise TypeError(
                        "Pipeline function list must contain PipeFunction instances, "
                        f"got {type(item).__name__}."
                    )

        self._validate_ownership(data_source)

        resolver_key = (type_name, field)
        if resolver_key in self._resolver_keys:
            raise ValueError(
                f"Duplicate resolver for {type_name}.{field} in AppSync '{self.name}'"
            )

        if (
            not isinstance(data_source, list)
            and data_source is not None
            and data_source.ds_type in _DS_TYPES_REQUIRING_CODE
            and code is None
        ):
            raise ValueError(
                f"code is required for {data_source.ds_type} data source resolvers. "
                "Provide APPSYNC_JS code as inline string or .js file path."
            )

        resolver = AppSyncResolver(
            self,
            AppsyncResolverConfig(
                type_name=type_name,
                field_name=field,
                data_source=data_source,
                code=code,
            ),
            customize=customize,
        )
        self._resolvers.append(resolver)
        self._resolver_keys.add(resolver_key)
        return resolver

    def _get_api_key_auth(self) -> ApiKeyAuth | None:
        if isinstance(self._config.auth, ApiKeyAuth):
            return self._config.auth
        for auth in self._config.additional_auth:
            if isinstance(auth, ApiKeyAuth):
                return auth
        return None

    def _create_resources(self) -> AppSyncResources:
        prefix = context().prefix

        auth_function, additional_auth_functions = self._create_auth_lambdas()
        api_args = self._build_api_args(auth_function, additional_auth_functions)

        graphql_api = appsync.GraphQLApi(
            prefix(self.name),
            **self._customizer("api", api_args, inject_tags=True),
            opts=self._resource_opts(),
        )

        auth_permissions = self._create_auth_permissions(
            graphql_api,
            auth_function,
            additional_auth_functions,
        )
        api_key_resource = self._create_api_key(graphql_api)

        none_data_source = appsync.DataSource(
            safe_name(prefix(), f"{self.name}-none-ds", 128),
            api_id=graphql_api.id,
            name="NONE",
            type=DS_TYPE_NONE,
            opts=self._resource_opts(),
        )

        domain_resources = self._create_domain_resources(graphql_api)

        pulumi.export(f"appsync_{self.name}_url", graphql_api.uris["GRAPHQL"])
        pulumi.export(f"appsync_{self.name}_arn", graphql_api.arn)
        pulumi.export(f"appsync_{self.name}_id", graphql_api.id)
        if api_key_resource is not None:
            pulumi.export(f"appsync_{self.name}_api_key", api_key_resource.key)

        resources = AppSyncResources(
            api=graphql_api,
            api_key=api_key_resource,
            none_data_source=none_data_source,
            auth_permissions=auth_permissions,
            **domain_resources,
        )
        self.register_outputs(
            {
                "url": graphql_api.uris["GRAPHQL"],
                "arn": graphql_api.arn,
                "api_id": graphql_api.id,
            }
        )
        return resources

    def _build_api_args(
        self,
        auth_function: Function | None,
        additional_auth_functions: dict[int, Function],
    ) -> dict[str, Any]:
        prefix = context().prefix
        api_args: dict[str, Any] = {
            "name": prefix(self.name),
            "schema": self._schema,
            "authentication_type": _auth_type_string(self._config.auth),
        }

        if isinstance(self._config.auth, CognitoAuth):
            user_pool_config = self._config.auth.to_provider_config()
            user_pool_config["default_action"] = "ALLOW"
            api_args["user_pool_config"] = user_pool_config
        elif isinstance(self._config.auth, OidcAuth):
            api_args["openid_connect_config"] = self._config.auth.to_provider_config()
        elif isinstance(self._config.auth, LambdaAuth):
            if auth_function is None:
                raise RuntimeError("Primary LambdaAuth function was not created")
            api_args["lambda_authorizer_config"] = self._config.auth.to_authorizer_config(
                auth_function.invoke_arn,
            )

        if self._config.additional_auth:
            api_args["additional_authentication_providers"] = [
                _build_additional_auth_provider(
                    auth,
                    lambda_authorizer_invoke_arn=(
                        additional_auth_functions[index].invoke_arn
                        if index in additional_auth_functions
                        else None
                    ),
                )
                for index, auth in enumerate(self._config.additional_auth)
            ]

        return api_args

    def _create_auth_lambdas(self) -> tuple[Function | None, dict[int, Function]]:
        auth_function: Function | None = None
        additional_auth_functions: dict[int, Function] = {}

        if isinstance(self._config.auth, LambdaAuth):
            auth_function = self._create_auth_lambda(self._config.auth)

        for index, auth in enumerate(self._config.additional_auth):
            if isinstance(auth, LambdaAuth):
                function = self._create_auth_lambda(auth, suffix=f"-additional-{index}")
                additional_auth_functions[index] = function

        return auth_function, additional_auth_functions

    def _create_auth_permissions(
        self,
        graphql_api: appsync.GraphQLApi,
        auth_function: Function | None,
        additional_auth_functions: dict[int, Function],
    ) -> list[lambda_.Permission]:
        prefix = context().prefix
        auth_permissions: list[lambda_.Permission] = []

        if auth_function is not None:
            permission = lambda_.Permission(
                safe_name(prefix(), f"{self.name}-auth-perm", 128),
                **self._customizer(
                    "auth_permissions",
                    {
                        "action": "lambda:InvokeFunction",
                        "function": auth_function.function_name,
                        "principal": "appsync.amazonaws.com",
                        "source_arn": graphql_api.arn,
                    },
                ),
                opts=self._resource_opts(),
            )
            auth_permissions.append(permission)

        for index, function in additional_auth_functions.items():
            permission = lambda_.Permission(
                safe_name(prefix(), f"{self.name}-auth-{index}-perm", 128),
                **self._customizer(
                    "auth_permissions",
                    {
                        "action": "lambda:InvokeFunction",
                        "function": function.function_name,
                        "principal": "appsync.amazonaws.com",
                        "source_arn": graphql_api.arn,
                    },
                ),
                opts=self._resource_opts(),
            )
            auth_permissions.append(permission)

        return auth_permissions

    def _create_auth_lambda(self, auth: LambdaAuth, suffix: str = "") -> Function:
        fn_name = f"{self.name}-authorizer{suffix}"
        if isinstance(auth.handler, Function):
            return auth.handler
        fn_config = parse_handler_config(auth.handler, auth.fn_opts)
        return Function(fn_name, fn_config, tags=self.tags)

    def _create_api_key(self, graphql_api: appsync.GraphQLApi) -> appsync.ApiKey | None:
        api_key_auth = self._get_api_key_auth()
        if api_key_auth is None:
            return None

        prefix = context().prefix
        # Compute expiry from "now" so each deploy refreshes to a full validity window
        # (bounded by ApiKeyAuth validation). This avoids near-expiry replacements during
        # later updates and keeps rotation timing predictable.
        expires_dt = datetime.now(tz=UTC) + timedelta(days=api_key_auth.expires)
        api_key_args: dict[str, Any] = {
            "api_id": graphql_api.id,
            "expires": expires_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        return appsync.ApiKey(
            safe_name(prefix(), f"{self.name}-api-key", 128),
            **self._customizer("api_key", api_key_args),
            opts=self._resource_opts(),
        )

    def _create_domain_resources(self, graphql_api: appsync.GraphQLApi) -> dict[str, Any]:
        if self._config.domain is None:
            return {}

        dns = context().dns
        if dns is None:
            raise DnsProviderNotConfiguredError(
                "DNS provider is not configured. "
                "Please set up a DNS provider to use custom domains."
            )

        prefix = context().prefix

        acm_validated_domain = acm.AcmValidatedDomain(
            f"{self.name}-acm-domain",
            domain_name=self._config.domain,
            tags=self.tags,
            customize=self._customize.get("acm_validated_domain"),
        )

        domain_name = appsync.DomainName(
            safe_name(prefix(), f"{self.name}-domain", 128),
            **self._customizer(
                "domain_name",
                {
                    "domain_name": self._config.domain,
                    "certificate_arn": acm_validated_domain.resources.certificate.arn,
                },
            ),
            opts=self._resource_opts(depends_on=[acm_validated_domain.resources.cert_validation]),
        )

        domain_association = appsync.DomainNameApiAssociation(
            safe_name(prefix(), f"{self.name}-domain-assoc", 128),
            **self._customizer(
                "domain_association",
                {
                    "api_id": graphql_api.id,
                    "domain_name": domain_name.domain_name,
                },
            ),
            opts=self._resource_opts(),
        )

        record = dns.create_record(
            resource_name=safe_name(prefix(), f"{self.name}-domain-record", 255),
            **self._customizer(
                "domain_dns_record",
                {
                    "name": self._config.domain,
                    "record_type": "CNAME",
                    "value": domain_name.appsync_domain_name,
                    "ttl": 1,
                },
            ),
        )

        return {
            "acm_validated_domain": acm_validated_domain,
            "domain_association": domain_association,
            "domain_dns_record": record,
        }


@link_config_creator(AppSync)
def _appsync_link_creator(api: AppSync) -> LinkConfig:
    properties: dict[str, Any] = {"url": api.url}
    permissions = [
        AwsPermission(
            actions=["appsync:GraphQL"],
            resources=[api.arn.apply(lambda arn: f"{arn}/*")],
        ),
    ]
    if api.api_key is not None:
        properties["api_key"] = api.api_key
    return LinkConfig(properties=properties, permissions=permissions)
