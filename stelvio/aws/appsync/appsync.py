import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Unpack, final

import pulumi
from pulumi import Output, ResourceOptions
from pulumi_aws import appsync, iam, lambda_

from stelvio import context
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
    APPSYNC_JS_RUNTIME,
    APPSYNC_JS_RUNTIME_VERSION,
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
    NONE_PASSTHROUGH_CODE,
)
from stelvio.aws.appsync.data_source import AppSyncDataSource, AppSyncDataSourceResources
from stelvio.aws.appsync.resolver import (
    AppSyncPipeFunctionResources,
    AppSyncResolver,
    AppSyncResolverResources,
    PipeFunction,
)
from stelvio.aws.function import Function, FunctionConfig, FunctionConfigDict, parse_handler_config
from stelvio.aws.permission import AwsPermission
from stelvio.component import Component, link_config_creator, safe_name
from stelvio.link import Link, Linkable, LinkableMixin, LinkConfig
from stelvio.project import get_project_root

if TYPE_CHECKING:
    from stelvio.aws.dynamo_db import DynamoTable

# Seconds in a day for API key expiration
_SECONDS_PER_DAY = 86400

# Data source types that require explicit JS code in resolvers
_DS_TYPES_REQUIRING_CODE = (DS_TYPE_DYNAMO, DS_TYPE_HTTP, DS_TYPE_RDS, DS_TYPE_OPENSEARCH)


def _read_file_or_inline(value: str) -> str:
    """If value is a file path relative to project root, read it; otherwise return as-is."""
    file_path = Path(get_project_root()) / value
    if file_path.is_file():
        return file_path.read_text()
    return value


_OPENSEARCH_ENDPOINT_RE = re.compile(
    r"^https://(?:search|vpc)-([^.]+)-[a-z0-9]+\.([a-z0-9-]+)\.es\.amazonaws\.com/?$"
)


def _opensearch_arn_from_endpoint(endpoint: str) -> str:
    """Derive OpenSearch domain ARN pattern from endpoint URL for IAM policy Resource."""
    match = _OPENSEARCH_ENDPOINT_RE.match(endpoint)
    if not match:
        raise ValueError(
            f"Cannot derive domain ARN from OpenSearch endpoint '{endpoint}'. "
            "Expected format: https://search-DOMAIN-ID.REGION.es.amazonaws.com"
        )
    domain_name = match.group(1)
    region = match.group(2)
    return f"arn:aws:es:{region}:*:domain/{domain_name}/*"


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


_LAMBDA_AUTH_FN_FIELDS = (
    "links",
    "memory",
    "timeout",
    "environment",
    "architecture",
    "runtime",
    "requirements",
    "layers",
    "folder",
    "url",
)


def _build_lambda_auth_function_config(auth: LambdaAuth) -> FunctionConfig | Function:
    """Build Function or FunctionConfig from LambdaAuth."""
    if isinstance(auth.handler, Function | FunctionConfig):
        return auth.handler
    kwargs: dict[str, Any] = {"handler": auth.handler}
    for name in _LAMBDA_AUTH_FN_FIELDS:
        value = getattr(auth, name)
        if value is None or value in ([], {}):
            continue
        kwargs[name] = value
    return FunctionConfig(**kwargs)


def _build_lambda_authorizer_config(auth: LambdaAuth, invoke_arn: Output[str]) -> dict[str, Any]:
    """Build lambda_authorizer_config dict for AppSync API."""
    config: dict[str, Any] = {"authorizer_uri": invoke_arn}
    if auth.result_ttl is not None:
        config["authorizer_result_ttl_in_seconds"] = auth.result_ttl
    if auth.identity_validation_expression:
        config["identity_validation_expression"] = auth.identity_validation_expression
    return config


def _build_ds_type_config(ds: AppSyncDataSource) -> dict[str, Any]:
    """Build data-source-type-specific args for appsync.DataSource."""
    config = ds.config
    extra: dict[str, Any] = {}

    if ds.ds_type == DS_TYPE_DYNAMO:
        table = config["table"]
        extra["dynamodb_config"] = {
            "table_name": table.resources.table.name,
            "region": context().aws.region,
        }
    elif ds.ds_type == DS_TYPE_HTTP:
        extra["http_config"] = {"endpoint": config["url"]}
    elif ds.ds_type == DS_TYPE_RDS:
        extra["relational_database_config"] = {
            "http_endpoint_config": {
                "db_cluster_identifier": config["cluster_arn"],
                "aws_secret_store_arn": config["secret_arn"],
                "database_name": config["database"],
            },
        }
    elif ds.ds_type == DS_TYPE_OPENSEARCH:
        extra["elasticsearch_config"] = {"endpoint": config["endpoint"]}

    return extra


def _appsync_trust_policy() -> str:
    return json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": "appsync.amazonaws.com"},
                    "Action": "sts:AssumeRole",
                }
            ],
        }
    )


def _static_policy_statements(ds: AppSyncDataSource) -> list[dict[str, Any]]:
    """Generate IAM policy statements for data source types with static ARNs."""
    config = ds.config

    if ds.ds_type == DS_TYPE_RDS:
        return [
            {
                "Effect": "Allow",
                "Action": [
                    "rds-data:ExecuteStatement",
                    "rds-data:BatchExecuteStatement",
                    "rds-data:BeginTransaction",
                    "rds-data:CommitTransaction",
                    "rds-data:RollbackTransaction",
                ],
                "Resource": config["cluster_arn"],
            },
            {
                "Effect": "Allow",
                "Action": ["secretsmanager:GetSecretValue"],
                "Resource": config["secret_arn"],
            },
        ]

    if ds.ds_type == DS_TYPE_OPENSEARCH:
        return [
            {
                "Effect": "Allow",
                "Action": ["es:ESHttp*"],
                "Resource": _opensearch_arn_from_endpoint(config["endpoint"]),
            },
        ]

    # Lambda and DynamoDB use Output-based policies; HTTP needs none
    return []


def _merge_customize(
    default_props: dict[str, Any],
    customize: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        **default_props,
        **(customize or {}),
    }


@final
@dataclass(frozen=True)
class AppSyncResources:
    api: appsync.GraphQLApi
    api_key: appsync.ApiKey | None
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

        self._schema = _read_file_or_inline(schema)
        self._auth = auth
        self._additional_auth = additional_auth or []
        self._domain = domain

        self._data_sources: dict[str, AppSyncDataSource] = {}
        self._resolvers: list[AppSyncResolver] = []
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
            function_config = None
            config: dict[str, Any] = {"function": handler}
        else:
            if links is not None:
                fn_opts["links"] = links
            function_config = parse_handler_config(handler, fn_opts)
            config = {}

        ds = AppSyncDataSource(
            name,
            DS_TYPE_LAMBDA,
            config=config,
            function_config=function_config,
            customize=customize,
        )
        ds._api_name = self.name  # noqa: SLF001
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

        ds = AppSyncDataSource(
            name,
            DS_TYPE_DYNAMO,
            config={"table": table},
            customize=customize,
        )
        ds._api_name = self.name  # noqa: SLF001
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
            DS_TYPE_HTTP,
            config={"url": url},
            customize=customize,
        )
        ds._api_name = self.name  # noqa: SLF001
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
            DS_TYPE_RDS,
            config={
                "cluster_arn": cluster_arn,
                "secret_arn": secret_arn,
                "database": database,
            },
            customize=customize,
        )
        ds._api_name = self.name  # noqa: SLF001
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
            DS_TYPE_OPENSEARCH,
            config={"endpoint": endpoint},
            customize=customize,
        )
        ds._api_name = self.name  # noqa: SLF001
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

        pf = PipeFunction(name, data_source, code=code, customize=customize)
        pf._api_name = self.name  # noqa: SLF001
        self._pipe_functions[name] = pf
        return pf

    # --- Internal helpers ---

    def _validate_data_source_name(self, name: str) -> None:
        if not name:
            raise ValueError("Data source name cannot be empty")
        if name in self._data_sources:
            raise ValueError(f"Duplicate data source name '{name}' in AppSync '{self.name}'")

    def _validate_ownership(
        self, data_source: AppSyncDataSource | list[PipeFunction] | None
    ) -> None:
        """Validate that data sources and pipe functions belong to this API."""
        if isinstance(data_source, AppSyncDataSource):
            if data_source._api_name is not None and data_source._api_name != self.name:  # noqa: SLF001
                raise ValueError(
                    f"Data source '{data_source.name}' belongs to "
                    f"AppSync '{data_source._api_name}', not '{self.name}'. "  # noqa: SLF001
                    f"Data sources cannot be shared across AppSync APIs."
                )
        elif isinstance(data_source, list):
            for pf in data_source:
                if pf._api_name is not None and pf._api_name != self.name:  # noqa: SLF001
                    raise ValueError(
                        f"Pipe function '{pf.name}' belongs to "
                        f"AppSync '{pf._api_name}', not '{self.name}'. "  # noqa: SLF001
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

        self._validate_ownership(data_source)

        for r in self._resolvers:
            if r.type_name == type_name and r.field_name == field:
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

        resolver = AppSyncResolver(type_name, field, data_source, code=code, customize=customize)
        self._resolvers.append(resolver)
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
        api_args = self._build_api_args()
        auth_function, additional_auth_functions = self._setup_auth_lambdas(api_args)

        graphql_api = appsync.GraphQLApi(
            prefix(self.name),
            **self._customizer("api", api_args),
        )

        auth_perm_outputs = self._create_auth_permissions(
            graphql_api, auth_function, additional_auth_functions
        )

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

        # 4. Create user data sources
        ds_resources: dict[str, appsync.DataSource] = {}
        for ds_name, ds in self._data_sources.items():
            ds_res, ds_child_outputs = self._create_data_source(ds, graphql_api)
            ds_resources[ds_name] = ds_res
            all_outputs.append(ds_res.arn)
            all_outputs.extend(ds_child_outputs)

        # 5. Create AppSync Functions (pipeline steps)
        pf_resources = self._create_pipe_functions(graphql_api)
        all_outputs.extend(pf.arn for pf in pf_resources.values())

        # 6. Create resolvers
        for r in self._resolvers:
            resolver_res = self._create_resolver(r, graphql_api, pf_resources)
            all_outputs.append(resolver_res.arn)

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

        return AppSyncResources(api=graphql_api, api_key=api_key_resource, completed=completed)

    def _build_api_args(self) -> dict[str, Any]:
        """Build the args dict for appsync.GraphQLApi."""
        prefix = context().prefix
        api_args: dict[str, Any] = {
            "name": prefix(self.name),
            "schema": self._schema,
            "authentication_type": _auth_type_string(self._auth),
        }

        if isinstance(self._auth, CognitoAuth):
            api_args["user_pool_config"] = _build_cognito_config(self._auth)
        elif isinstance(self._auth, OidcAuth):
            api_args["openid_connect_config"] = _build_oidc_config(self._auth)

        if self._additional_auth:
            api_args["additional_authentication_providers"] = [
                _build_additional_auth_provider(a) for a in self._additional_auth
            ]

        return api_args

    def _setup_auth_lambdas(
        self, api_args: dict[str, Any]
    ) -> tuple[Function | None, dict[int, Function]]:
        """Create Lambda authorizer functions and patch api_args with their config."""
        auth_function: Function | None = None
        additional_auth_functions: dict[int, Function] = {}

        if isinstance(self._auth, LambdaAuth):
            auth_function = self._create_auth_lambda(self._auth)
            api_args["lambda_authorizer_config"] = _build_lambda_authorizer_config(
                self._auth, auth_function.invoke_arn
            )

        for i, a in enumerate(self._additional_auth):
            if isinstance(a, LambdaAuth):
                fn = self._create_auth_lambda(a, suffix=f"-additional-{i}")
                additional_auth_functions[i] = fn
                provider_entry = api_args["additional_authentication_providers"][i]
                provider_entry["lambda_authorizer_config"] = _build_lambda_authorizer_config(
                    a, fn.invoke_arn
                )

        return auth_function, additional_auth_functions

    def _create_auth_permissions(
        self,
        graphql_api: appsync.GraphQLApi,
        auth_function: Function | None,
        additional_auth_functions: dict[int, Function],
    ) -> list[Output[str]]:
        """Create Lambda permissions for authorizer functions to be invoked by AppSync."""
        prefix = context().prefix
        outputs: list[Output[str]] = []
        if auth_function is not None:
            perm = lambda_.Permission(
                safe_name(prefix(), f"{self.name}-auth-perm", 128),
                action="lambda:InvokeFunction",
                function=auth_function.function_name,
                principal="appsync.amazonaws.com",
                source_arn=graphql_api.arn,
            )
            outputs.append(perm.id)

        for i, fn in additional_auth_functions.items():
            perm = lambda_.Permission(
                safe_name(prefix(), f"{self.name}-auth-{i}-perm", 128),
                action="lambda:InvokeFunction",
                function=fn.function_name,
                principal="appsync.amazonaws.com",
                source_arn=graphql_api.arn,
            )
            outputs.append(perm.id)

        return outputs

    def _create_auth_lambda(self, auth: LambdaAuth, suffix: str = "") -> Function:
        """Create a Lambda function for Lambda authorizer auth."""
        fn_config = _build_lambda_auth_function_config(auth)
        fn_name = f"{self.name}-authorizer{suffix}"
        if isinstance(fn_config, Function):
            return fn_config
        return Function(fn_name, fn_config)

    def _create_api_key(self, graphql_api: appsync.GraphQLApi) -> appsync.ApiKey | None:
        """Create an API Key resource if API_KEY auth is configured."""
        api_key_auth = self._get_api_key_auth()
        if api_key_auth is None:
            return None

        prefix = context().prefix
        expires = datetime.now(tz=UTC).timestamp() + (api_key_auth.expires * _SECONDS_PER_DAY)
        return appsync.ApiKey(
            safe_name(prefix(), f"{self.name}-api-key", 128),
            api_id=graphql_api.id,
            expires=str(int(expires)),
        )

    def _create_data_source(
        self,
        ds: AppSyncDataSource,
        graphql_api: appsync.GraphQLApi,
    ) -> tuple[appsync.DataSource, list[Output[str]]]:
        """Create a Pulumi data source with IAM role and return (DataSource, child_outputs)."""
        prefix = context().prefix

        role, role_policy_outputs = self._create_data_source_role(ds)

        ds_args: dict[str, Any] = {
            "api_id": graphql_api.id,
            "name": ds.name,
            "type": ds.ds_type,
            "service_role_arn": role.arn,
        }

        function_instance = self._resolve_lambda_function(ds)
        if function_instance is not None:
            ds_args["lambda_config"] = {"function_arn": function_instance.resources.function.arn}

        ds_args.update(_build_ds_type_config(ds))

        pulumi_ds = appsync.DataSource(
            safe_name(prefix(), f"{self.name}-ds-{ds.name}", 128),
            **_merge_customize(
                ds_args,
                ds.customize.get("data_source") if ds.customize else None,
            ),
        )

        ds._set_resources(  # noqa: SLF001
            AppSyncDataSourceResources(
                data_source=pulumi_ds,
                service_role=role,
                function=function_instance,
            )
        )

        # Create Output-based IAM policies (Lambda ARN, DynamoDB ARN)
        output_policy_outputs = self._create_data_source_output_policies(ds, role)

        return pulumi_ds, role_policy_outputs + output_policy_outputs

    def _resolve_lambda_function(self, ds: AppSyncDataSource) -> Function | None:
        """Create or retrieve the Lambda function for a Lambda data source."""
        if ds.ds_type != DS_TYPE_LAMBDA:
            return None
        if "function" in ds.config:
            return ds.config["function"]
        return Function(f"{self.name}-ds-{ds.name}", ds.function_config)

    def _create_data_source_role(
        self, ds: AppSyncDataSource
    ) -> tuple[iam.Role, list[Output[str]]]:
        """Create an IAM service role for a data source and return (role, policy_outputs)."""
        prefix = context().prefix
        policy_outputs: list[Output[str]] = []

        role = iam.Role(
            safe_name(prefix(), f"{self.name}-ds-{ds.name}-role", 64),
            **_merge_customize(
                {
                    "assume_role_policy": _appsync_trust_policy(),
                },
                ds.customize.get("service_role") if ds.customize else None,
            ),
        )

        # Attach static inline policy for RDS and OpenSearch (ARNs are plain strings)
        policy_statements = _static_policy_statements(ds)
        if policy_statements:
            inline_policy = iam.RolePolicy(
                safe_name(prefix(), f"{self.name}-ds-{ds.name}-policy", 128),
                role=role.name,
                policy=json.dumps(
                    {
                        "Version": "2012-10-17",
                        "Statement": policy_statements,
                    }
                ),
            )
            policy_outputs.append(inline_policy.id)

        return role, policy_outputs

    def _create_data_source_output_policies(
        self,
        ds: AppSyncDataSource,
        role: iam.Role,
    ) -> list[Output[str]]:
        """Create IAM policies that depend on Pulumi Outputs (Lambda ARN, DynamoDB ARN)."""
        prefix = context().prefix
        outputs: list[Output[str]] = []

        if ds.ds_type == DS_TYPE_LAMBDA:
            function_instance = ds.resources.function
            if function_instance is not None:
                fn_arn = function_instance.resources.function.arn
                policy = iam.RolePolicy(
                    safe_name(prefix(), f"{self.name}-ds-{ds.name}-lambda-policy", 128),
                    role=role.name,
                    policy=fn_arn.apply(
                        lambda arn: json.dumps(
                            {
                                "Version": "2012-10-17",
                                "Statement": [
                                    {
                                        "Effect": "Allow",
                                        "Action": ["lambda:InvokeFunction"],
                                        "Resource": arn,
                                    }
                                ],
                            }
                        )
                    ),
                )
                outputs.append(policy.id)

        elif ds.ds_type == DS_TYPE_DYNAMO:
            table = ds.config["table"]
            policy = iam.RolePolicy(
                safe_name(prefix(), f"{self.name}-ds-{ds.name}-dynamo-policy", 128),
                role=role.name,
                policy=table.arn.apply(
                    lambda arn: json.dumps(
                        {
                            "Version": "2012-10-17",
                            "Statement": [
                                {
                                    "Effect": "Allow",
                                    "Action": [
                                        "dynamodb:GetItem",
                                        "dynamodb:PutItem",
                                        "dynamodb:UpdateItem",
                                        "dynamodb:DeleteItem",
                                        "dynamodb:Query",
                                        "dynamodb:Scan",
                                    ],
                                    "Resource": [arn, f"{arn}/index/*"],
                                }
                            ],
                        }
                    )
                ),
            )
            outputs.append(policy.id)

        return outputs

    def _create_pipe_functions(
        self,
        graphql_api: appsync.GraphQLApi,
    ) -> dict[str, appsync.Function]:
        """Create AppSync Functions (pipeline steps)."""
        prefix = context().prefix
        pf_resources: dict[str, appsync.Function] = {}

        for pf_name, pf in self._pipe_functions.items():
            ds_api_name = pf.data_source.name if pf.data_source is not None else "NONE"

            appsync_fn = appsync.Function(
                safe_name(prefix(), f"{self.name}-fn-{pf_name}", 128),
                **_merge_customize(
                    {
                        "api_id": graphql_api.id,
                        "name": pf_name,
                        "data_source": ds_api_name,
                        "code": _read_file_or_inline(pf.code),
                        "runtime": appsync.FunctionRuntimeArgs(
                            name=APPSYNC_JS_RUNTIME,
                            runtime_version=APPSYNC_JS_RUNTIME_VERSION,
                        ),
                    },
                    pf.customize.get("function") if pf.customize else None,
                ),
            )
            pf_resources[pf_name] = appsync_fn
            pf._set_resources(AppSyncPipeFunctionResources(function=appsync_fn))  # noqa: SLF001

        return pf_resources

    def _create_resolver(
        self,
        resolver: AppSyncResolver,
        graphql_api: appsync.GraphQLApi,
        pf_resources: dict[str, appsync.Function],
    ) -> appsync.Resolver:
        """Create a Pulumi Resolver resource."""
        prefix = context().prefix

        resolver_args: dict[str, Any] = {
            "api_id": graphql_api.id,
            "type": resolver.type_name,
            "field": resolver.field_name,
            "runtime": appsync.ResolverRuntimeArgs(
                name=APPSYNC_JS_RUNTIME,
                runtime_version=APPSYNC_JS_RUNTIME_VERSION,
            ),
        }

        if resolver.is_pipeline:
            _build_pipeline_resolver_args(resolver, resolver_args, pf_resources)
        else:
            _build_unit_resolver_args(resolver, resolver_args)

        pulumi_resolver = appsync.Resolver(
            safe_name(prefix(), f"{self.name}-{resolver.type_name}-{resolver.field_name}", 128),
            **_merge_customize(
                resolver_args,
                resolver.customize.get("resolver") if resolver.customize else None,
            ),
        )
        resolver._set_resources(AppSyncResolverResources(resolver=pulumi_resolver))  # noqa: SLF001
        return pulumi_resolver

    def _create_custom_domain(self, graphql_api: appsync.GraphQLApi) -> list[Output[str]]:
        """Create custom domain with ACM certificate and DNS."""
        from stelvio.aws import acm  # noqa: PLC0415
        from stelvio.dns import DnsProviderNotConfiguredError  # noqa: PLC0415

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


def _build_pipeline_resolver_args(
    resolver: AppSyncResolver,
    resolver_args: dict[str, Any],
    pf_resources: dict[str, appsync.Function],
) -> None:
    functions = resolver.data_source
    resolver_args["kind"] = "PIPELINE"
    resolver_args["pipeline_config"] = appsync.ResolverPipelineConfigArgs(
        functions=[pf_resources[pf.name].function_id for pf in functions],
    )
    resolver_args["code"] = (
        _read_file_or_inline(resolver.code) if resolver.code else NONE_PASSTHROUGH_CODE
    )


def _build_unit_resolver_args(
    resolver: AppSyncResolver,
    resolver_args: dict[str, Any],
) -> None:
    resolver_args["kind"] = "UNIT"

    if resolver.data_source is None:
        resolver_args["data_source"] = "NONE"
        resolver_args["code"] = (
            _read_file_or_inline(resolver.code) if resolver.code else NONE_PASSTHROUGH_CODE
        )
    else:
        ds = resolver.data_source
        resolver_args["data_source"] = ds.name

        if ds.ds_type == DS_TYPE_LAMBDA and resolver.code is None:
            # Direct Lambda Resolver — no code, no runtime
            del resolver_args["runtime"]
        elif resolver.code:
            resolver_args["code"] = _read_file_or_inline(resolver.code)


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
