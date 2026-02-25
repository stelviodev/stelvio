from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Unpack, final

import pulumi
from pulumi import Output, ResourceOptions
from pulumi_aws import appsync, lambda_

from stelvio import context
from stelvio.aws import acm
from stelvio.aws.appsync.config import (
    ApiKeyAuth,
    AppSyncCustomizationDict,
    AppSyncDataSourceCustomizationDict,
    AppSyncPipeFunctionCustomizationDict,
    AppSyncResolverCustomizationDict,
    AuthConfig,
    CognitoAuth,
    LambdaAuth,
    OidcAuth,
    validate_auth_config,
)
from stelvio.aws.appsync.constants import (
    AUTH_TYPE_API_KEY,
    AUTH_TYPE_COGNITO,
    AUTH_TYPE_IAM,
    AUTH_TYPE_LAMBDA,
    AUTH_TYPE_OIDC,
    DS_TYPE_DYNAMO,
    DS_TYPE_HTTP,
    DS_TYPE_LAMBDA,
    DS_TYPE_NONE,
    DS_TYPE_OPENSEARCH,
    DS_TYPE_RDS,
)
from stelvio.aws.appsync.data_source import (
    AppSyncDataSource,
    _opensearch_arn_from_endpoint,
)
from stelvio.aws.appsync.resolver import (
    AppSyncResolver,
    PipeFunction,
)
from stelvio.aws.dynamo_db import DynamoTable
from stelvio.aws.function import Function, FunctionConfig, FunctionConfigDict, parse_handler_config
from stelvio.aws.permission import AwsPermission
from stelvio.component import Component, link_config_creator, safe_name
from stelvio.dns import DnsProviderNotConfiguredError
from stelvio.link import Link, Linkable, LinkableMixin, LinkConfig
from stelvio.project import get_project_root

# Data source types that require explicit JS code in resolvers
_DS_TYPES_REQUIRING_CODE = (DS_TYPE_DYNAMO, DS_TYPE_HTTP, DS_TYPE_RDS, DS_TYPE_OPENSEARCH)


def _read_schema_or_inline(value: str) -> str:
    """Read a GraphQL schema from file or treat as inline SDL.

    Treats value as a file path if it ends in .graphql or .gql.
    Otherwise returns the value as inline SDL.
    """
    return _read_file_or_inline(value, extensions=(".graphql", ".gql"))


def _read_file_or_inline(value: str, *, extensions: tuple[str, ...]) -> str:
    """Read from file if value ends with a recognized extension, else return as inline content."""
    if any(value.endswith(ext) for ext in extensions):
        file_path = (Path(get_project_root()) / value).resolve()
        if not file_path.is_file():
            raise FileNotFoundError(f"File '{value}' not found (resolved to '{file_path}').")
        return file_path.read_text()
    return value


def _validate_no_duplicate_auth(auth: AuthConfig, additional_auth: list[AuthConfig]) -> None:
    """Reject duplicate auth modes across default and additional auth."""
    all_types = [_auth_type_string(auth)]
    for a in additional_auth:
        auth_type = _auth_type_string(a)
        if auth_type in all_types:
            raise ValueError(
                f"Duplicate authentication mode '{auth_type}'. "
                "Each auth mode can only appear once across auth and additional_auth."
            )
        all_types.append(auth_type)


def _auth_type_string(auth: AuthConfig) -> str:
    """Map an AuthConfig value to the AppSync authentication_type string."""
    if auth == "iam":
        return AUTH_TYPE_IAM
    if isinstance(auth, ApiKeyAuth):
        return AUTH_TYPE_API_KEY
    if isinstance(auth, CognitoAuth):
        return AUTH_TYPE_COGNITO
    if isinstance(auth, OidcAuth):
        return AUTH_TYPE_OIDC
    if isinstance(auth, LambdaAuth):
        return AUTH_TYPE_LAMBDA
    raise TypeError(f"Unexpected auth config type: {type(auth).__name__}")


def _build_cognito_config(auth: CognitoAuth) -> dict[str, Any]:
    config: dict[str, Any] = {"user_pool_id": auth.user_pool_id}
    if auth.region:
        config["aws_region"] = auth.region
    if auth.app_id_client_regex:
        config["app_id_client_regex"] = auth.app_id_client_regex
    return config


def _build_oidc_config(auth: OidcAuth) -> dict[str, Any]:
    config: dict[str, Any] = {"issuer": auth.issuer}
    if auth.client_id:
        config["client_id"] = auth.client_id
    if auth.auth_ttl is not None:
        config["auth_ttl"] = auth.auth_ttl
    if auth.iat_ttl is not None:
        config["iat_ttl"] = auth.iat_ttl
    return config


def _build_additional_auth_provider(auth: AuthConfig) -> dict[str, Any]:
    """Build an additional_authentication_provider entry for AppSync."""
    provider: dict[str, Any] = {"authentication_type": _auth_type_string(auth)}

    if isinstance(auth, CognitoAuth):
        provider["user_pool_config"] = _build_cognito_config(auth)
    elif isinstance(auth, OidcAuth):
        provider["openid_connect_config"] = _build_oidc_config(auth)

    return provider


def _build_lambda_authorizer_config(auth: LambdaAuth, invoke_arn: Output[str]) -> dict[str, Any]:
    """Build lambda_authorizer_config dict for AppSync API."""
    config: dict[str, Any] = {"authorizer_uri": invoke_arn}
    if auth.result_ttl is not None:
        config["authorizer_result_ttl_in_seconds"] = auth.result_ttl
    if auth.identity_validation_expression:
        config["identity_validation_expression"] = auth.identity_validation_expression
    return config


@final
@dataclass(frozen=True)
class AppSyncResources:
    api: appsync.GraphQLApi
    api_key: appsync.ApiKey | None
    none_data_source: appsync.DataSource
    completed: Output[list[str]] = field(default_factory=lambda: Output.from_input([]))


@final
class AppSync(Component[AppSyncResources, AppSyncCustomizationDict], LinkableMixin):
    """AWS AppSync GraphQL API component.

    Uses a builder pattern: add data sources and resolvers before accessing .resources.

    Args:
        name: Component name (must be unique across all Stelvio components).
        schema: GraphQL schema — file path (relative to project root) or inline SDL string.
        auth: Default authentication mode.
        additional_auth: Additional authentication modes for multi-auth.
        domain: Custom domain name (ACM + DNS auto-handled).
        customize: Customization overrides for underlying Pulumi resources.
    """

    def __init__(  # noqa: PLR0913
        self,
        name: str,
        schema: str,
        *,
        auth: AuthConfig,
        additional_auth: list[AuthConfig] | None = None,
        domain: str | None = None,
        customize: AppSyncCustomizationDict | None = None,
    ) -> None:
        validate_auth_config(auth)
        if additional_auth:
            for a in additional_auth:
                validate_auth_config(a)

        if additional_auth:
            _validate_no_duplicate_auth(auth, additional_auth)

        self._schema = _read_schema_or_inline(schema)
        self._auth = auth
        self._additional_auth = additional_auth or []
        self._domain = domain

        self._data_sources: dict[str, AppSyncDataSource] = {}
        self._resolvers: list[AppSyncResolver] = []
        self._resolver_keys: set[tuple[str, str]] = set()
        self._pipe_functions: dict[str, PipeFunction] = {}

        super().__init__(name, customize=customize)

    def _check_not_created(self) -> None:
        if self._resources is not None:
            raise RuntimeError(
                f"Cannot modify AppSync '{self.name}' after resources have been created. "
                "Add all data sources and resolvers before accessing the .resources property."
            )

    # --- Properties ---

    @property
    def url(self) -> Output[str]:
        """GraphQL endpoint URL."""
        return self.resources.api.uris["GRAPHQL"]

    @property
    def arn(self) -> Output[str]:
        """API ARN."""
        return self.resources.api.arn

    @property
    def api_id(self) -> Output[str]:
        """API ID."""
        return self.resources.api.id

    @property
    def api_key(self) -> Output[str] | None:
        """API key value, or None if API_KEY auth isn't configured."""
        if self.resources.api_key is None:
            return None
        return self.resources.api_key.key

    # --- Data source builder methods ---

    def data_source_lambda(
        self,
        name: str,
        handler: str | FunctionConfig | Function,
        *,
        links: list[Link | Linkable] | None = None,
        customize: AppSyncDataSourceCustomizationDict | None = None,
        **fn_opts: Unpack[FunctionConfigDict],
    ) -> AppSyncDataSource:
        """Add a Lambda data source.

        Args:
            name: Data source name (unique within this API).
            handler: Lambda handler — string path, FunctionConfig, or Function instance.
            links: Resources to link to the Lambda function.
            customize: Customization for data_source and service_role resources.
            **fn_opts: Additional function options (memory, timeout, etc.).
        """
        self._check_not_created()
        self._validate_data_source_name(name)

        if isinstance(handler, Function):
            has_extra = links is not None or fn_opts
            if has_extra:
                raise ValueError(
                    "Cannot specify links or function options when handler is a Function "
                    "instance. Configure these on the Function directly."
                )
            resolved_handler: FunctionConfig | Function = handler
        else:
            if links is not None:
                fn_opts["links"] = links
            resolved_handler = parse_handler_config(handler, fn_opts)

        ds = AppSyncDataSource(
            name,
            self,
            DS_TYPE_LAMBDA,
            handler=resolved_handler,
            customize=customize,
        )
        self._data_sources[name] = ds
        return ds

    def data_source_dynamo(
        self,
        name: str,
        *,
        table: "DynamoTable",
        customize: AppSyncDataSourceCustomizationDict | None = None,
    ) -> AppSyncDataSource:
        """Add a DynamoDB data source.

        Args:
            name: Data source name (unique within this API).
            table: Stelvio DynamoDB component instance.
            customize: Customization for data_source and service_role resources.
        """
        self._check_not_created()
        self._validate_data_source_name(name)

        if not isinstance(table, DynamoTable):
            raise TypeError(
                "table must be a DynamoTable component instance created with "
                "stelvio.aws.dynamo_db.DynamoTable"
            )

        ds = AppSyncDataSource(
            name,
            self,
            DS_TYPE_DYNAMO,
            table=table,
            customize=customize,
        )
        self._data_sources[name] = ds
        return ds

    def data_source_http(
        self,
        name: str,
        *,
        url: str,
        customize: AppSyncDataSourceCustomizationDict | None = None,
    ) -> AppSyncDataSource:
        """Add an HTTP data source.

        Args:
            name: Data source name (unique within this API).
            url: Base URL for the HTTP endpoint.
            customize: Customization for data_source and service_role resources.
        """
        self._check_not_created()
        self._validate_data_source_name(name)

        if not url:
            raise ValueError("url cannot be empty")

        ds = AppSyncDataSource(
            name,
            self,
            DS_TYPE_HTTP,
            url=url,
            customize=customize,
        )
        self._data_sources[name] = ds
        return ds

    def data_source_rds(
        self,
        name: str,
        *,
        cluster_arn: str,
        secret_arn: str,
        database: str,
        customize: AppSyncDataSourceCustomizationDict | None = None,
    ) -> AppSyncDataSource:
        """Add an Aurora RDS (Data API) data source.

        Args:
            name: Data source name (unique within this API).
            cluster_arn: Aurora cluster ARN.
            secret_arn: Secrets Manager secret ARN for database credentials.
            database: Database name.
            customize: Customization for data_source and service_role resources.
        """
        self._check_not_created()
        self._validate_data_source_name(name)

        if not cluster_arn:
            raise ValueError("cluster_arn cannot be empty")
        if not secret_arn:
            raise ValueError("secret_arn cannot be empty")
        if not database:
            raise ValueError("database cannot be empty")

        ds = AppSyncDataSource(
            name,
            self,
            DS_TYPE_RDS,
            cluster_arn=cluster_arn,
            secret_arn=secret_arn,
            database=database,
            customize=customize,
        )
        self._data_sources[name] = ds
        return ds

    def data_source_opensearch(
        self,
        name: str,
        *,
        endpoint: str,
        customize: AppSyncDataSourceCustomizationDict | None = None,
    ) -> AppSyncDataSource:
        """Add an OpenSearch data source.

        Args:
            name: Data source name (unique within this API).
            endpoint: OpenSearch domain endpoint URL.
            customize: Customization for data_source and service_role resources.
        """
        self._check_not_created()
        self._validate_data_source_name(name)

        if not endpoint:
            raise ValueError("endpoint cannot be empty")
        _opensearch_arn_from_endpoint(endpoint)

        ds = AppSyncDataSource(
            name,
            self,
            DS_TYPE_OPENSEARCH,
            endpoint=endpoint,
            customize=customize,
        )
        self._data_sources[name] = ds
        return ds

    # --- Resolver builder methods ---

    def query(
        self,
        field: str,
        data_source: AppSyncDataSource | list[PipeFunction] | None,
        *,
        code: str | None = None,
        customize: AppSyncResolverCustomizationDict | None = None,
    ) -> AppSyncResolver:
        """Add a Query resolver."""
        return self._add_resolver("Query", field, data_source, code=code, customize=customize)

    def mutation(
        self,
        field: str,
        data_source: AppSyncDataSource | list[PipeFunction] | None,
        *,
        code: str | None = None,
        customize: AppSyncResolverCustomizationDict | None = None,
    ) -> AppSyncResolver:
        """Add a Mutation resolver."""
        return self._add_resolver("Mutation", field, data_source, code=code, customize=customize)

    def subscription(
        self,
        field: str,
        data_source: AppSyncDataSource | list[PipeFunction] | None,
        *,
        code: str | None = None,
        customize: AppSyncResolverCustomizationDict | None = None,
    ) -> AppSyncResolver:
        """Add a Subscription resolver."""
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
        """Add a resolver for any GraphQL type (including nested types)."""
        return self._add_resolver(type_name, field, data_source, code=code, customize=customize)

    # --- Pipeline function builder ---

    def pipe_function(
        self,
        name: str,
        data_source: AppSyncDataSource | None,
        *,
        code: str,
        customize: AppSyncPipeFunctionCustomizationDict | None = None,
    ) -> PipeFunction:
        """Create a pipeline function (step) for use in pipeline resolvers.

        Args:
            name: Pipeline function name (unique within this API).
            data_source: Data source this step uses, or None for NONE.
            code: APPSYNC_JS code — inline string or .js file path. Required.
            customize: Customization for the AppSync Function resource.
        """
        self._check_not_created()

        if not name:
            raise ValueError("Pipe function name cannot be empty")

        if name in self._pipe_functions:
            raise ValueError(f"Duplicate pipe function name '{name}' in AppSync '{self.name}'")

        if not code:
            raise ValueError("code is required for pipe_function")

        self._validate_ownership(data_source)

        pf = PipeFunction(name, self, data_source, code=code, customize=customize)
        self._pipe_functions[name] = pf
        return pf

    # --- Internal helpers ---

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
        self, data_source: AppSyncDataSource | list[PipeFunction] | None
    ) -> None:
        """Validate that data sources and pipe functions belong to this API."""
        if isinstance(data_source, AppSyncDataSource):
            if data_source._api is not self:  # noqa: SLF001
                raise ValueError(
                    f"Data source '{data_source.ds_name}' belongs to "
                    f"AppSync '{data_source._api.name}', not '{self.name}'. "  # noqa: SLF001
                    f"Data sources cannot be shared across AppSync APIs."
                )
        elif isinstance(data_source, list):
            for pf in data_source:
                if pf._api is not self:  # noqa: SLF001
                    raise ValueError(
                        f"Pipe function '{pf.name}' belongs to "
                        f"AppSync '{pf._api.name}', not '{self.name}'. "  # noqa: SLF001
                        f"Pipe functions cannot be shared across AppSync APIs."
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
        self._check_not_created()

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
                        f"Pipeline function list must contain PipeFunction instances, "
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
                f"Provide APPSYNC_JS code as inline string or .js file path."
            )

        resolver_name = f"{self.name}-{type_name}-{field}"
        resolver = AppSyncResolver(
            resolver_name,
            self,
            type_name,
            field,
            data_source,
            code=code,
            customize=customize,
        )
        self._resolvers.append(resolver)
        self._resolver_keys.add(resolver_key)
        return resolver

    # --- Resource creation ---

    def _get_api_key_auth(self) -> ApiKeyAuth | None:
        """Get the ApiKeyAuth config if configured (default or additional)."""
        if isinstance(self._auth, ApiKeyAuth):
            return self._auth
        for a in self._additional_auth:
            if isinstance(a, ApiKeyAuth):
                return a
        return None

    def _create_resources(self) -> AppSyncResources:
        prefix = context().prefix

        # 1. Create GraphQL API with auth config
        api_args, auth_functions = self._build_api_args_with_auth()

        graphql_api = appsync.GraphQLApi(
            prefix(self.name),
            **self._customizer("api", api_args),
        )

        auth_perm_outputs = self._create_auth_permissions(graphql_api, auth_functions)

        # 2. Create API Key if API_KEY auth configured
        api_key_resource = self._create_api_key(graphql_api)

        # 3. Create internal NONE data source (shared by all NONE resolvers)
        none_ds = appsync.DataSource(
            safe_name(prefix(), f"{self.name}-none-ds", 128),
            api_id=graphql_api.id,
            name="NONE",
            type=DS_TYPE_NONE,
        )

        # Track all resource outputs for completion signaling
        all_outputs: list[Output[str]] = [none_ds.arn]
        all_outputs.extend(auth_perm_outputs)

        # Store core resources so children can access them
        resources = AppSyncResources(
            api=graphql_api,
            api_key=api_key_resource,
            none_data_source=none_ds,
        )
        # Temporarily set _resources so children can access parent resources
        self._resources = resources

        # 4. Trigger child data source resource creation
        for ds in self._data_sources.values():
            if not ds._creating:  # noqa: SLF001
                ds_res = ds.resources
                all_outputs.append(ds_res.data_source.arn)

        # 5. Trigger child pipe function resource creation
        for pf in self._pipe_functions.values():
            if not pf._creating:  # noqa: SLF001
                pf_res = pf.resources
                all_outputs.append(pf_res.function.arn)

        # 6. Trigger child resolver resource creation
        for r in self._resolvers:
            if not r._creating:  # noqa: SLF001
                r_res = r.resources
                all_outputs.append(r_res.resolver.arn)

        # 7. Custom domain
        if self._domain is not None:
            domain_outputs = self._create_custom_domain(graphql_api)
            all_outputs.extend(domain_outputs)

        # Exports
        pulumi.export(f"appsync_{self.name}_url", graphql_api.uris["GRAPHQL"])
        pulumi.export(f"appsync_{self.name}_arn", graphql_api.arn)
        pulumi.export(f"appsync_{self.name}_id", graphql_api.id)
        if api_key_resource is not None:
            pulumi.export(f"appsync_{self.name}_api_key", api_key_resource.key)
            all_outputs.append(api_key_resource.id)

        completed = Output.all(*all_outputs)

        # Update resources with completed signal
        return AppSyncResources(
            api=graphql_api,
            api_key=api_key_resource,
            none_data_source=none_ds,
            completed=completed,
        )

    def _build_api_args_with_auth(self) -> tuple[dict[str, Any], list[Function]]:
        """Build API args dict and create auth Lambda functions.

        Returns:
            Tuple of (api_args, auth_functions) where auth_functions contains all
            Lambda authorizer functions created for default and additional auth.
        """
        prefix = context().prefix
        api_args: dict[str, Any] = {
            "name": prefix(self.name),
            "schema": self._schema,
            "authentication_type": _auth_type_string(self._auth),
        }
        auth_functions: list[Function] = []

        # Default auth config
        if isinstance(self._auth, CognitoAuth):
            api_args["user_pool_config"] = _build_cognito_config(self._auth)
        elif isinstance(self._auth, OidcAuth):
            api_args["openid_connect_config"] = _build_oidc_config(self._auth)
        elif isinstance(self._auth, LambdaAuth):
            fn = self._create_auth_lambda(self._auth)
            auth_functions.append(fn)
            api_args["lambda_authorizer_config"] = _build_lambda_authorizer_config(
                self._auth, fn.invoke_arn
            )

        # Additional auth providers
        if self._additional_auth:
            providers = []
            for i, a in enumerate(self._additional_auth):
                provider = _build_additional_auth_provider(a)
                if isinstance(a, LambdaAuth):
                    fn = self._create_auth_lambda(a, suffix=f"-additional-{i}")
                    auth_functions.append(fn)
                    provider["lambda_authorizer_config"] = _build_lambda_authorizer_config(
                        a, fn.invoke_arn
                    )
                providers.append(provider)
            api_args["additional_authentication_providers"] = providers

        return api_args, auth_functions

    def _create_auth_permissions(
        self,
        graphql_api: appsync.GraphQLApi,
        auth_functions: list[Function],
    ) -> list[Output[str]]:
        """Create Lambda permissions for authorizer functions to be invoked by AppSync."""
        prefix = context().prefix
        outputs: list[Output[str]] = []
        for i, fn in enumerate(auth_functions):
            suffix = f"-auth-{i}-perm" if i > 0 else "-auth-perm"
            perm = lambda_.Permission(
                safe_name(prefix(), f"{self.name}{suffix}", 128),
                action="lambda:InvokeFunction",
                function=fn.function_name,
                principal="appsync.amazonaws.com",
                source_arn=graphql_api.arn,
            )
            outputs.append(perm.id)

        return outputs

    def _create_auth_lambda(self, auth: LambdaAuth, suffix: str = "") -> Function:
        """Create a Lambda function for Lambda authorizer auth."""
        fn_name = f"{self.name}-authorizer{suffix}"
        if isinstance(auth.handler, Function):
            return auth.handler
        return Function(fn_name, auth.handler)

    def _create_api_key(self, graphql_api: appsync.GraphQLApi) -> appsync.ApiKey | None:
        """Create an API Key resource if API_KEY auth is configured."""
        api_key_auth = self._get_api_key_auth()
        if api_key_auth is None:
            return None

        prefix = context().prefix
        # Expiry is recalculated on each deploy, effectively auto-extending the key.
        # The key value itself remains stable across deployments.
        expires_dt = datetime.now(tz=UTC) + timedelta(days=api_key_auth.expires)
        api_key_args: dict[str, Any] = {
            "api_id": graphql_api.id,
            "expires": expires_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        return appsync.ApiKey(
            safe_name(prefix(), f"{self.name}-api-key", 128),
            **self._customizer("api_key", api_key_args),
        )

    def _create_custom_domain(self, graphql_api: appsync.GraphQLApi) -> list[Output[str]]:
        """Create custom domain with ACM certificate and DNS."""
        prefix = context().prefix
        dns = context().dns

        if dns is None:
            raise DnsProviderNotConfiguredError(
                "DNS provider is not configured. "
                "Please set up a DNS provider to use custom domains."
            )

        custom_domain = acm.AcmValidatedDomain(
            f"{self.name}-acm-domain",
            domain_name=self._domain,
        )

        domain_name = appsync.DomainName(
            safe_name(prefix(), f"{self.name}-domain", 128),
            **self._customizer(
                "domain_name",
                {
                    "domain_name": self._domain,
                    "certificate_arn": custom_domain.resources.certificate.arn,
                },
            ),
            opts=ResourceOptions(depends_on=[custom_domain.resources.cert_validation]),
        )

        domain_name_assoc = appsync.DomainNameApiAssociation(
            safe_name(prefix(), f"{self.name}-domain-assoc", 128),
            api_id=graphql_api.id,
            domain_name=domain_name.domain_name,
        )

        dns.create_record(
            resource_name=prefix(f"{self.name}-domain-record"),
            name=self._domain,
            record_type="CNAME",
            value=domain_name.appsync_domain_name,
            ttl=1,
        )

        return [domain_name.urn, domain_name_assoc.id]


@link_config_creator(AppSync)
def _appsync_link_creator(api: AppSync) -> LinkConfig:
    """Default link configuration for AppSync.

    Grants GraphQL execution permissions and exposes URL (and API key if configured).
    """
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
