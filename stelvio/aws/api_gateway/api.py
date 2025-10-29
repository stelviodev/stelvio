from dataclasses import dataclass
from typing import Literal, Unpack, final

import pulumi
from pulumi import Output, ResourceOptions, StringAsset
from pulumi_aws import get_caller_identity, get_region
from pulumi_aws.apigateway import (
    Authorizer as PulumiAuthorizer,
)
from pulumi_aws.apigateway import (
    BasePathMapping,
    Deployment,
    DomainName,
    Integration,
    Method,
    Resource,
    RestApi,
    Stage,
)
from pulumi_aws.lambda_ import Permission

from stelvio import context
from stelvio.aws import acm
from stelvio.aws.api_gateway.config import ApiConfig, ApiConfigDict, _ApiRoute, _Authorizer
from stelvio.aws.api_gateway.constants import (
    DEFAULT_ENDPOINT_TYPE,
    DEFAULT_STAGE_NAME,
    HTTPMethodInput,
)
from stelvio.aws.api_gateway.deployment import _create_deployment
from stelvio.aws.api_gateway.iam import _create_api_gateway_account_and_role
from stelvio.aws.api_gateway.routing import (
    _create_routing_file,
    _get_group_config_map,
    _group_routes_by_lambda,
)
from stelvio.aws.function import Function, FunctionConfig, FunctionConfigDict
from stelvio.aws.function.function import FunctionAssetsRegistry
from stelvio.component import Component, safe_name
from stelvio.dns import DnsProviderNotConfiguredError


@final
@dataclass(frozen=True)
class ApiResources:
    rest_api: RestApi
    deployment: Deployment
    stage: Stage


@final
class Api(Component[ApiResources]):
    _routes: list[_ApiRoute]
    _config: ApiConfig
    _authorizers: list[_Authorizer]
    _default_auth: _Authorizer | Literal["IAM"] | None

    def __init__(
        self,
        name: str,
        config: ApiConfig | None = None,
        **opts: Unpack[ApiConfigDict],
    ) -> None:
        self._routes = []
        self._authorizers = []
        self._default_auth = None
        self._config = self._parse_config(config, opts)
        super().__init__(name)

    @staticmethod
    def _parse_config(config: ApiConfig | ApiConfigDict | None, opts: ApiConfigDict) -> ApiConfig:
        if config and opts:
            raise ValueError(
                "Invalid configuration: cannot combine 'config' parameter with additional options "
                "- provide all settings either in 'config' or as separate options"
            )
        if config is None:
            return ApiConfig(**opts)
        if isinstance(config, ApiConfig):
            return config
        if isinstance(config, dict):
            return ApiConfig(**config)

        raise TypeError(
            f"Invalid config type: expected ApiConfig or dict, got {type(config).__name__}"
        )

    @property
    def config(self) -> ApiConfig:
        return self._config

    @property
    def domain_name(self) -> str | None:
        return self._config.domain_name

    @property
    def invoke_url(self) -> Output[str]:
        """Get the invoke URL for this API."""
        return self.resources.stage.invoke_url

    @property
    def api_arn(self) -> Output[str]:
        """Get the ARN for this API."""
        return self.resources.rest_api.arn

    def _validate_authorizer_name(self, name: str) -> None:
        """Validate that authorizer name is unique within this API."""
        if any(auth.name == name for auth in self._authorizers):
            raise ValueError(
                f"Duplicate authorizer name: '{name}'. "
                f"Authorizer names must be unique within an API."
            )

    def _create_authorizer_permission(
        self,
        auth_name: str,
        function: Function,
        rest_api: RestApi,
        authorizer: PulumiAuthorizer,
    ) -> Permission:
        """Create Lambda permission for API Gateway to invoke authorizer function.

        This is created once per authorizer (TOKEN and REQUEST types only).
        """
        return Permission(
            safe_name(
                context().prefix(),
                f"{self.name}-authorizer-{auth_name}-permission",
                128,
            ),
            action="lambda:InvokeFunction",
            function=function.function_name,
            principal="apigateway.amazonaws.com",
            source_arn=pulumi.Output.all(rest_api.execution_arn, authorizer.id).apply(
                lambda args: f"{args[0]}/authorizers/{args[1]}"
            ),
        )

    def add_token_authorizer(
        self,
        name: str,
        handler: str | Function,
        /,
        *,
        identity_source: str = "method.request.header.Authorization",
        ttl: int = 300,
        **function_config: Unpack[FunctionConfigDict],
    ) -> _Authorizer:
        """Add a TOKEN authorizer (bearer token auth - JWT, OAuth).

        Args:
            name: Authorizer name
            handler: Lambda function path or Function instance
            identity_source: Header to extract token from (default: Authorization)
            ttl: Cache TTL in seconds (default: 300)
            **function_config: Function configuration (memory, timeout, links, etc.)

        Returns:
            _Authorizer instance to use in route() calls
        """
        self._validate_authorizer_name(name)

        # Create Function if handler is a string
        if isinstance(handler, str):
            function = Function(f"{self.name}-auth-{name}", handler=handler, **function_config)
        else:
            function = handler

        authorizer = _Authorizer(
            name=name,
            token_function=function,
            identity_source=identity_source,
            ttl=ttl,
        )
        self._authorizers.append(authorizer)
        return authorizer

    def add_request_authorizer(
        self,
        name: str,
        handler: str | Function,
        /,
        *,
        identity_source: str | list[str] = "method.request.header.Authorization",
        ttl: int = 300,
        **function_config: Unpack[FunctionConfigDict],
    ) -> _Authorizer:
        """Add a REQUEST authorizer (multi-source auth, full request context).

        Args:
            name: Authorizer name
            handler: Lambda function path or Function instance
            identity_source: Source(s) for auth data (header, query param, etc.).
                Can be a single source string or list of sources.
                Defaults to "method.request.header.Authorization"
            ttl: Cache TTL in seconds (default: 300)
            **function_config: Function configuration (memory, timeout, links, etc.)

        Returns:
            _Authorizer instance to use in route() calls
        """
        self._validate_authorizer_name(name)

        # Create Function if handler is a string
        if isinstance(handler, str):
            function = Function(f"{self.name}-auth-{name}", handler=handler, **function_config)
        else:
            function = handler

        # Normalize identity_source to list[str]
        normalized_sources = (
            [identity_source] if isinstance(identity_source, str) else identity_source
        )

        authorizer = _Authorizer(
            name=name,
            request_function=function,
            identity_source=normalized_sources,
            ttl=ttl,
        )
        self._authorizers.append(authorizer)
        return authorizer

    def add_cognito_authorizer(
        self,
        name: str,
        /,
        *,
        user_pools: list[str],
        ttl: int = 300,
    ) -> _Authorizer:
        """Add a Cognito User Pool authorizer.

        Args:
            name: Authorizer name
            user_pools: List of Cognito User Pool ARNs
            ttl: Cache TTL in seconds (default: 300)

        Returns:
            _Authorizer instance to use in route() calls
        """
        self._validate_authorizer_name(name)

        authorizer = _Authorizer(
            name=name,
            user_pools=user_pools,
            ttl=ttl,
        )
        self._authorizers.append(authorizer)
        return authorizer

    @property
    def default_auth(self) -> _Authorizer | Literal["IAM"] | None:
        """Get default authorization for all routes."""
        return self._default_auth

    @default_auth.setter
    def default_auth(self, auth: _Authorizer | Literal["IAM"] | None) -> None:
        """Set default authorization for all routes.

        Routes without explicit auth parameter will use this default.
        Routes can opt out with auth=False.

        Args:
            auth: Default authorizer, "IAM" for AWS IAM auth, or None for no default
        """
        self._default_auth = auth

    def route(
        self,
        http_method: HTTPMethodInput,
        path: str,
        handler: str | FunctionConfig | FunctionConfigDict | Function | None = None,
        /,
        *,
        auth: _Authorizer | Literal["IAM", False] | None = None,
        **opts: Unpack[FunctionConfigDict],
    ) -> None:
        """Add a route to the API.

        The route handler can be specified in three ways:
        1. As a complete configuration object (FunctionConfig, FunctionConfigDict, or Function)
        2. As a handler path string with optional FunctionConfigDict fields as kwargs
        3. As FunctionConfigDict fields passed directly as keyword arguments

        Args:
            http_method: HTTP method(s) to handle. Can be:
                - String ("GET", "POST", etc.)
                - HTTPMethod enum value
                - List of methods for multiple method support
                - "ANY" or "*" to handle all methods
            path: URL path for the route
            handler: Route handler specification. Can be:
                - Function handler path as string
                - Complete FunctionConfig object
                - FunctionConfigDict dictionary
                - Function instance
                - None (if handler is specified in opts)
            auth: Authorization for this route:
                - _Authorizer instance (from add_*_authorizer methods)
                - "IAM" for AWS IAM authentication
                - False to explicitly make route public (override default)
                - None to use default auth if set, otherwise public
            **opts: Additional FunctionConfigDict fields when using handler path

        Raises:
            ValueError: If the configuration is ambiguous or incomplete
            TypeError: If handler is of invalid type
            ValueError: If a route with the same path and method already exists

        Examples:
            # Single method
            api.route("GET", "/users", "users.index", memory=128)
            api.route(HTTPMethod.GET, "/users", "users.index")

            # With authorization
            auth = api.add_token_authorizer("jwt", "auth/jwt.handler")
            api.route("GET", "/users", "users.index", auth=auth)
            api.route("POST", "/admin", "admin.handler", auth="IAM")
            api.route("GET", "/health", "health.check", auth=False)

            # Multiple methods
            api.route(["GET", "POST"], "/users", "users.handle")

            # All methods
            api.route("ANY", "/users", "users.handle")

            # Configuration examples
            api.route("GET", "/users", {"handler": "users.index", "memory": 128})
            api.route("GET", "/users", handler="users.index", memory=128)

        """
        # Create the route object
        api_route = self._create_route(http_method, path, handler, auth, opts)

        # Check for duplicate routes
        for method in api_route.methods:
            for existing_route in self._routes:
                # Skip routes with different paths
                if path != existing_route.path:
                    continue
                if (  # Route conflict occurs when:
                    method in existing_route.methods  # Direct method match
                    or method in ("ANY", "*")  # Current route uses ANY
                    or any(
                        m in ("ANY", "*") for m in existing_route.methods
                    )  # Existing route uses ANY
                ):
                    raise ValueError(
                        f"Route conflict: {method} {path} conflicts with existing route."
                    )

        # Add the route if no conflicts found
        self._routes.append(api_route)

    @staticmethod
    def _create_route(
        http_method: HTTPMethodInput,
        path: str,
        handler: str | FunctionConfig | FunctionConfigDict | Function | None,
        auth: _Authorizer | Literal["IAM", False] | None,
        opts: dict,
    ) -> _ApiRoute:
        if isinstance(handler, dict | FunctionConfig | Function) and opts:
            raise ValueError(
                "Invalid configuration: cannot combine complete handler "
                "configuration with additional options"
            )

        if isinstance(handler, FunctionConfig | Function):
            return _ApiRoute(http_method, path, handler, auth=auth)

        if isinstance(handler, dict):
            return _ApiRoute(http_method, path, FunctionConfig(**handler), auth=auth)

        if isinstance(handler, str):
            if "handler" in opts:
                raise ValueError(
                    "Ambiguous handler configuration: handler is specified both as positional "
                    "argument and in options"
                )
            return _ApiRoute(http_method, path, FunctionConfig(handler=handler, **opts), auth=auth)

        if handler is None:
            if "handler" not in opts:
                raise ValueError(
                    "Missing handler configuration: when handler argument is None, "
                    "'handler' option must be provided"
                )
            return _ApiRoute(http_method, path, FunctionConfig(**opts), auth=auth)

        raise TypeError(
            f"Invalid handler type: expected str, FunctionConfig, dict, or Function, "
            f"got {type(handler).__name__}"
        )

    @staticmethod
    def path_to_resource_name(path_parts: list[str]) -> str:
        """Convert path parts to a valid resource name.
        Example: ['users', '{id}', 'orders'] -> 'users-id-orders'
        """
        # Remove any curly braces and convert to safe name
        safe_parts = [
            part.replace("{", "").replace("}", "").replace("+", "plus") for part in path_parts
        ]
        # TODO: check of longer than 256? if so cut the beginning or middle?
        return "-".join(safe_parts) or "root"

    def get_or_create_resource(
        self, path_parts: list[str], resources: dict[str, Resource], rest_api: RestApi
    ) -> Output[str]:
        if not path_parts:
            return rest_api.root_resource_id

        path_key = "/".join(path_parts)
        if path_key in resources:
            return resources[path_key].id

        part = path_parts[-1]
        parent_parts = path_parts[:-1]

        parent_resource_id = self.get_or_create_resource(parent_parts, resources, rest_api)
        resource = Resource(
            context().prefix(f"{self.name}-resource-{self.path_to_resource_name(path_parts)}"),
            rest_api=rest_api.id,
            parent_id=parent_resource_id,
            path_part=part,
        )
        resources[path_key] = resource
        return resource.id

    def _create_authorizers(self, rest_api: RestApi) -> dict[str, Output[str]]:
        """Create Pulumi Authorizer resources from configured authorizers.

        Returns:
            Mapping of authorizer name to Pulumi resource ID
        """
        authorizer_id_map: dict[str, Output[str]] = {}

        for auth in self._authorizers:
            # Determine authorizer type and build type-specific parameters
            func = None
            if auth.token_function is not None:
                func = auth.token_function
                type_params = {
                    "type": "TOKEN",
                    "authorizer_uri": func.invoke_arn,
                    "identity_source": auth.identity_source,
                }
            elif auth.request_function is not None:
                func = auth.request_function
                type_params = {
                    "type": "REQUEST",
                    "authorizer_uri": func.invoke_arn,
                    "identity_source": ",".join(auth.identity_source)
                    if auth.identity_source
                    else None,
                }
            else:  # auth.user_pools is not None
                type_params = {
                    "type": "COGNITO_USER_POOLS",
                    "provider_arns": auth.user_pools,
                }

            # Create authorizer with common + type-specific params
            pulumi_auth = PulumiAuthorizer(
                safe_name(context().prefix(), f"{self.name}-authorizer-{auth.name}", 128),
                rest_api=rest_api.id,
                name=auth.name,
                authorizer_result_ttl_in_seconds=auth.ttl,
                **type_params,
            )

            # Create Lambda permission for TOKEN and REQUEST authorizers
            if func is not None:
                self._create_authorizer_permission(auth.name, func, rest_api, pulumi_auth)

            authorizer_id_map[auth.name] = pulumi_auth.id

        return authorizer_id_map

    def _create_resources(self) -> ApiResources:
        # This is what needs to be done:
        #   1. create rest api
        #   2. for each route:
        #       a. create resource(s)
        #       b. create method(s)
        #       c. create lambda from handler if it doesn't exists (we need to group
        #           routes based on lambda)
        #       d. give lambda resource policy so it can be called by given
        #           gateway/resource/method
        #           https://docs.aws.amazon.com/lambda/latest/dg/access-control-resource-based.html
        #       e. create integration between method and lambda
        #   3. create role for gateway and give it permission to write to cloudwatch
        #   4. create account and give it a role
        #   5. create deployment
        #   6. create stage
        #   7. create custom domain name if specified
        #       a. create ACM certificate
        #           i. request certificate from aws acm
        #           ii. create validation record in DNS
        #           iii. wait for validation using `acm.CertificateValidation`
        #       b. create DNS record for the custom domain name
        #       c. create base path mapping
        endpoint_type = self._config.endpoint_type or DEFAULT_ENDPOINT_TYPE
        rest_api = RestApi(
            context().prefix(self.name), endpoint_configuration={"types": endpoint_type.upper()}
        )

        account = _create_api_gateway_account_and_role()

        authorizer_id_map = self._create_authorizers(rest_api)

        grouped_routes_by_lambda = _group_routes_by_lambda(self._routes)
        group_config_map = _get_group_config_map(grouped_routes_by_lambda)

        resources = {}

        # Create all method-integration pairs in a single comprehension
        method_integration_pairs = [
            pair
            for key, group in grouped_routes_by_lambda.items()
            for pair in self._create_route_resources(
                group,
                rest_api,
                self.get_group_function(key, rest_api, group_config_map[key], group),
                resources,
                authorizer_id_map,
            )
        ]

        # Flatten the pairs for deployment dependencies
        all_deployment_dependencies = [
            resource for pair in method_integration_pairs for resource in pair
        ]
        deployment = _create_deployment(
            rest_api, self.name, self._routes, all_deployment_dependencies
        )

        stage_name = self._config.stage_name or DEFAULT_STAGE_NAME
        stage = Stage(
            safe_name(context().prefix(), f"{self.name}-stage-{stage_name}", 128),
            rest_api=rest_api.id,
            deployment=deployment.id,
            stage_name=stage_name,
            # xray_tracing_enabled=True,
            access_log_settings={
                "destination_arn": rest_api.name.apply(
                    lambda name: f"arn:aws:logs:{get_region().name}:"
                    f"{get_caller_identity().account_id}"
                    f":log-group:/aws/apigateway/{name}"
                ),
                "format": '{"requestId":"$context.requestId", "ip": "$context.identity.sourceIp", '
                '"caller":"$context.identity.caller", "user":"$context.identity.user",'
                '"requestTime":"$context.requestTime", "httpMethod":'
                '"$context.httpMethod","resourcePath":"$context.resourcePath", '
                '"status":"$context.status","protocol":"$context.protocol", '
                '"responseLength":"$context.responseLength"}',
            },
            variables={"loggingLevel": "INFO"},
            opts=ResourceOptions(depends_on=[account]),
        )

        if self.domain_name is not None:
            aws_custom_domain_name, base_path_mapping = _create_custom_domain(
                self.name, self.domain_name, rest_api, stage
            )
            # Export custom domain outputs
            pulumi.export(f"api_{self.name}_bpm_domain_name", aws_custom_domain_name.domain_name)
            pulumi.export(f"api_{self.name}_bpm_base_path", base_path_mapping.base_path)
            pulumi.export(
                f"api_{self.name}_bpm__invoke_url",
                pulumi.Output.concat(
                    "https://",
                    aws_custom_domain_name.domain_name,
                    "/",
                    base_path_mapping.base_path,
                ),
            )

        pulumi.export(f"api_{self.name}_arn", rest_api.arn)
        pulumi.export(f"api_{self.name}_invoke_url", stage.invoke_url)
        pulumi.export(f"api_{self.name}_id", rest_api.id)
        pulumi.export(f"api_{self.name}_stage_name", stage.stage_name)

        return ApiResources(rest_api, deployment, stage)

    def _create_method_and_integration(  # noqa: PLR0913
        self,
        route: _ApiRoute,
        http_method: str,
        resource_id: Output[str],
        rest_api: RestApi,
        function: Function,
        authorizer_id_map: dict[str, Output[str]],
    ) -> tuple[Method, Integration]:
        # Determine authorization type and authorizer ID
        auth = route.auth if route.auth is not None else self._default_auth

        if auth is False or auth is None:
            authorization_type = "NONE"
            authorizer_id = None
        elif auth == "IAM":
            authorization_type = "AWS_IAM"
            authorizer_id = None
        elif isinstance(auth, _Authorizer):
            authorization_type = "CUSTOM"
            authorizer_id = authorizer_id_map.get(auth.name)
            if authorizer_id is None:
                raise ValueError(
                    f"Authorizer '{auth.name}' not found in authorizer map. "
                    "This should not happen - please report this as a bug."
                )
        else:
            raise ValueError(
                f"Invalid auth value: {auth!r} (type: {type(auth).__name__}). Expected "
                f"_Authorizer instance (from add_*_authorizer methods), 'IAM', False, or None."
            )

        method = Method(
            context().prefix(
                f"{self.name}-method-{http_method}-{self.path_to_resource_name(route.path_parts)}"
            ),
            rest_api=rest_api.id,
            resource_id=resource_id,
            http_method=http_method,
            authorization=authorization_type,
            authorizer_id=authorizer_id,
        )

        # Integration must wait for Method to be created in AWS.
        # By referencing method.http_method (an Output), we create an implicit dependency.
        # This ensures correct ordering: Resource → Authorizer → Method → Integration
        # Without this, Integration could try to create before Method exists, causing 404.
        integration = Integration(
            context().prefix(
                f"{self.name}-integration-{http_method}-{self.path_to_resource_name(route.path_parts)}"
            ),
            rest_api=rest_api.id,
            resource_id=resource_id,
            http_method=method.http_method,  # Output reference creates dependency
            integration_http_method="POST",
            type="AWS_PROXY",
            uri=function.invoke_arn,
        )

        return method, integration

    def _create_route_resources(
        self,
        routes: list[_ApiRoute],
        rest_api: RestApi,
        function: Function,
        resources: dict[str, Resource],
        authorizer_id_map: dict[str, Output[str]],
    ) -> list[tuple[Method, Integration]]:
        return [
            # Create method and integration for each route and HTTP method
            self._create_method_and_integration(
                route,
                http_method,
                self.get_or_create_resource(route.path_parts, resources, rest_api),
                rest_api,
                function,
                authorizer_id_map,
            )
            # For each route and HTTP method
            for route in routes
            for http_method in route.methods
        ]

    def get_group_function(
        self, key: str, rest_api: RestApi, route_with_config: _ApiRoute, routes: list[_ApiRoute]
    ) -> Function:
        if isinstance(route_with_config.handler, Function):
            function = route_with_config.handler
        else:
            # Handler must be FunctionConfig due to validation
            function_config = route_with_config.handler

            # Generate routing file if needed
            routing_file_content = _create_routing_file(routes, route_with_config)

            extra_assets = {}
            if routing_file_content:
                extra_assets["stlv_routing_handler.py"] = StringAsset(routing_file_content)

            # TODO: find better naming strategy, for now use key which is path to func and
            #  replace / with - this will not work if one function used by multiple APIs?? Check!
            # ok, we prefix function with api name so it will work. And by design you can't create
            # multiple functions from one handler. Although we might allow this later. But that
            # might require changes (way to turn off auto-routing or remove that.
            function = Function(f"{self.name}-{key.replace('/', '-')}", function_config)
            if extra_assets:
                FunctionAssetsRegistry.add(function, extra_assets)
        Permission(
            context().prefix(f"{function.name}-permission"),
            action="lambda:InvokeFunction",
            function=function.function_name,
            principal="apigateway.amazonaws.com",
            source_arn=rest_api.execution_arn.apply(lambda arn: f"{arn}/*/*"),
        )
        return function


def _create_custom_domain(
    api_name: str,
    domain_name: str,
    rest_api: RestApi,
    stage: Stage,
) -> tuple[DomainName, BasePathMapping]:
    """Create custom domain with ACM certificate, DNS records, and base path mapping.

    Returns:
        Tuple of (DomainName, BasePathMapping) resources
    """
    if not isinstance(domain_name, str):
        raise TypeError("Domain name must be a string")
    if not domain_name:
        raise ValueError("Domain name cannot be empty")

    dns = context().dns

    if dns is None:
        raise DnsProviderNotConfiguredError(
            "DNS provider is not configured in the context. "
            "Please set up a DNS provider to use custom domains."
        )

    # 1-3 - Create the ACM certificate and validation record
    custom_domain = acm.AcmValidatedDomain(
        f"{api_name}-acm-custom-domain",
        domain_name=domain_name,
    )

    # 4 - Create the custom domain name in API Gateway
    aws_custom_domain_name = DomainName(
        context().prefix(f"{api_name}-custom-domain"),
        domain_name=domain_name,
        certificate_arn=custom_domain.resources.certificate.arn,
        opts=pulumi.ResourceOptions(depends_on=[custom_domain.resources.cert_validation]),
    )

    # 5 - DNS record creation for the API Gateway custom domain with DNS PROVIDER
    api_record = dns.create_record(
        resource_name=context().prefix(f"{api_name}-custom-domain-record"),
        name=domain_name,
        record_type="CNAME",
        value=aws_custom_domain_name.cloudfront_domain_name,
        ttl=1,
    )

    # 6 - Base Path Mapping
    base_path_mapping = BasePathMapping(
        context().prefix(f"{api_name}-custom-domain-base-path-mapping"),
        rest_api=rest_api.id,
        stage_name=stage.stage_name,
        domain_name=aws_custom_domain_name.domain_name,
        opts=pulumi.ResourceOptions(
            depends_on=[stage, api_record.pulumi_resource, aws_custom_domain_name]
        ),
    )

    return aws_custom_domain_name, base_path_mapping
