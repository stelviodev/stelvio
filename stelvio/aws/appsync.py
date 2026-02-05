"""AWS AppSync GraphQL API component for Stelvio."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, TypedDict, Unpack, final

import pulumi
from pulumi import Output, ResourceOptions
from pulumi_aws import appsync, iam, lambda_

from stelvio import context
from stelvio.aws.function import (
    Function,
    FunctionConfig,
    FunctionConfigDict,
    parse_handler_config,
)
from stelvio.aws.permission import AwsPermission
from stelvio.component import Component, link_config_creator, safe_name
from stelvio.link import LinkableMixin, LinkConfig

if TYPE_CHECKING:
    from collections.abc import Sequence

# AWS resource name limits
MAX_APPSYNC_NAME_LENGTH = 64
MAX_IAM_ROLE_NAME_LENGTH = 64


# =============================================================================
# Authentication Configuration TypedDicts
# =============================================================================


class CognitoAuthConfig(TypedDict):
    """Configuration for Cognito User Pool authentication.

    Attributes:
        type: Must be "AMAZON_COGNITO_USER_POOLS"
        user_pool_id: The Cognito User Pool ID
        aws_region: AWS region of the User Pool (optional, defaults to current region)
        app_id_client_regex: Regular expression for validating client IDs (optional)
    """

    type: Literal["AMAZON_COGNITO_USER_POOLS"]
    user_pool_id: str
    aws_region: str | None
    app_id_client_regex: str | None


class OidcAuthConfig(TypedDict):
    """Configuration for OpenID Connect authentication.

    Attributes:
        type: Must be "OPENID_CONNECT"
        issuer: The OIDC provider's issuer URL
        client_id: The client identifier (optional)
        auth_ttl: Token validity duration in milliseconds (optional)
        iat_ttl: Issued-at claim validity duration in milliseconds (optional)
    """

    type: Literal["OPENID_CONNECT"]
    issuer: str
    client_id: str | None
    auth_ttl: int | None
    iat_ttl: int | None


class LambdaAuthConfig(TypedDict, total=False):
    """Configuration for Lambda authorizer authentication.

    The Lambda function receives the authorization token and must return
    authorization decisions.

    Required Attributes:
        type: Must be "AWS_LAMBDA"
        handler: Lambda handler specification

    Optional Attributes:
        identity_validation_expression: Regex to validate auth token format
        authorizer_result_ttl: Cache TTL for auth results in seconds
    """

    type: Literal["AWS_LAMBDA"]
    handler: str | FunctionConfig | FunctionConfigDict | Function
    identity_validation_expression: str
    authorizer_result_ttl: int


# Union type for all additional auth configurations
AdditionalAuthConfig = CognitoAuthConfig | OidcAuthConfig | LambdaAuthConfig


class AppSyncCustomizationDict(TypedDict, total=False):
    """Customization options for AppSync resources."""

    api: appsync.GraphQLApiArgs | dict[str, Any] | None


# =============================================================================
# Data Source Types
# =============================================================================


class RdsDataSourceConfig(TypedDict):
    """Configuration for RDS data source.

    Attributes:
        cluster_arn: ARN of the Aurora Serverless cluster
        secret_arn: ARN of the Secrets Manager secret for credentials
        database_name: Name of the database (optional)
    """

    cluster_arn: str
    secret_arn: str
    database_name: str | None


# =============================================================================
# Return Dataclasses
# =============================================================================


@final
@dataclass(frozen=True)
class AppSyncResources:
    """Resources created by the AppSync component.

    Attributes:
        api: The AppSync GraphQL API resource
        api_key: The API key resource (if API_KEY auth is used)
        data_sources: Dict of data source name to DataSource resource
        functions: Dict of function name to Function resource
        resolvers: List of Resolver resources
    """

    api: appsync.GraphQLApi
    api_key: appsync.ApiKey | None
    data_sources: dict[str, appsync.DataSource] = field(default_factory=dict)
    functions: dict[str, appsync.Function] = field(default_factory=dict)
    resolvers: list[appsync.Resolver] = field(default_factory=list)


@final
@dataclass(frozen=True)
class AppSyncDataSource:
    """Represents an AppSync data source.

    Attributes:
        name: The data source name for use in functions and resolvers
        resource: The underlying Pulumi data source resource
    """

    name: str
    resource: appsync.DataSource


@final
@dataclass(frozen=True)
class AppSyncFunction:
    """Represents an AppSync pipeline function.

    Attributes:
        name: The function name for use in pipeline resolvers
        resource: The underlying Pulumi function resource
    """

    name: str
    resource: appsync.Function


@final
@dataclass(frozen=True)
class AppSyncResolver:
    """Represents an AppSync resolver.

    Attributes:
        type_name: The GraphQL type (e.g., "Query", "Mutation")
        field_name: The field name on the type
        resource: The underlying Pulumi resolver resource
    """

    type_name: str
    field_name: str
    resource: appsync.Resolver


# =============================================================================
# Internal Data Structures
# =============================================================================


@dataclass
class _DataSourceConfig:
    """Internal configuration for a data source."""

    name: str
    ds_type: Literal["lambda", "dynamodb", "http", "eventbridge", "opensearch", "rds", "none"]
    lambda_handler: FunctionConfig | Function | None = None
    dynamodb_table_name: str | None = None
    dynamodb_region: str | None = None
    http_endpoint: str | None = None
    eventbridge_bus_arn: str | None = None
    opensearch_endpoint: str | None = None
    rds_config: RdsDataSourceConfig | None = None


@dataclass
class _FunctionConfig:
    """Internal configuration for a pipeline function."""

    name: str
    data_source: str
    code: str | None = None
    request_template: str | None = None
    response_template: str | None = None


@dataclass
class _ResolverConfig:
    """Internal configuration for a resolver."""

    type_name: str
    field_name: str
    kind: Literal["UNIT", "PIPELINE"]
    data_source: str | None = None
    functions: list[str] = field(default_factory=list)
    code: str | None = None
    request_template: str | None = None
    response_template: str | None = None


# =============================================================================
# Main Component
# =============================================================================


@final
class AppSync(Component[AppSyncResources, AppSyncCustomizationDict], LinkableMixin):
    """AWS AppSync GraphQL API component.

    Creates a fully managed GraphQL API with support for multiple data sources,
    pipeline functions, and resolvers.

    Args:
        name: Unique name for the AppSync API
        schema: GraphQL schema as file path or inline SDL string.
            If the value is a path to an existing file, the schema is loaded from that file.
            Otherwise, it's treated as an inline GraphQL schema definition.
        api_key_expires: Number of days until the API key expires (default: 365).
            Set to 0 to disable API key authentication entirely.
        additional_auth: List of additional authentication providers.
            Each provider can be CognitoAuthConfig, OidcAuthConfig, or LambdaAuthConfig.
        customize: Customization dictionary for underlying Pulumi resources

    Examples:
        Basic API with schema file:
            api = AppSync("my-api", schema="schema.graphql")

        Inline schema:
            api = AppSync("my-api", schema='''
                type Query {
                    hello: String
                }
            ''')

        With Cognito authentication:
            api = AppSync(
                "my-api",
                schema="schema.graphql",
                additional_auth=[{
                    "type": "AMAZON_COGNITO_USER_POOLS",
                    "user_pool_id": "us-east-1_xxxxx",
                    "aws_region": "us-east-1",
                }]
            )
    """

    _data_sources: list[_DataSourceConfig]
    _functions: list[_FunctionConfig]
    _resolvers: list[_ResolverConfig]
    _created_data_sources: dict[str, AppSyncDataSource]
    _created_functions: dict[str, AppSyncFunction]
    _created_lambda_auth_functions: list[Function]

    def __init__(
        self,
        name: str,
        schema: str,
        /,
        *,
        api_key_expires: int = 365,
        additional_auth: Sequence[AdditionalAuthConfig] | None = None,
        customize: AppSyncCustomizationDict | None = None,
    ):
        super().__init__(name, customize=customize)

        if not schema:
            raise ValueError("Schema cannot be empty")
        if api_key_expires < 0:
            raise ValueError("api_key_expires must be non-negative")

        self._schema = schema
        self._api_key_expires = api_key_expires
        self._additional_auth = list(additional_auth) if additional_auth else []

        # Validate additional auth configurations
        self._validate_additional_auth()

        # Initialize collections
        self._data_sources = []
        self._functions = []
        self._resolvers = []
        self._created_data_sources = {}
        self._created_functions = {}
        self._created_lambda_auth_functions = []

    def _validate_additional_auth(self) -> None:
        """Validate additional authentication configurations."""
        for auth in self._additional_auth:
            auth_type = auth.get("type")
            if auth_type == "AMAZON_COGNITO_USER_POOLS":
                if not auth.get("user_pool_id"):
                    raise ValueError("Cognito auth requires 'user_pool_id'")
            elif auth_type == "OPENID_CONNECT":
                if not auth.get("issuer"):
                    raise ValueError("OIDC auth requires 'issuer'")
            elif auth_type == "AWS_LAMBDA":
                if not auth.get("handler"):
                    raise ValueError("Lambda auth requires 'handler'")
            else:
                raise ValueError(f"Unknown auth type: {auth_type}")

    def _check_not_created(self) -> None:
        """Raise error if resources have already been created."""
        if self._resources is not None:
            raise RuntimeError(
                f"Cannot modify AppSync '{self.name}' after resources have been created. "
                "Add all data sources, functions, and resolvers before accessing .resources."
            )

    @property
    def url(self) -> Output[str]:
        """Get the GraphQL API endpoint URL."""
        return self.resources.api.uris["GRAPHQL"]

    @property
    def api_id(self) -> Output[str]:
        """Get the GraphQL API ID."""
        return self.resources.api.id

    @property
    def arn(self) -> Output[str]:
        """Get the GraphQL API ARN."""
        return self.resources.api.arn

    # =========================================================================
    # add_data_source
    # =========================================================================

    def add_data_source(  # noqa: PLR0913
        self,
        name: str,
        /,
        *,
        handler: str | FunctionConfig | FunctionConfigDict | Function | None = None,
        dynamodb: str | None = None,
        dynamodb_region: str | None = None,
        http: str | None = None,
        eventbridge: str | None = None,
        opensearch: str | None = None,
        rds: RdsDataSourceConfig | None = None,
        none: bool = False,
        **opts: Unpack[FunctionConfigDict],
    ) -> AppSyncDataSource:
        """Add a data source to the AppSync API.

        Exactly one data source type must be specified.

        Args:
            name: Unique name for the data source
            handler: Lambda function handler for Lambda data sources.
                Can be a handler path string, FunctionConfig, dict, or Function instance.
            dynamodb: DynamoDB table name for DynamoDB data sources
            dynamodb_region: AWS region for the DynamoDB table (optional)
            http: HTTP endpoint URL for HTTP data sources
            eventbridge: EventBridge event bus ARN for EventBridge data sources
            opensearch: OpenSearch endpoint URL for OpenSearch data sources
            rds: RDS cluster configuration for RDS data sources
            none: If True, creates a NONE data source (for local resolvers)
            **opts: Additional Lambda function configuration when using handler string

        Returns:
            AppSyncDataSource with name property for use in functions and resolvers

        Raises:
            ValueError: If no data source type or multiple types are specified
            ValueError: If a data source with the same name already exists
            RuntimeError: If called after resources have been created

        Examples:
            Lambda data source:
                ds = api.add_data_source("users", handler="functions/users.handler")

            DynamoDB data source:
                ds = api.add_data_source("users-table", dynamodb="users")

            HTTP data source:
                ds = api.add_data_source("rest-api", http="https://api.example.com")

            None data source (local resolver):
                ds = api.add_data_source("local", none=True)
        """
        self._check_not_created()

        # Validate name uniqueness
        if any(ds.name == name for ds in self._data_sources):
            raise ValueError(f"Data source '{name}' already exists")

        # Determine data source type - exactly one must be specified
        sources = [
            ("lambda", handler is not None or bool(opts)),
            ("dynamodb", dynamodb is not None),
            ("http", http is not None),
            ("eventbridge", eventbridge is not None),
            ("opensearch", opensearch is not None),
            ("rds", rds is not None),
            ("none", none),
        ]
        specified = [s for s in sources if s[1]]

        if len(specified) == 0:
            raise ValueError(
                "Must specify exactly one data source type: "
                "handler, dynamodb, http, eventbridge, opensearch, rds, or none=True"
            )
        if len(specified) > 1:
            types_specified = [s[0] for s in specified]
            raise ValueError(f"Cannot specify multiple data source types. Got: {types_specified}")

        ds_type = specified[0][0]

        # Parse Lambda handler configuration
        lambda_config: FunctionConfig | Function | None = None
        if ds_type == "lambda":
            lambda_config = self._parse_handler(handler, opts)

        config = _DataSourceConfig(
            name=name,
            ds_type=ds_type,
            lambda_handler=lambda_config,
            dynamodb_table_name=dynamodb,
            dynamodb_region=dynamodb_region,
            http_endpoint=http,
            eventbridge_bus_arn=eventbridge,
            opensearch_endpoint=opensearch,
            rds_config=rds,
        )
        self._data_sources.append(config)

        # Return a placeholder that will be populated after resource creation
        # For now, return a dataclass with just the name
        return AppSyncDataSource(name=name, resource=None)  # type: ignore[arg-type]

    @staticmethod
    def _parse_handler(
        handler: str | FunctionConfig | FunctionConfigDict | Function | None,
        opts: FunctionConfigDict,
    ) -> FunctionConfig | Function:
        """Parse handler specification into FunctionConfig or Function."""
        if isinstance(handler, Function):
            if opts:
                raise ValueError(
                    "Invalid configuration: cannot combine Function instance "
                    "with additional options"
                )
            return handler

        # Use the shared parse_handler_config for other cases
        return parse_handler_config(handler, opts)

    # =========================================================================
    # add_function
    # =========================================================================

    def add_function(
        self,
        name: str,
        data_source: str | AppSyncDataSource,
        /,
        *,
        code: str | None = None,
        request_template: str | None = None,
        response_template: str | None = None,
    ) -> AppSyncFunction:
        """Add a pipeline function to the AppSync API.

        Pipeline functions are used in pipeline resolvers to compose
        multiple operations.

        Args:
            name: Unique name for the function
            data_source: Data source name or AppSyncDataSource object
            code: JavaScript (APPSYNC_JS) resolver code
            request_template: VTL request mapping template (for VTL resolvers)
            response_template: VTL response mapping template (for VTL resolvers)

        Returns:
            AppSyncFunction with name property for use in pipeline resolvers

        Raises:
            ValueError: If both code and VTL templates are specified
            ValueError: If a function with the same name already exists
            RuntimeError: If called after resources have been created

        Examples:
            JavaScript function:
                fn = api.add_function(
                    "get-user",
                    "users-ds",
                    code='''
                        export function request(ctx) {
                            return { operation: "GetItem", key: { id: ctx.args.id } };
                        }
                        export function response(ctx) {
                            return ctx.result;
                        }
                    '''
                )

            VTL function:
                fn = api.add_function(
                    "get-user",
                    "users-ds",
                    request_template='{"version": "2018-05-29", "operation": "GetItem"}',
                    response_template='$util.toJson($ctx.result)'
                )
        """
        self._check_not_created()

        # Validate name uniqueness
        if any(f.name == name for f in self._functions):
            raise ValueError(f"Function '{name}' already exists")

        # Validate resolver type - can't mix JS and VTL
        if code and (request_template or response_template):
            raise ValueError("Cannot specify both 'code' (JS) and VTL templates")

        # Get data source name
        ds_name = data_source.name if isinstance(data_source, AppSyncDataSource) else data_source

        # Validate data source exists
        if not any(ds.name == ds_name for ds in self._data_sources):
            raise ValueError(
                f"Data source '{ds_name}' not found. "
                "Add the data source before creating functions that use it."
            )

        config = _FunctionConfig(
            name=name,
            data_source=ds_name,
            code=code,
            request_template=request_template,
            response_template=response_template,
        )
        self._functions.append(config)

        return AppSyncFunction(name=name, resource=None)  # type: ignore[arg-type]

    # =========================================================================
    # add_resolver
    # =========================================================================

    def add_resolver(  # noqa: PLR0913 C901 PLR0912
        self,
        operation: str,
        /,
        *,
        kind: Literal["unit", "pipeline"] = "unit",
        data_source: str | AppSyncDataSource | None = None,
        functions: Sequence[str | AppSyncFunction] | None = None,
        code: str | None = None,
        request_template: str | None = None,
        response_template: str | None = None,
    ) -> AppSyncResolver:
        """Add a resolver to the AppSync API.

        Args:
            operation: GraphQL operation in format "Type field" (e.g., "Query getUser")
            kind: Resolver type - "unit" for single data source, "pipeline" for multiple functions
            data_source: Data source name or object (required for unit resolvers)
            functions: List of function names or objects (required for pipeline resolvers)
            code: JavaScript (APPSYNC_JS) resolver code
            request_template: VTL request mapping template
            response_template: VTL response mapping template

        Returns:
            AppSyncResolver with type_name and field_name properties

        Raises:
            ValueError: If operation format is invalid
            ValueError: If unit resolver missing data_source or pipeline resolver missing functions
            ValueError: If a resolver for the same operation already exists
            RuntimeError: If called after resources have been created

        Examples:
            Unit resolver:
                api.add_resolver(
                    "Query getUser",
                    data_source="users-ds",
                    code='''
                        export function request(ctx) {
                            return { operation: "GetItem", key: { id: ctx.args.id } };
                        }
                        export function response(ctx) {
                            return ctx.result;
                        }
                    '''
                )

            Pipeline resolver:
                api.add_resolver(
                    "Mutation createOrder",
                    kind="pipeline",
                    functions=["validate-input", "create-order", "send-notification"],
                    code='''
                        export function request(ctx) { return {}; }
                        export function response(ctx) { return ctx.prev.result; }
                    '''
                )
        """
        self._check_not_created()

        # Parse operation
        parts = operation.split(maxsplit=1)
        if len(parts) != 2:  # noqa: PLR2004
            raise ValueError(
                f"Invalid operation format: '{operation}'. "
                "Expected 'Type field' (e.g., 'Query getUser', 'Mutation createUser')"
            )
        type_name, field_name = parts

        # Validate no duplicate resolver
        if any(r.type_name == type_name and r.field_name == field_name for r in self._resolvers):
            raise ValueError(f"Resolver for '{operation}' already exists")

        # Validate resolver configuration based on kind
        kind_upper = kind.upper()
        if kind_upper == "UNIT":
            if data_source is None:
                raise ValueError("Unit resolver requires 'data_source'")
            if functions:
                raise ValueError("Unit resolver cannot have 'functions' - use kind='pipeline'")
        else:  # PIPELINE
            if not functions:
                raise ValueError("Pipeline resolver requires 'functions'")
            if data_source is not None:
                raise ValueError(
                    "Pipeline resolver cannot have 'data_source' - use functions instead"
                )

        # Validate code vs VTL
        if code and (request_template or response_template):
            raise ValueError("Cannot specify both 'code' (JS) and VTL templates")

        # Get data source name if specified
        ds_name: str | None = None
        if data_source is not None:
            ds_name = (
                data_source.name if isinstance(data_source, AppSyncDataSource) else data_source
            )
            # Validate data source exists
            if not any(ds.name == ds_name for ds in self._data_sources):
                raise ValueError(f"Data source '{ds_name}' not found")

        # Get function names
        fn_names: list[str] = []
        if functions:
            for fn in functions:
                fn_name = fn.name if isinstance(fn, AppSyncFunction) else fn
                fn_names.append(fn_name)
                # Validate function exists
                if not any(f.name == fn_name for f in self._functions):
                    raise ValueError(
                        f"Function '{fn_name}' not found. "
                        "Add the function before creating resolvers that use it."
                    )

        config = _ResolverConfig(
            type_name=type_name,
            field_name=field_name,
            kind=kind_upper,
            data_source=ds_name,
            functions=fn_names,
            code=code,
            request_template=request_template,
            response_template=response_template,
        )
        self._resolvers.append(config)

        return AppSyncResolver(
            type_name=type_name,
            field_name=field_name,
            resource=None,  # type: ignore[arg-type]
        )

    # =========================================================================
    # Resource Creation
    # =========================================================================

    def _load_schema(self) -> str:
        """Load schema from file or return inline schema."""
        schema_path = Path(self._schema)
        if schema_path.exists():
            return schema_path.read_text()
        return self._schema

    def _build_auth_config(self) -> dict[str, Any]:
        """Build authentication configuration for the API."""
        # Default authentication is API_KEY if api_key_expires > 0
        if self._api_key_expires > 0:
            default_auth_type = "API_KEY"
        elif self._additional_auth:
            # Use first additional auth as default
            default_auth_type = self._additional_auth[0]["type"]
        else:
            raise ValueError(
                "At least one authentication method is required. "
                "Either set api_key_expires > 0 or provide additional_auth."
            )

        config: dict[str, Any] = {"authentication_type": default_auth_type}

        # Build additional auth providers
        additional_providers = []
        auth_list = (
            self._additional_auth if self._api_key_expires > 0 else self._additional_auth[1:]
        )

        for auth in auth_list:
            auth_type = auth["type"]
            if auth_type == "AMAZON_COGNITO_USER_POOLS":
                additional_providers.append(
                    {
                        "authentication_type": auth_type,
                        "user_pool_config": {
                            "user_pool_id": auth["user_pool_id"],
                            "aws_region": auth.get("aws_region"),
                            "app_id_client_regex": auth.get("app_id_client_regex"),
                        },
                    }
                )
            elif auth_type == "OPENID_CONNECT":
                additional_providers.append(
                    {
                        "authentication_type": auth_type,
                        "openid_connect_config": {
                            "issuer": auth["issuer"],
                            "client_id": auth.get("client_id"),
                            "auth_ttl": auth.get("auth_ttl"),
                            "iat_ttl": auth.get("iat_ttl"),
                        },
                    }
                )
            elif auth_type == "AWS_LAMBDA":
                # Lambda auth requires creating the function first - handled separately
                # The function and permission are created in _create_resources
                additional_providers.append(
                    {
                        "authentication_type": auth_type,
                        # lambda_authorizer_config is added in _create_resources
                    }
                )

        if additional_providers:
            config["additional_authentication_providers"] = additional_providers

        return config

    def _create_resources(self) -> AppSyncResources:
        schema_definition = self._load_schema()

        # Build auth config (basic structure)
        auth_config = self._build_auth_config()

        # Handle Lambda authorizers - create functions first
        lambda_authorizer_configs = self._create_lambda_auth_functions()

        # Update additional auth providers with Lambda ARNs
        if "additional_authentication_providers" in auth_config:
            for _i, provider in enumerate(auth_config["additional_authentication_providers"]):
                if provider["authentication_type"] == "AWS_LAMBDA":
                    lambda_config = lambda_authorizer_configs.pop(0)
                    provider["lambda_authorizer_config"] = lambda_config

        # Create the GraphQL API
        api = appsync.GraphQLApi(
            safe_name(context().prefix(), f"{self.name}-api", MAX_APPSYNC_NAME_LENGTH),
            **self._customizer(
                "api",
                {
                    "name": f"{context().prefix()}{self.name}",
                    "schema": schema_definition,
                    **auth_config,
                },
            ),
        )

        # Create API key if using API_KEY auth
        api_key: appsync.ApiKey | None = None
        if self._api_key_expires > 0:
            expires_at = datetime.now(tz=UTC) + timedelta(days=self._api_key_expires)
            api_key = appsync.ApiKey(
                safe_name(context().prefix(), f"{self.name}-key", MAX_APPSYNC_NAME_LENGTH),
                api_id=api.id,
                expires=expires_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            )

        # Create data sources
        for ds_config in self._data_sources:
            ds = self._create_data_source(api, ds_config)
            self._created_data_sources[ds_config.name] = AppSyncDataSource(
                name=ds_config.name, resource=ds
            )

        # Create functions
        for fn_config in self._functions:
            fn = self._create_function(api, fn_config)
            self._created_functions[fn_config.name] = AppSyncFunction(
                name=fn_config.name, resource=fn
            )

        # Create resolvers
        created_resolvers: list[appsync.Resolver] = []
        for resolver_config in self._resolvers:
            resolver = self._create_resolver(api, resolver_config)
            created_resolvers.append(resolver)

        # Export outputs
        pulumi.export(f"appsync_{self.name}_url", api.uris["GRAPHQL"])
        pulumi.export(f"appsync_{self.name}_id", api.id)
        pulumi.export(f"appsync_{self.name}_arn", api.arn)
        if api_key:
            pulumi.export(f"appsync_{self.name}_api_key", api_key.key)

        return AppSyncResources(
            api=api,
            api_key=api_key,
            data_sources={name: ds.resource for name, ds in self._created_data_sources.items()},
            functions={name: fn.resource for name, fn in self._created_functions.items()},
            resolvers=created_resolvers,
        )

    def _create_lambda_auth_functions(self) -> list[dict]:
        """Create Lambda authorizer functions and return their configs."""
        configs = []
        for auth in self._additional_auth:
            if auth.get("type") != "AWS_LAMBDA":
                continue

            handler_input = auth.get("handler")
            if isinstance(handler_input, Function):
                fn = handler_input
            else:
                # Parse handler config
                handler_config = self._parse_handler(handler_input, {})
                fn = Function(
                    f"{self.name}-auth-{len(self._created_lambda_auth_functions)}",
                    handler_config,
                )

            self._created_lambda_auth_functions.append(fn)

            # Build lambda authorizer config
            config: dict[str, Any] = {
                "authorizer_uri": fn.resources.function.invoke_arn,
            }
            if auth.get("identity_validation_expression"):
                config["identity_validation_expression"] = auth["identity_validation_expression"]
            if auth.get("authorizer_result_ttl"):
                config["authorizer_result_ttl_in_seconds"] = auth["authorizer_result_ttl"]

            configs.append(config)

            # Create permission for AppSync to invoke Lambda
            lambda_.Permission(
                safe_name(
                    context().prefix(),
                    f"{self.name}-auth-{len(self._created_lambda_auth_functions)}-perm",
                    128,
                ),
                action="lambda:InvokeFunction",
                function=fn.function_name,
                principal="appsync.amazonaws.com",
            )

        return configs

    def _create_data_source(
        self, api: appsync.GraphQLApi, config: _DataSourceConfig
    ) -> appsync.DataSource:
        """Create a data source resource."""
        ds_name = safe_name(
            context().prefix(), f"{self.name}-ds-{config.name}", MAX_APPSYNC_NAME_LENGTH
        )

        # Common properties
        props: dict[str, Any] = {
            "api_id": api.id,
            "name": config.name,
        }

        # Create service role and type-specific config
        if config.ds_type == "lambda":
            role = self._create_lambda_ds_role(config.name)
            props["type"] = "AWS_LAMBDA"
            props["service_role_arn"] = role.arn

            # Create or get Lambda function
            if isinstance(config.lambda_handler, Function):
                fn = config.lambda_handler
            else:
                fn = Function(f"{self.name}-ds-{config.name}-fn", config.lambda_handler)

            props["lambda_config"] = {"function_arn": fn.resources.function.arn}

            # Add permission for AppSync to invoke Lambda
            lambda_.Permission(
                safe_name(context().prefix(), f"{self.name}-ds-{config.name}-perm", 128),
                action="lambda:InvokeFunction",
                function=fn.function_name,
                principal="appsync.amazonaws.com",
                source_arn=api.arn,
            )

        elif config.ds_type == "dynamodb":
            role = self._create_dynamodb_ds_role(config.name, config.dynamodb_table_name)
            props["type"] = "AMAZON_DYNAMODB"
            props["service_role_arn"] = role.arn
            props["dynamodb_config"] = {
                "table_name": config.dynamodb_table_name,
            }
            if config.dynamodb_region:
                props["dynamodb_config"]["region"] = config.dynamodb_region

        elif config.ds_type == "http":
            props["type"] = "HTTP"
            props["http_config"] = {"endpoint": config.http_endpoint}

        elif config.ds_type == "eventbridge":
            role = self._create_eventbridge_ds_role(config.name, config.eventbridge_bus_arn)
            props["type"] = "AMAZON_EVENTBRIDGE"
            props["service_role_arn"] = role.arn
            props["event_bridge_config"] = {"event_bus_arn": config.eventbridge_bus_arn}

        elif config.ds_type == "opensearch":
            role = self._create_opensearch_ds_role(config.name, config.opensearch_endpoint)
            props["type"] = "AMAZON_OPENSEARCH_SERVICE"
            props["service_role_arn"] = role.arn
            props["opensearchservice_config"] = {"endpoint": config.opensearch_endpoint}

        elif config.ds_type == "rds":
            rds_cfg = config.rds_config
            role = self._create_rds_ds_role(config.name, rds_cfg)
            props["type"] = "RELATIONAL_DATABASE"
            props["service_role_arn"] = role.arn
            props["relational_database_config"] = {
                "http_endpoint_config": {
                    "aws_secret_store_arn": rds_cfg["secret_arn"],
                    "db_cluster_identifier": rds_cfg["cluster_arn"],
                },
            }
            if rds_cfg.get("database_name"):
                props["relational_database_config"]["http_endpoint_config"]["database_name"] = (
                    rds_cfg["database_name"]
                )

        else:  # none
            props["type"] = "NONE"

        return appsync.DataSource(ds_name, **props)

    def _create_lambda_ds_role(self, ds_name: str) -> iam.Role:
        """Create IAM role for Lambda data source."""
        role_name = safe_name(
            context().prefix(), f"{self.name}-ds-{ds_name}-role", MAX_IAM_ROLE_NAME_LENGTH
        )
        assume_role_policy = json.dumps(
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

        role = iam.Role(
            role_name,
            assume_role_policy=assume_role_policy,
        )

        # Attach Lambda invoke policy
        iam.RolePolicyAttachment(
            safe_name(context().prefix(), f"{self.name}-ds-{ds_name}-lambda-policy", 128),
            role=role.name,
            policy_arn="arn:aws:iam::aws:policy/service-role/AWSLambdaRole",
        )

        return role

    def _create_dynamodb_ds_role(self, ds_name: str, table_name: str) -> iam.Role:
        """Create IAM role for DynamoDB data source."""
        role_name = safe_name(
            context().prefix(), f"{self.name}-ds-{ds_name}-role", MAX_IAM_ROLE_NAME_LENGTH
        )
        assume_role_policy = json.dumps(
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

        role = iam.Role(
            role_name,
            assume_role_policy=assume_role_policy,
        )

        # Create inline policy for DynamoDB access
        policy_document = json.dumps(
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
                            "dynamodb:BatchGetItem",
                            "dynamodb:BatchWriteItem",
                        ],
                        "Resource": [
                            f"arn:aws:dynamodb:*:*:table/{table_name}",
                            f"arn:aws:dynamodb:*:*:table/{table_name}/*",
                        ],
                    }
                ],
            }
        )

        iam.RolePolicy(
            safe_name(context().prefix(), f"{self.name}-ds-{ds_name}-ddb-policy", 128),
            role=role.name,
            policy=policy_document,
        )

        return role

    def _create_eventbridge_ds_role(self, ds_name: str, bus_arn: str) -> iam.Role:
        """Create IAM role for EventBridge data source."""
        role_name = safe_name(
            context().prefix(), f"{self.name}-ds-{ds_name}-role", MAX_IAM_ROLE_NAME_LENGTH
        )
        assume_role_policy = json.dumps(
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

        role = iam.Role(
            role_name,
            assume_role_policy=assume_role_policy,
        )

        # Create inline policy for EventBridge access
        policy_document = json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": ["events:PutEvents"],
                        "Resource": [bus_arn],
                    }
                ],
            }
        )

        iam.RolePolicy(
            safe_name(context().prefix(), f"{self.name}-ds-{ds_name}-eb-policy", 128),
            role=role.name,
            policy=policy_document,
        )

        return role

    def _create_opensearch_ds_role(self, ds_name: str, _endpoint: str) -> iam.Role:
        """Create IAM role for OpenSearch data source."""
        role_name = safe_name(
            context().prefix(), f"{self.name}-ds-{ds_name}-role", MAX_IAM_ROLE_NAME_LENGTH
        )
        assume_role_policy = json.dumps(
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

        role = iam.Role(
            role_name,
            assume_role_policy=assume_role_policy,
        )

        # Attach OpenSearch policy
        iam.RolePolicyAttachment(
            safe_name(context().prefix(), f"{self.name}-ds-{ds_name}-os-policy", 128),
            role=role.name,
            policy_arn="arn:aws:iam::aws:policy/AmazonOpenSearchServiceFullAccess",
        )

        return role

    def _create_rds_ds_role(self, ds_name: str, rds_config: RdsDataSourceConfig) -> iam.Role:
        """Create IAM role for RDS data source."""
        role_name = safe_name(
            context().prefix(), f"{self.name}-ds-{ds_name}-role", MAX_IAM_ROLE_NAME_LENGTH
        )
        assume_role_policy = json.dumps(
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

        role = iam.Role(
            role_name,
            assume_role_policy=assume_role_policy,
        )

        # Create inline policy for RDS Data API and Secrets Manager
        policy_document = json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": [
                            "rds-data:ExecuteStatement",
                            "rds-data:BatchExecuteStatement",
                            "rds-data:BeginTransaction",
                            "rds-data:CommitTransaction",
                            "rds-data:RollbackTransaction",
                        ],
                        "Resource": [rds_config["cluster_arn"]],
                    },
                    {
                        "Effect": "Allow",
                        "Action": ["secretsmanager:GetSecretValue"],
                        "Resource": [rds_config["secret_arn"]],
                    },
                ],
            }
        )

        iam.RolePolicy(
            safe_name(context().prefix(), f"{self.name}-ds-{ds_name}-rds-policy", 128),
            role=role.name,
            policy=policy_document,
        )

        return role

    def _create_function(
        self, api: appsync.GraphQLApi, config: _FunctionConfig
    ) -> appsync.Function:
        """Create an AppSync pipeline function."""
        fn_name = safe_name(
            context().prefix(), f"{self.name}-fn-{config.name}", MAX_APPSYNC_NAME_LENGTH
        )

        # Get the created data source
        ds = self._created_data_sources.get(config.data_source)
        if not ds:
            raise ValueError(f"Data source '{config.data_source}' not found in created resources")

        props: dict[str, Any] = {
            "api_id": api.id,
            "data_source": ds.resource.name,
            "name": config.name,
        }

        # Add resolver code or VTL templates
        if config.code:
            props["code"] = config.code
            props["runtime"] = {
                "name": "APPSYNC_JS",
                "runtime_version": "1.0.0",
            }
        else:
            if config.request_template:
                props["request_mapping_template"] = config.request_template
            if config.response_template:
                props["response_mapping_template"] = config.response_template

        return appsync.Function(fn_name, **props)

    def _create_resolver(
        self, api: appsync.GraphQLApi, config: _ResolverConfig
    ) -> appsync.Resolver:
        """Create an AppSync resolver."""
        resolver_name = safe_name(
            context().prefix(),
            f"{self.name}-resolver-{config.type_name}-{config.field_name}",
            MAX_APPSYNC_NAME_LENGTH,
        )

        props: dict[str, Any] = {
            "api_id": api.id,
            "type": config.type_name,
            "field": config.field_name,
            "kind": config.kind,
        }

        # Add data source for unit resolvers
        if config.kind == "UNIT" and config.data_source:
            ds = self._created_data_sources.get(config.data_source)
            if ds:
                props["data_source"] = ds.resource.name

        # Add pipeline config for pipeline resolvers
        if config.kind == "PIPELINE" and config.functions:
            fn_arns = []
            for fn_name in config.functions:
                fn = self._created_functions.get(fn_name)
                if fn:
                    fn_arns.append(fn.resource.function_id)
            props["pipeline_config"] = {"functions": fn_arns}

        # Add resolver code or VTL templates
        if config.code:
            props["code"] = config.code
            props["runtime"] = {
                "name": "APPSYNC_JS",
                "runtime_version": "1.0.0",
            }
        else:
            if config.request_template:
                props["request_template"] = config.request_template
            if config.response_template:
                props["response_template"] = config.response_template

        return appsync.Resolver(
            resolver_name,
            **props,
            opts=ResourceOptions(
                depends_on=[
                    self._created_data_sources[ds].resource
                    for ds in ([config.data_source] if config.data_source else [])
                    if ds in self._created_data_sources
                ]
                + [
                    self._created_functions[fn].resource
                    for fn in config.functions
                    if fn in self._created_functions
                ]
            ),
        )


# =============================================================================
# Link Configuration
# =============================================================================


@link_config_creator(AppSync)
def default_appsync_link(appsync_api: AppSync) -> LinkConfig:
    """Default link configuration for AppSync component.

    Grants permissions to execute GraphQL operations.
    """
    api = appsync_api.resources.api
    return LinkConfig(
        properties={
            "url": api.uris["GRAPHQL"],
            "api_id": api.id,
            "arn": api.arn,
        },
        permissions=[
            AwsPermission(
                actions=["appsync:GraphQL"],
                resources=[api.arn.apply(lambda arn: f"{arn}/*")],
            ),
        ],
    )
