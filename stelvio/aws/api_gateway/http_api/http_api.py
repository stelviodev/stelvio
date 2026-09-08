from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, TypedDict, Unpack, final

from pulumi import Output
from pulumi_aws import apigatewayv2, cloudwatch, lambda_

from stelvio import context
from stelvio.aws.api_gateway.domain import ApiDomain, build_url
from stelvio.aws.api_gateway.http_api.authorizers import (
    _CognitoAuthorizer,
    _HttpAuthorizer,
    _JwtAuthorizer,
    _LambdaAuthorizer,
    _parse_user_pool_arn,
)
from stelvio.aws.api_gateway.http_api.routes import _HttpRoute
from stelvio.aws.api_gateway.iam import _create_api_gateway_account_and_role
from stelvio.aws.api_gateway.routing import (
    fn_name_from_key,
    get_group_config_map,
    group_routes_by_handler,
)
from stelvio.aws.api_gateway.validators import (
    DEFAULT_STAGE_NAME,
    PERMISSION_NAME_MAX_LENGTH,
    validate_api_mapping_key,
    validate_domain_name,
    validate_log_retention_days,
    validate_stage_name,
)
from stelvio.aws.cognito import UserPool, UserPoolClient
from stelvio.aws.cors import CorsConfig, CorsConfigDict, normalize_cors_config
from stelvio.aws.function import (
    Function,
    FunctionConfig,
    FunctionConfigDict,
    parse_handler_config,
)
from stelvio.component import Component, link_config_creator, safe_name
from stelvio.link import LinkableMixin, LinkConfig
from stelvio.provider import ProviderStore

if TYPE_CHECKING:
    from stelvio.aws.api_gateway.rest_api.constants import HTTPMethodInput

# Default access-log format for HTTP APIs (v2)
_ACCESS_LOG_FORMAT = (
    '{"requestId":"$context.requestId",'
    '"ip":"$context.identity.sourceIp",'
    '"requestTime":"$context.requestTime",'
    '"httpMethod":"$context.httpMethod",'
    '"routeKey":"$context.routeKey",'
    '"status":"$context.status",'
    '"protocol":"$context.protocol",'
    '"responseLength":"$context.responseLength",'
    '"integrationErrorMessage":"$context.integrationErrorMessage"}'
)


class HttpApiConfigDict(TypedDict, total=False):
    domain_name: str
    domain: ApiDomain
    stage_name: str
    cors: bool | CorsConfig | CorsConfigDict | None
    disable_execute_api_endpoint: bool
    api_mapping_key: str
    access_log_retention_days: int | Literal["forever"]


@dataclass(frozen=True, kw_only=True)
class HttpApiConfig:
    domain_name: str | None = None
    domain: ApiDomain | None = None
    stage_name: str = DEFAULT_STAGE_NAME
    cors: bool | CorsConfig | CorsConfigDict | None = None
    disable_execute_api_endpoint: bool = False
    api_mapping_key: str | None = None
    access_log_retention_days: int | Literal["forever"] = 30

    def __post_init__(self) -> None:
        validate_stage_name(self.stage_name)
        validate_log_retention_days(self.access_log_retention_days)
        if self.domain_name is not None:
            validate_domain_name(self.domain_name)
        if self.domain_name is not None and self.domain is not None:
            raise ValueError(
                "Cannot specify both 'domain_name' and 'domain'. "
                "Use 'domain_name' for a simple custom domain owned by this API, "
                "or 'domain' for a shared ApiDomain component."
            )
        if self.api_mapping_key is not None:
            validate_api_mapping_key(self.api_mapping_key)
        has_domain = self.domain_name is not None or self.domain is not None
        if self.api_mapping_key is not None and not has_domain:
            raise ValueError(
                "api_mapping_key requires either 'domain_name' or 'domain' to be set."
            )
        if self.disable_execute_api_endpoint and not has_domain:
            raise ValueError(
                "disable_execute_api_endpoint=True requires either 'domain_name' or 'domain'."
            )

    @property
    def normalized_cors(self) -> CorsConfig | None:
        return normalize_cors_config(self.cors)


@final
@dataclass(frozen=True)
class HttpApiResources:
    api: apigatewayv2.Api
    stage: apigatewayv2.Stage
    log_group: cloudwatch.LogGroup
    integrations: list[apigatewayv2.Integration]
    routes: list[apigatewayv2.Route]
    permissions: list[lambda_.Permission]
    api_mapping: apigatewayv2.ApiMapping | None = None


class HttpApiCustomizationDict(TypedDict, total=False):
    api: apigatewayv2.ApiArgs | dict[str, Any] | None
    stage: apigatewayv2.StageArgs | dict[str, Any] | None
    log_group: cloudwatch.LogGroupArgs | dict[str, Any] | None
    api_mapping: apigatewayv2.ApiMappingArgs | dict[str, Any] | None


def _reject_jwt_scopes_without_jwt(rk: str, jwt_scopes: list[str] | None) -> None:
    if jwt_scopes is not None:
        raise ValueError(f"jwt_scopes only works with JWT authorizers in route '{rk}'")


def _validate_jwt_scopes(rk: str, jwt_scopes: list[str] | None) -> None:
    if jwt_scopes is None:
        return
    for scope in jwt_scopes:
        if not scope:
            raise ValueError(f"jwt_scopes values must be non-empty strings in route '{rk}'")


@final
class HttpApi(
    Component[HttpApiResources, HttpApiCustomizationDict],
    LinkableMixin,
):
    """AWS API Gateway HTTP API (v2) component.

    Creates an HTTP API with auto-deploy stage, CloudWatch access logs,
    Lambda integrations, authorizers, and optional custom domain.
    """

    _routes: list[_HttpRoute]
    _authorizers: dict[str, _HttpAuthorizer]
    _default_auth: _HttpAuthorizer | Literal["IAM"] | None
    _config: HttpApiConfig

    def __init__(
        self,
        name: str,
        config: HttpApiConfig | HttpApiConfigDict | None = None,
        *,
        tags: dict[str, str] | None = None,
        customize: HttpApiCustomizationDict | None = None,
        **opts: Unpack[HttpApiConfigDict],
    ) -> None:
        super().__init__(
            ProviderStore.aws(), "stelvio:aws:HttpApi", name, tags=tags, customize=customize
        )
        self._routes = []
        self._authorizers = {}
        self._default_auth = None

        if config is not None and opts:
            raise ValueError(
                "Invalid configuration: cannot combine 'config' parameter with additional"
                " options. Provide all settings either in 'config' or as separate options."
            )

        self._config = self._parse_config(config, opts)

    @staticmethod
    def _parse_config(
        config: HttpApiConfig | HttpApiConfigDict | None,
        opts: HttpApiConfigDict,
    ) -> HttpApiConfig:
        if config is None:
            return HttpApiConfig(**opts)
        if isinstance(config, HttpApiConfig):
            return config
        if isinstance(config, dict):
            return HttpApiConfig(**config)
        raise TypeError(
            f"Invalid config type: expected HttpApiConfig or dict, got {type(config).__name__}"
        )

    def _check_not_created(self) -> None:
        if self._resources is not None:
            raise RuntimeError(
                f"Cannot modify HttpApi '{self.name}' after resources have been created. "
                "Add all routes and authorizers before accessing the .resources property."
            )

    @property
    def domain_name(self) -> str | None:
        if self._config.domain:
            return self._config.domain.domain_name
        return self._config.domain_name

    @property
    def config(self) -> HttpApiConfig:
        return self._config

    @property
    def url(self) -> Output[str]:
        """Base URL for this API."""
        domain = self.domain_name
        if domain is not None:
            return build_url("https", domain, self._config.api_mapping_key)
        return self.resources.stage.invoke_url

    @property
    def api_id(self) -> Output[str]:
        return self.resources.api.id

    @property
    def arn(self) -> Output[str]:
        return self.resources.api.arn

    @property
    def execution_arn(self) -> Output[str]:
        return self.resources.api.execution_arn

    # --- Authorizer registration ---

    def add_lambda_authorizer(
        self,
        name: str,
        handler: str | FunctionConfig | FunctionConfigDict | Function | None = None,
        /,
        *,
        identity_sources: list[str],
        ttl: int = 300,
        simple_response: bool = True,
        **fn_opts: Unpack[FunctionConfigDict],
    ) -> _LambdaAuthorizer:
        """Add a Lambda (REQUEST) authorizer."""
        self._check_not_created()
        self._validate_authorizer_name(name)

        if isinstance(handler, str):
            function_config = parse_handler_config(handler, fn_opts)
            function = Function(
                f"{self.name}-auth-{name}",
                config=function_config,
                tags=self._tags,
                parent=self,
            )
        elif isinstance(handler, Function):
            if fn_opts:
                raise ValueError("Cannot combine a Function handler with function options.")
            function = handler
        else:
            function_config = parse_handler_config(handler, fn_opts)
            function = Function(
                f"{self.name}-auth-{name}",
                config=function_config,
                tags=self._tags,
                parent=self,
            )

        auth = _LambdaAuthorizer(
            name=name,
            function=function,
            identity_sources=identity_sources,
            ttl=ttl,
            simple_response=simple_response,
        )
        self._authorizers[name] = auth
        return auth

    def add_jwt_authorizer(
        self,
        name: str,
        *,
        issuer: str,
        audiences: list[str],
        identity_source: str = "$request.header.Authorization",
    ) -> _JwtAuthorizer:
        """Add a generic JWT/OIDC authorizer."""
        self._check_not_created()
        self._validate_authorizer_name(name)
        auth = _JwtAuthorizer(
            name=name,
            issuer=issuer,
            audiences=audiences,
            identity_source=identity_source,
        )
        self._authorizers[name] = auth
        return auth

    def add_cognito_authorizer(
        self,
        name: str,
        *,
        user_pool: UserPool | str,
        audiences: list[UserPoolClient | str],
        identity_source: str = "$request.header.Authorization",
    ) -> _CognitoAuthorizer:
        """Add a Cognito JWT authorizer."""
        self._check_not_created()
        self._validate_authorizer_name(name)

        if not audiences:
            raise ValueError(f"Cognito authorizer '{name}' audiences cannot be empty")

        if isinstance(user_pool, str):
            region, pool_id = _parse_user_pool_arn(name, user_pool)
            issuer = Output.from_input(f"https://cognito-idp.{region}.amazonaws.com/{pool_id}")
        else:
            region = context().aws.region
            issuer = Output.concat(
                f"https://cognito-idp.{region}.amazonaws.com/",
                user_pool.resources.user_pool.id,
            )

        # Resolve audiences (UserPoolClient → client id, raw string stays)
        resolved_audiences = []
        for aud in audiences:
            if isinstance(aud, UserPoolClient):
                if isinstance(user_pool, str):
                    raise TypeError(
                        f"Cognito authorizer '{name}': UserPoolClient audiences require "
                        "user_pool to be a UserPool component, not an ARN string."
                    )
                if aud.pool is not user_pool:
                    raise ValueError(
                        f"Cognito authorizer '{name}': UserPoolClient '{aud.name}' "
                        f"belongs to a different UserPool."
                    )
                resolved_audiences.append(aud.resources.client.id)
            else:
                resolved_audiences.append(aud)

        auth = _CognitoAuthorizer(
            name=name,
            user_pool_issuer=issuer,
            audiences=resolved_audiences,
            identity_source=identity_source,
        )
        self._authorizers[name] = auth
        return auth

    def _validate_authorizer_name(self, name: str) -> None:
        if name in self._authorizers:
            raise ValueError(
                f"Duplicate authorizer name: '{name}'. Authorizer names must be unique."
            )

    # --- Default auth ---

    @property
    def default_auth(self) -> _HttpAuthorizer | Literal["IAM"] | None:
        return self._default_auth

    @default_auth.setter
    def default_auth(self, value: _HttpAuthorizer | Literal["IAM"] | None) -> None:
        self._check_not_created()
        if value is False:
            raise ValueError(
                "default_auth cannot be False. "
                "Use None to disable auth, or False only on individual routes."
            )
        self._default_auth = value

    # --- Route registration ---

    def route(
        self,
        http_method: HTTPMethodInput,
        path: str,
        handler: str | FunctionConfig | FunctionConfigDict | Function | None = None,
        /,
        *,
        auth: _HttpAuthorizer | Literal["IAM", False] | None = None,
        jwt_scopes: list[str] | None = None,
        **opts: Unpack[FunctionConfigDict],
    ) -> None:
        """Add a route to the HTTP API."""
        self._check_not_created()

        resolved_handler = self._resolve_handler(handler, opts)
        route = _HttpRoute(
            method=http_method,
            path=path,
            handler=resolved_handler,
            auth=auth,
            jwt_scopes=jwt_scopes,
        )

        # Check for duplicate route keys
        new_keys = set(route.route_keys)
        for existing in self._routes:
            existing_keys = set(existing.route_keys)
            conflicts = new_keys & existing_keys
            if conflicts:
                raise ValueError(
                    f"Duplicate route key(s): {sorted(conflicts)}. Each route key must be unique."
                )

        self._routes.append(route)

    @staticmethod
    def _resolve_handler(
        handler: str | FunctionConfig | FunctionConfigDict | Function | None,
        opts: FunctionConfigDict,
    ) -> FunctionConfig | Function:
        if isinstance(handler, Function):
            if opts:
                raise ValueError("Cannot combine a Function handler with function options.")
            return handler
        return parse_handler_config(handler, opts)

    # --- Resource creation ---

    def _create_resources(self) -> HttpApiResources:
        # 1. Resolve domain
        domain = self._resolve_domain()

        # 2. Build CORS args
        cors_args = self._build_cors_args()

        # 3. Create apigatewayv2.Api
        api_args = {
            "protocol_type": "HTTP",
            "disable_execute_api_endpoint": self._config.disable_execute_api_endpoint,
        }
        if cors_args:
            api_args["cors_configuration"] = cors_args

        api = apigatewayv2.Api(
            safe_name(context().prefix(), self.name, 128),
            **self._customizer("api", api_args, inject_tags=True),
            opts=self._resource_opts(),
        )

        # 4. Create CloudWatch log group
        log_group_args: dict[str, Any] = {
            "name": Output.concat("/aws/apigateway/", api.id),
        }
        if self._config.access_log_retention_days != "forever":
            log_group_args["retention_in_days"] = self._config.access_log_retention_days

        log_group = cloudwatch.LogGroup(
            context().prefix(f"{self.name}-logs"),
            **self._customizer("log_group", log_group_args, inject_tags=True),
            opts=self._resource_opts(),
        )

        # 5. Ensure API Gateway account has CloudWatch logging role
        account = _create_api_gateway_account_and_role()

        # 6. Create authorizers
        authorizer_resources = self._materialize_authorizers(api)

        # 7. Group routes by Lambda, create Functions + Integrations + Routes
        grouped = group_routes_by_handler(self._routes)
        lambdas = self._resolve_lambdas(grouped)

        integrations = self._create_integrations(api, lambdas)
        routes = self._create_routes(api, integrations, authorizer_resources)

        # 8. Create Lambda permissions for route Lambdas
        permissions = self._create_route_permissions(api, lambdas)

        # 9. Create auto-deploy Stage
        stage = apigatewayv2.Stage(
            context().prefix(f"{self.name}-stage"),
            **self._customizer(
                "stage",
                {
                    "api_id": api.id,
                    "name": self._config.stage_name,
                    "auto_deploy": True,
                    "access_log_settings": {
                        "destination_arn": log_group.arn,
                        "format": _ACCESS_LOG_FORMAT,
                    },
                },
                inject_tags=True,
            ),
            opts=self._resource_opts(depends_on=[account, log_group]),
        )

        # 10. Create ApiMapping if domain is configured
        api_mapping = None
        if domain is not None:
            api_mapping = self._create_api_mapping(api, stage, domain)

        output_url = (
            build_url("https", domain.domain_name, self._config.api_mapping_key)
            if domain is not None
            else stage.invoke_url
        )

        self.register_outputs(
            {
                "url": output_url,
                "_arn": api.arn,
            }
        )

        return HttpApiResources(
            api=api,
            stage=stage,
            log_group=log_group,
            integrations=list(integrations.values()),
            routes=routes,
            permissions=permissions,
            api_mapping=api_mapping,
        )

    def _resolve_domain(self) -> ApiDomain | None:
        if self._config.domain is not None:
            return self._config.domain
        if self._config.domain_name is not None:
            return ApiDomain(
                f"{self.name}-domain",
                domain_name=self._config.domain_name,
                tags=self._tags,
                parent=self,
            )
        return None

    def _build_cors_args(self) -> dict[str, Any] | None:
        cors = self._config.normalized_cors
        if cors is None:
            return None

        def _to_list(v: str | list[str]) -> list[str]:
            return [v] if isinstance(v, str) else v

        args = {
            "allow_origins": _to_list(cors.allow_origins),
            "allow_methods": _to_list(cors.allow_methods),
            "allow_headers": _to_list(cors.allow_headers),
        }
        if cors.allow_credentials:
            args["allow_credentials"] = cors.allow_credentials
        if cors.max_age is not None:
            args["max_age"] = cors.max_age
        if cors.expose_headers:
            args["expose_headers"] = cors.expose_headers
        return args

    def _resolve_lambdas(self, grouped: dict[str, list[_HttpRoute]]) -> dict[str, Function]:
        """For each logical Lambda group, resolve (or create) the Function."""
        group_config_map = get_group_config_map(grouped)
        return {key: self._resolve_group_function(key, group_config_map[key]) for key in grouped}

    def _resolve_group_function(self, key: str, route_with_config: _HttpRoute) -> Function:
        if isinstance(route_with_config.handler, Function):
            return route_with_config.handler
        return Function(
            fn_name_from_key(self.name, key),
            config=route_with_config.handler,
            tags=self._tags,
            parent=self,
        )

    def _create_integrations(
        self,
        api: apigatewayv2.Api,
        functions: dict[str, Function],
    ) -> dict[str, apigatewayv2.Integration]:
        integrations = {}
        for key, fn in functions.items():
            integration = apigatewayv2.Integration(
                context().prefix(f"{self.name}-integration-{fn_name_from_key(self.name, key)}"),
                api_id=api.id,
                integration_type="AWS_PROXY",
                integration_method="POST",
                integration_uri=fn.invoke_arn,
                payload_format_version="2.0",
                timeout_milliseconds=30000,
                opts=self._resource_opts(),
            )
            integrations[key] = integration
        return integrations

    def _create_routes(
        self,
        api: apigatewayv2.Api,
        integrations: dict[str, apigatewayv2.Integration],
        authorizer_resources: dict[str, apigatewayv2.Authorizer],
    ) -> list[apigatewayv2.Route]:
        routes_created = []
        for http_route in self._routes:
            # Resolve integration key
            if isinstance(http_route.handler, Function):
                key = http_route.handler.name
            else:
                key = http_route.handler.full_handler_path
            integration = integrations[key]

            # Resolve effective auth
            effective_auth = http_route.auth if http_route.auth is not None else self._default_auth
            if http_route.auth is False:
                effective_auth = None

            for rk in http_route.route_keys:
                auth_type, authorizer_id, scopes = self._resolve_auth_for_route(
                    rk,
                    effective_auth,
                    http_route.jwt_scopes,
                    authorizer_resources,
                )
                route_args = {
                    "api_id": api.id,
                    "route_key": rk,
                    "target": Output.concat("integrations/", integration.id),
                    "authorization_type": auth_type,
                }
                if authorizer_id is not None:
                    route_args["authorizer_id"] = authorizer_id
                if scopes:
                    route_args["authorization_scopes"] = scopes

                route_name_part = (
                    "default" if rk == "$default" else rk.replace(" ", "-").replace("/", "-")
                ).strip("-")
                r = apigatewayv2.Route(
                    context().prefix(f"{self.name}-route-{route_name_part}"),
                    **route_args,
                    opts=self._resource_opts(),
                )
                routes_created.append(r)

        return routes_created

    def _resolve_auth_for_route(
        self,
        rk: str,
        effective_auth: _HttpAuthorizer | Literal["IAM"] | None,
        jwt_scopes: list[str] | None,
        authorizer_resources: dict[str, apigatewayv2.Authorizer],
    ) -> tuple[str, Output[str] | None, list[str] | None]:
        if effective_auth is None:
            _reject_jwt_scopes_without_jwt(rk, jwt_scopes)
            return "NONE", None, None
        if effective_auth == "IAM":
            _reject_jwt_scopes_without_jwt(rk, jwt_scopes)
            return "AWS_IAM", None, None
        if isinstance(effective_auth, _LambdaAuthorizer):
            _reject_jwt_scopes_without_jwt(rk, jwt_scopes)
            auth_res = authorizer_resources[effective_auth.name]
            return "CUSTOM", auth_res.id, None
        if isinstance(effective_auth, _JwtAuthorizer | _CognitoAuthorizer):
            _validate_jwt_scopes(rk, jwt_scopes)
            auth_res = authorizer_resources[effective_auth.name]
            return "JWT", auth_res.id, jwt_scopes or None

        raise TypeError(f"Unsupported auth type for route '{rk}': {type(effective_auth).__name__}")

    def _create_route_permissions(
        self, api: apigatewayv2.Api, lambdas: dict[str, Function]
    ) -> list[lambda_.Permission]:
        permissions = []
        for key, fn in lambdas.items():
            permission = lambda_.Permission(
                safe_name(
                    context().prefix(),
                    f"{self.name}-permission-{fn_name_from_key(self.name, key)}",
                    PERMISSION_NAME_MAX_LENGTH,
                ),
                action="lambda:InvokeFunction",
                function=fn.function_name,
                principal="apigateway.amazonaws.com",
                source_arn=Output.concat(api.execution_arn, "/*/*"),
                opts=self._resource_opts(),
            )
            permissions.append(permission)
        return permissions

    def _materialize_authorizers(
        self, api: apigatewayv2.Api
    ) -> dict[str, apigatewayv2.Authorizer]:
        result = {}
        for name, auth in self._authorizers.items():
            if isinstance(auth, _LambdaAuthorizer):
                authorizer_type = "REQUEST"
                payload_version = "2.0"
                auth_resource = apigatewayv2.Authorizer(
                    context().prefix(f"{self.name}-authorizer-{name}"),
                    api_id=api.id,
                    authorizer_type=authorizer_type,
                    authorizer_uri=auth.function.invoke_arn,
                    identity_sources=auth.identity_sources,
                    authorizer_result_ttl_in_seconds=auth.ttl,
                    authorizer_payload_format_version=payload_version,
                    enable_simple_responses=auth.simple_response,
                    name=name,
                    opts=self._resource_opts(),
                )
                # Lambda permission for authorizer
                lambda_.Permission(
                    safe_name(
                        context().prefix(),
                        f"{self.name}-auth-permission-{name}",
                        PERMISSION_NAME_MAX_LENGTH,
                    ),
                    action="lambda:InvokeFunction",
                    function=auth.function.function_name,
                    principal="apigateway.amazonaws.com",
                    source_arn=Output.concat(api.execution_arn, "/authorizers/*"),
                    opts=self._resource_opts(),
                )
                result[name] = auth_resource

            elif isinstance(auth, _JwtAuthorizer):
                auth_resource = apigatewayv2.Authorizer(
                    context().prefix(f"{self.name}-authorizer-{name}"),
                    api_id=api.id,
                    authorizer_type="JWT",
                    identity_sources=[auth.identity_source],
                    jwt_configuration={
                        "audiences": auth.audiences,
                        "issuer": auth.issuer,
                    },
                    name=name,
                    opts=self._resource_opts(),
                )
                result[name] = auth_resource

            elif isinstance(auth, _CognitoAuthorizer):
                auth_resource = apigatewayv2.Authorizer(
                    context().prefix(f"{self.name}-authorizer-{name}"),
                    api_id=api.id,
                    authorizer_type="JWT",
                    identity_sources=[auth.identity_source],
                    jwt_configuration={
                        "audiences": auth.audiences,
                        "issuer": auth.user_pool_issuer,
                    },
                    name=name,
                    opts=self._resource_opts(),
                )
                result[name] = auth_resource

        return result

    def _create_api_mapping(
        self,
        api: apigatewayv2.Api,
        stage: apigatewayv2.Stage,
        domain: ApiDomain,
    ) -> apigatewayv2.ApiMapping:
        domain.register_mapping(self.name, self._config.api_mapping_key)
        mapping_args: dict[str, Any] = {
            "api_id": api.id,
            "domain_name": domain.resources.custom_domain.domain_name,
            "stage": stage.id,
        }
        if self._config.api_mapping_key is not None:
            mapping_args["api_mapping_key"] = self._config.api_mapping_key
        return apigatewayv2.ApiMapping(
            context().prefix(f"{self.name}-api-mapping"),
            **self._customizer("api_mapping", mapping_args),
            opts=self._resource_opts(),
        )


@link_config_creator(HttpApi)
def _http_api_link_creator(api: HttpApi) -> LinkConfig:
    return LinkConfig(
        properties={
            "api_url": api.url,
            "api_execution_arn": api.execution_arn,
        },
        permissions=[],
    )
