from __future__ import annotations

import inspect
import warnings
from dataclasses import dataclass
from typing import Any, Literal, TypedDict, Unpack, final

import pulumi_aws
from pulumi import Output
from pulumi_aws import cloudwatch

from stelvio import context
from stelvio.aws.api_gateway.iam import _create_api_gateway_account_and_role
from stelvio.aws.cors import CorsConfig, CorsConfigDict
from stelvio.aws.function import Function, FunctionConfig, FunctionConfigDict
from stelvio.aws.http_api._authorizers import (
    _CognitoAuthorizer,
    _HttpAuthorizer,
    _JwtAuthorizer,
    _LambdaAuthorizer,
)
from stelvio.aws.http_api._domain import HttpApiDomain
from stelvio.aws.http_api._routes import (
    _HttpRoute,
    rewrite_v1_identity_source,
    validate_api_mapping_key,
    validate_log_retention_days,
    validate_stage_name,
)
from stelvio.component import Component, link_config_creator, safe_name
from stelvio.link import LinkableMixin, LinkConfig

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

DEFAULT_STAGE_NAME = "$default"
_warned_cognito_scope_call_sites: set[tuple[str, int]] = set()


@final
@dataclass(frozen=True)
class HttpApiResources:
    api: pulumi_aws.apigatewayv2.Api
    stage: pulumi_aws.apigatewayv2.Stage
    log_group: cloudwatch.LogGroup
    api_mapping: pulumi_aws.apigatewayv2.ApiMapping | None = None


class HttpApiConfigDict(TypedDict, total=False):
    domain_name: str
    domain: HttpApiDomain
    stage_name: str
    cors: bool | CorsConfig | CorsConfigDict | None
    disable_execute_api_endpoint: bool
    api_mapping_key: str
    access_log_retention_days: int | None


@dataclass(frozen=True, kw_only=True)
class HttpApiConfig:
    domain_name: str | None = None
    domain: HttpApiDomain | None = None
    stage_name: str = DEFAULT_STAGE_NAME
    cors: bool | CorsConfig | CorsConfigDict | None = None
    disable_execute_api_endpoint: bool = False
    api_mapping_key: str | None = None
    access_log_retention_days: int | None = 30

    def __post_init__(self) -> None:
        validate_stage_name(self.stage_name)
        validate_log_retention_days(self.access_log_retention_days)
        if self.domain_name is not None and self.domain is not None:
            raise ValueError(
                "Cannot specify both 'domain_name' and 'domain'. "
                "Use 'domain_name' for a simple custom domain owned by this API, "
                "or 'domain' for a shared HttpApiDomain component."
            )
        if self.api_mapping_key is not None:
            validate_api_mapping_key(self.api_mapping_key)

    @property
    def normalized_cors(self) -> CorsConfig | None:
        if self.cors is True:
            return CorsConfig(
                allow_origins="*",
                allow_methods="*",
                allow_headers="*",
            )
        if isinstance(self.cors, CorsConfig):
            return self.cors
        if isinstance(self.cors, dict):
            return CorsConfig(**self.cors)
        return None


class HttpApiCustomizationDict(TypedDict, total=False):
    api: pulumi_aws.apigatewayv2.ApiArgs | dict[str, Any] | None
    stage: pulumi_aws.apigatewayv2.StageArgs | dict[str, Any] | None
    log_group: cloudwatch.LogGroupArgs | dict[str, Any] | None
    api_mapping: pulumi_aws.apigatewayv2.ApiMappingArgs | dict[str, Any] | None


def _resolve_route_scopes(
    jwt_scopes: list[str] | None,
    cognito_scopes: list[str] | None,
) -> list[str] | None:
    if jwt_scopes is not None and cognito_scopes is not None:
        raise ValueError("Specify either 'jwt_scopes' or 'cognito_scopes', not both.")
    if cognito_scopes is None:
        return jwt_scopes

    caller_frame = inspect.currentframe()
    route_frame = caller_frame.f_back if caller_frame is not None else None
    user_frame = route_frame.f_back if route_frame is not None else None
    if user_frame is not None:
        call_site = (user_frame.f_code.co_filename, user_frame.f_lineno)
    else:
        call_site = ("<unknown>", 0)

    if call_site not in _warned_cognito_scope_call_sites:
        _warned_cognito_scope_call_sites.add(call_site)
        warnings.warn(
            "cognito_scopes is deprecated for HttpApi routes; use jwt_scopes instead.",
            DeprecationWarning,
            stacklevel=3,
        )
    return cognito_scopes


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
    _domain_component: HttpApiDomain | None
    _domain_ref: HttpApiDomain | None  # resolved during _create_resources

    def __init__(
        self,
        name: str,
        config: HttpApiConfig | HttpApiConfigDict | None = None,
        *,
        domain: HttpApiDomain | None = None,
        tags: dict[str, str] | None = None,
        customize: HttpApiCustomizationDict | None = None,
        **opts: Unpack[HttpApiConfigDict],
    ) -> None:
        super().__init__("stelvio:aws:HttpApi", name, tags=tags, customize=customize)
        self._routes = []
        self._authorizers = {}
        self._default_auth = None
        self._domain_ref = None
        self._permissions: list[pulumi_aws.lambda_.Permission] = []
        self._route_resources: list[pulumi_aws.apigatewayv2.Route] = []
        self._authorizer_resources: list[pulumi_aws.apigatewayv2.Authorizer] = []

        if config is not None and opts:
            raise ValueError(
                "Invalid configuration: cannot combine 'config' parameter with additional"
                " options. Provide all settings either in 'config' or as separate options."
            )

        self._config = self._parse_config(config, opts)
        if self._config.domain is not None and domain is not None:
            raise ValueError("Cannot specify 'domain' both in config and as a keyword argument.")
        self._domain_component = domain or self._config.domain

        # Validate domain_name + domain exclusivity
        if self._config.domain_name is not None and self._domain_component is not None:
            raise ValueError(
                "Cannot specify both 'domain_name' and 'domain'. "
                "Use 'domain_name' for a simple custom domain owned by this API, "
                "or 'domain' for a shared HttpApiDomain component."
            )

        # api_mapping_key requires a domain
        if self._config.api_mapping_key is not None and (
            self._config.domain_name is None and self._domain_component is None
        ):
            raise ValueError(
                "api_mapping_key requires either 'domain_name' or 'domain' to be set."
            )

        # disable_execute_api_endpoint requires a domain
        if self._config.disable_execute_api_endpoint and (
            self._config.domain_name is None and self._domain_component is None
        ):
            raise ValueError(
                "disable_execute_api_endpoint=True requires either 'domain_name' or 'domain'."
            )

        # CORS validation: allow_credentials=True with wildcard origin
        cors = self._config.normalized_cors
        if (
            cors is not None
            and cors.allow_credentials
            and (
                cors.allow_origins == "*"
                or (isinstance(cors.allow_origins, list) and "*" in cors.allow_origins)
            )
        ):
            raise ValueError(
                "cors.allow_credentials=True is incompatible with allow_origins='*'. "
                "List explicit origins to use credentials."
            )

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
        return self._config.domain_name

    @property
    def stage_name(self) -> str:
        return self._config.stage_name

    @property
    def url(self) -> Output[str]:
        """Base URL for this API."""
        domain = self._config.domain_name or (
            self._domain_component.domain_name if self._domain_component else None
        )
        if domain is not None:
            mapping_key = self._config.api_mapping_key
            if mapping_key:
                return Output.from_input(f"https://{domain}/{mapping_key}")
            return Output.from_input(f"https://{domain}")

        # execute-api URL
        api_id = self.resources.api.id
        region = context().aws.region
        stage = self._config.stage_name
        if stage == "$default":
            return Output.concat("https://", api_id, f".execute-api.{region}.amazonaws.com")
        return Output.concat("https://", api_id, f".execute-api.{region}.amazonaws.com/{stage}")

    @property
    def api_id(self) -> Output[str]:
        return self.resources.api.id

    @property
    def api_arn(self) -> Output[str]:
        return self.resources.api.arn

    @property
    def execution_arn(self) -> Output[str]:
        return self.resources.api.execution_arn

    # --- Authorizer registration ---

    def add_lambda_authorizer(
        self,
        name: str,
        handler: str | Function,
        /,
        *,
        identity_sources: str | list[str],
        ttl: int = 300,
        simple_response: bool = True,
        **fn_opts: Unpack[FunctionConfigDict],
    ) -> _LambdaAuthorizer:
        """Add a Lambda (REQUEST) authorizer."""
        self._check_not_created()
        self._validate_authorizer_name(name)

        # Normalize identity_sources
        sources_list = (
            [identity_sources] if isinstance(identity_sources, str) else list(identity_sources)
        )
        # Rewrite v1-style sources
        sources_list = [rewrite_v1_identity_source(name, s) for s in sources_list]

        if isinstance(handler, str):
            function = Function(
                f"{self.name}-auth-{name}",
                handler=handler,
                tags=self._tags or None,
                parent=self,
                **fn_opts,
            )
        else:
            function = handler

        auth = _LambdaAuthorizer(
            name=name,
            function=function,
            identity_sources=sources_list,
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
            identity_source=rewrite_v1_identity_source(name, identity_source),
        )
        self._authorizers[name] = auth
        return auth

    def add_cognito_authorizer(
        self,
        name: str,
        *,
        user_pool: object,
        audiences: list[object],
        identity_source: str = "$request.header.Authorization",
    ) -> _CognitoAuthorizer:
        """Add a Cognito JWT authorizer."""
        self._check_not_created()
        self._validate_authorizer_name(name)

        # Resolve issuer from UserPool
        region = context().aws.region
        pool_id = user_pool.resources.user_pool.id
        issuer = Output.concat(f"https://cognito-idp.{region}.amazonaws.com/", pool_id)

        # Resolve audiences (UserPoolClient → client id, raw string stays)
        resolved_audiences = []
        for aud in audiences:
            if hasattr(aud, "resources"):
                # It's a UserPoolClient — validate same pool
                if getattr(aud, "_pool", None) is not user_pool:
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
            identity_source=rewrite_v1_identity_source(name, identity_source),
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

    def route(  # noqa: PLR0913
        self,
        http_method: str | list[str],
        path: str,
        handler: str | FunctionConfig | dict | Function,
        /,
        *,
        auth: _HttpAuthorizer | Literal["IAM", False] | None = None,
        jwt_scopes: list[str] | None = None,
        cognito_scopes: list[str] | None = None,
        **opts: Unpack[FunctionConfigDict],
    ) -> None:
        """Add a route to the HTTP API."""
        self._check_not_created()
        scopes = _resolve_route_scopes(jwt_scopes, cognito_scopes)

        resolved_handler = self._resolve_handler(handler, opts)
        route = _HttpRoute(
            method=http_method,
            path=path,
            handler=resolved_handler,
            auth=auth,
            jwt_scopes=scopes,
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
        handler: str | FunctionConfig | dict | Function,
        opts: dict,
    ) -> FunctionConfig | Function:
        if isinstance(handler, Function | FunctionConfig):
            return handler
        if isinstance(handler, dict):
            if opts:
                raise ValueError("Cannot combine a dict handler with additional keyword options.")
            return FunctionConfig(**handler)
        if isinstance(handler, str):
            if "handler" in opts:
                raise ValueError(
                    "Ambiguous handler: specified both as positional argument and in opts."
                )
            return FunctionConfig(handler=handler, **opts)
        raise TypeError(
            f"Handler must be str, FunctionConfig, dict, or Function, got {type(handler).__name__}"
        )

    # --- Resource creation ---

    def _create_resources(self) -> HttpApiResources:
        # 1. Resolve domain
        domain = self._resolve_domain()

        # 2. Build CORS args
        cors_args = self._build_cors_args()

        # 3. Create apigatewayv2.Api
        api_args: dict[str, Any] = {
            "protocol_type": "HTTP",
            "disable_execute_api_endpoint": self._config.disable_execute_api_endpoint,
        }
        if cors_args:
            api_args["cors_configuration"] = cors_args

        api = pulumi_aws.apigatewayv2.Api(
            context().prefix(self.name),
            **self._customizer("api", api_args, inject_tags=True),
            opts=self._resource_opts(),
        )

        # 4. Create CloudWatch log group
        log_group_args: dict[str, Any] = {
            "name": Output.concat("/aws/apigateway/", api.id),
        }
        if self._config.access_log_retention_days is not None:
            log_group_args["retention_in_days"] = self._config.access_log_retention_days

        log_group = cloudwatch.LogGroup(
            context().prefix(f"{self.name}-logs"),
            **self._customizer("log_group", log_group_args, inject_tags=True),
            opts=self._resource_opts(),
        )

        # 5. Ensure API Gateway account has CloudWatch logging role
        _create_api_gateway_account_and_role()

        # 6. Create authorizers
        authorizer_resources = self._materialize_authorizers(api)
        self._authorizer_resources = list(authorizer_resources.values())

        # 7. Group routes by Lambda, create Functions + Integrations + Routes
        grouped = self._group_routes_by_handler()
        lambdas = self._resolve_lambdas(grouped)
        self._validate_lambda_timeouts(lambdas)

        integrations = self._create_integrations(api, lambdas)
        self._route_resources = self._create_routes(api, integrations, authorizer_resources)

        # 8. Create Lambda permissions for route Lambdas
        self._create_route_permissions(api, lambdas)

        # 9. Create auto-deploy Stage
        stage = pulumi_aws.apigatewayv2.Stage(
            safe_name(context().prefix(), f"{self.name}-stage", 128),
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
            opts=self._resource_opts(),
        )

        # 10. Create ApiMapping if domain is configured
        api_mapping: pulumi_aws.apigatewayv2.ApiMapping | None = None
        if domain is not None:
            api_mapping = self._create_api_mapping(api, stage, domain)

        return HttpApiResources(
            api=api,
            stage=stage,
            log_group=log_group,
            api_mapping=api_mapping,
        )

    def _resolve_domain(self) -> HttpApiDomain | None:
        """Resolve the domain component to use (create inline or use shared)."""
        if self._domain_component is not None:
            self._domain_ref = self._domain_component
            return self._domain_component

        if self._config.domain_name is not None:
            domain = HttpApiDomain(
                f"{self.name}-domain",
                domain_name=self._config.domain_name,
                tags=self._tags or None,
                parent=self,
            )
            self._domain_ref = domain
            return domain

        return None

    def _build_cors_args(self) -> dict[str, Any] | None:
        cors = self._config.normalized_cors
        if cors is None:
            return None

        def _to_list(v: str | list[str]) -> list[str]:
            return [v] if isinstance(v, str) else v

        args: dict[str, Any] = {
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

    def _group_routes_by_handler(self) -> dict[str, list[_HttpRoute]]:
        groups: dict[str, list[_HttpRoute]] = {}
        for route in self._routes:
            if isinstance(route.handler, Function):
                key = route.handler.name
            else:
                key = route.handler.full_handler_path
            groups.setdefault(key, []).append(route)
        return groups

    def _resolve_lambdas(self, grouped: dict[str, list[_HttpRoute]]) -> dict[str, Function]:
        """For each logical Lambda group, resolve (or create) the Function."""
        result: dict[str, Function] = {}
        for key, routes in grouped.items():
            # Find the route with non-default FunctionConfig (if any)
            config_routes = [
                r
                for r in routes
                if isinstance(r.handler, FunctionConfig) and not r.handler.has_only_defaults
            ]
            if len(config_routes) > 1:
                paths = [r.path for r in config_routes]
                raise ValueError(
                    f"Multiple routes try to configure the same Lambda function: "
                    f"{', '.join(paths)}"
                )
            representative = config_routes[0] if config_routes else routes[0]

            if isinstance(representative.handler, Function):
                result[key] = representative.handler
            else:
                fn_config: FunctionConfig = representative.handler
                # Derive function name from the handler path
                fn_name = self._fn_name_from_key(key)
                result[key] = Function(
                    fn_name,
                    config=fn_config,
                    tags=self._tags or None,
                    parent=self,
                )
        return result

    def _fn_name_from_key(self, key: str) -> str:
        """Derive a unique component name for a Lambda from its handler key."""
        # key is e.g. "functions/users.handler" → "my-api-functions-users_handler"
        safe = key.replace("/", "-").replace(".", "_").replace("::", "-")
        return f"{self.name}-{safe}"

    def _validate_lambda_timeouts(self, lambdas: dict[str, Function]) -> None:
        for key, routes in self._group_routes_by_handler().items():
            fn = lambdas[key]
            # Resolve timeout from either a pre-built Function or a FunctionConfig route.
            timeout: int | None = None
            if isinstance(fn, Function) and fn.config is not None:
                timeout = fn.config.timeout
            if timeout is None:
                for r in routes:
                    if isinstance(r.handler, FunctionConfig) and r.handler.timeout is not None:
                        timeout = r.handler.timeout
                        break
            if timeout is not None and timeout > 30:  # noqa: PLR2004
                route_key_str = routes[0].route_keys[0] if routes else key
                raise ValueError(
                    f"HttpApi route '{route_key_str}' uses a Lambda with timeout={timeout}s, "
                    f"but HTTP APIs cap integration response time at 30s. "
                    f"Reduce the Lambda timeout or move this route to the v1 Api component."
                )

    def _create_integrations(
        self,
        api: pulumi_aws.apigatewayv2.Api,
        lambdas: dict[str, Function],
    ) -> dict[str, pulumi_aws.apigatewayv2.Integration]:
        integrations: dict[str, pulumi_aws.apigatewayv2.Integration] = {}
        for key, fn in lambdas.items():
            integration = pulumi_aws.apigatewayv2.Integration(
                safe_name(
                    context().prefix(),
                    f"{self.name}-integration-{self._fn_name_from_key(key)}",
                    128,
                ),
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
        api: pulumi_aws.apigatewayv2.Api,
        integrations: dict[str, pulumi_aws.apigatewayv2.Integration],
        authorizer_resources: dict[str, pulumi_aws.apigatewayv2.Authorizer],
    ) -> list[pulumi_aws.apigatewayv2.Route]:
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
                route_args: dict[str, Any] = {
                    "api_id": api.id,
                    "route_key": rk,
                    "target": Output.concat("integrations/", integration.id),
                    "authorization_type": auth_type,
                }
                if authorizer_id is not None:
                    route_args["authorizer_id"] = authorizer_id
                if scopes:
                    route_args["authorization_scopes"] = scopes

                route_name_part = rk.replace(" ", "-").replace("/", "-").replace("$", "default")
                r = pulumi_aws.apigatewayv2.Route(
                    safe_name(
                        context().prefix(),
                        f"{self.name}-route-{route_name_part}",
                        128,
                    ),
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
        authorizer_resources: dict[str, pulumi_aws.apigatewayv2.Authorizer],
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
        self,
        api: pulumi_aws.apigatewayv2.Api,
        lambdas: dict[str, Function],
    ) -> None:
        for key, fn in lambdas.items():
            permission = pulumi_aws.lambda_.Permission(
                safe_name(
                    context().prefix(),
                    f"{self.name}-permission-{self._fn_name_from_key(key)}",
                    128,
                ),
                action="lambda:InvokeFunction",
                function=fn.function_name,
                principal="apigateway.amazonaws.com",
                source_arn=Output.concat(api.execution_arn, "/*/*"),
                opts=self._resource_opts(),
            )
            self._permissions.append(permission)

    def _materialize_authorizers(
        self, api: pulumi_aws.apigatewayv2.Api
    ) -> dict[str, pulumi_aws.apigatewayv2.Authorizer]:
        result: dict[str, pulumi_aws.apigatewayv2.Authorizer] = {}
        for name, auth in self._authorizers.items():
            if isinstance(auth, _LambdaAuthorizer):
                authorizer_type = "REQUEST"
                payload_version = "2.0"
                auth_resource = pulumi_aws.apigatewayv2.Authorizer(
                    safe_name(context().prefix(), f"{self.name}-authorizer-{name}", 128),
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
                pulumi_aws.lambda_.Permission(
                    safe_name(
                        context().prefix(),
                        f"{self.name}-auth-permission-{name}",
                        128,
                    ),
                    action="lambda:InvokeFunction",
                    function=auth.function.function_name,
                    principal="apigateway.amazonaws.com",
                    source_arn=Output.concat(api.execution_arn, "/authorizers/*"),
                    opts=self._resource_opts(),
                )
                result[name] = auth_resource

            elif isinstance(auth, _JwtAuthorizer):
                auth_resource = pulumi_aws.apigatewayv2.Authorizer(
                    safe_name(context().prefix(), f"{self.name}-authorizer-{name}", 128),
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
                auth_resource = pulumi_aws.apigatewayv2.Authorizer(
                    safe_name(context().prefix(), f"{self.name}-authorizer-{name}", 128),
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
        api: pulumi_aws.apigatewayv2.Api,
        stage: pulumi_aws.apigatewayv2.Stage,
        domain: HttpApiDomain,
    ) -> pulumi_aws.apigatewayv2.ApiMapping:
        # Register for duplicate detection
        domain.register_mapping(self.name, self._config.api_mapping_key)

        mapping_args: dict[str, Any] = {
            "api_id": api.id,
            "domain_name": domain.resources.custom_domain.domain_name,
            "stage": stage.id,
        }
        if self._config.api_mapping_key is not None:
            mapping_args["api_mapping_key"] = self._config.api_mapping_key

        return pulumi_aws.apigatewayv2.ApiMapping(
            context().prefix(f"{self.name}-api-mapping"),
            **self._customizer("api_mapping", mapping_args),
            opts=self._resource_opts(),
        )


@link_config_creator(HttpApi)
def _http_api_link_creator(api: HttpApi) -> LinkConfig:
    return LinkConfig(
        properties={
            "url": api.url,
            "execution_arn": api.execution_arn,
        },
        permissions=[],
    )
