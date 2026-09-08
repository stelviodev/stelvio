from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property
from typing import Any, Literal, TypedDict, Unpack, final

from pulumi import Output
from pulumi_aws import apigatewayv2, cloudwatch, lambda_

from stelvio import context
from stelvio.aws.api_gateway.domain import ApiDomain, build_url
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
from stelvio.aws.function import Function, FunctionConfig, FunctionConfigDict, parse_handler_config
from stelvio.aws.permission import AwsPermission
from stelvio.component import Component, link_config_creator, safe_name
from stelvio.link import LinkableMixin, LinkConfig
from stelvio.provider import ProviderStore, aws_region_of

DEFAULT_ROUTE_SELECTION_EXPRESSION = "$request.body.action"
_RESERVED_ROUTE_KEYS = frozenset({"$connect", "$disconnect", "$default"})

# Default access-log format for WebSocket APIs (v2)
_ACCESS_LOG_FORMAT = (
    '{"requestId":"$context.requestId",'
    '"ip":"$context.identity.sourceIp",'
    '"requestTime":"$context.requestTime",'
    '"routeKey":"$context.routeKey",'
    '"connectionId":"$context.connectionId",'
    '"eventType":"$context.eventType",'
    '"status":"$context.status",'
    '"integrationErrorMessage":"$context.integrationErrorMessage"}'
)


class WebsocketApiConfigDict(TypedDict, total=False):
    domain_name: str
    domain: ApiDomain
    stage_name: str
    route_selection_expression: str
    api_mapping_key: str
    disable_execute_api_endpoint: bool
    access_log_retention_days: int | Literal["forever"]


@final
@dataclass(frozen=True, kw_only=True)
class WebsocketApiConfig:
    domain_name: str | None = None
    domain: ApiDomain | None = None
    stage_name: str = DEFAULT_STAGE_NAME
    route_selection_expression: str = DEFAULT_ROUTE_SELECTION_EXPRESSION
    api_mapping_key: str | None = None
    disable_execute_api_endpoint: bool = False
    access_log_retention_days: int | Literal["forever"] = 30

    def __post_init__(self) -> None:
        validate_stage_name(self.stage_name)
        if not self.route_selection_expression:
            raise ValueError("route_selection_expression cannot be empty")
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


@final
@dataclass(frozen=True)
class WebsocketApiResources:
    api: apigatewayv2.Api
    stage: apigatewayv2.Stage
    log_group: cloudwatch.LogGroup
    api_mapping: apigatewayv2.ApiMapping | None = None


class WebsocketApiCustomizationDict(TypedDict, total=False):
    api: apigatewayv2.ApiArgs | dict[str, Any] | None
    stage: apigatewayv2.StageArgs | dict[str, Any] | None
    log_group: cloudwatch.LogGroupArgs | dict[str, Any] | None
    api_mapping: apigatewayv2.ApiMappingArgs | dict[str, Any] | None


@final
@dataclass(frozen=True)
class _WebsocketLambdaAuthorizer:
    api: WebsocketApi
    name: str
    function: Function
    identity_sources: list[str]

    def __post_init__(self) -> None:
        if not isinstance(self.identity_sources, list) or not self.identity_sources:
            raise ValueError(
                f"Authorizer '{self.name}' requires a non-empty list of identity_sources."
            )
        if any(not isinstance(source, str) or not source for source in self.identity_sources):
            raise ValueError(
                f"Authorizer '{self.name}' identity_sources must contain only non-empty strings."
            )


@final
@dataclass(frozen=True)
class _WebsocketRoute:
    route_key: str
    handler: FunctionConfig | Function
    auth: _WebsocketLambdaAuthorizer | Literal["IAM"] | None = None

    def __post_init__(self) -> None:
        if not self.route_key:
            raise ValueError("WebSocket route key cannot be empty")
        if " " in self.route_key:
            raise ValueError(f"WebSocket route key {self.route_key!r} cannot contain spaces")
        if self.route_key.startswith("$") and self.route_key not in _RESERVED_ROUTE_KEYS:
            raise ValueError(
                f"Invalid WebSocket route key {self.route_key!r}. "
                "Keys starting with '$' must be $connect, $disconnect, or $default."
            )
        if not self.route_key.startswith("$") and self.route_key.replace("/", "-") in {
            f"sys-{k[1:]}" for k in _RESERVED_ROUTE_KEYS
        }:
            raise ValueError(
                f"Invalid WebSocket route key {self.route_key!r}. "
                "Custom keys cannot use names reserved for $connect, $disconnect, or $default."
            )

    @property
    def path(self) -> str:
        return self.route_key


@final
class WebsocketApi(
    Component[WebsocketApiResources, WebsocketApiCustomizationDict],
    LinkableMixin,
):
    """AWS API Gateway WebSocket API backed by Lambda proxy integrations."""

    _routes: list[_WebsocketRoute]
    _authorizers: dict[str, _WebsocketLambdaAuthorizer]
    _config: WebsocketApiConfig

    def __init__(
        self,
        name: str,
        config: WebsocketApiConfig | WebsocketApiConfigDict | None = None,
        *,
        tags: dict[str, str] | None = None,
        customize: WebsocketApiCustomizationDict | None = None,
        **opts: Unpack[WebsocketApiConfigDict],
    ) -> None:
        super().__init__(
            ProviderStore.aws(), "stelvio:aws:WebsocketApi", name, tags=tags, customize=customize
        )
        self._routes = []
        self._authorizers = {}
        if config is not None and opts:
            raise ValueError(
                "Invalid configuration: cannot combine 'config' parameter with additional"
                " options. Provide all settings either in 'config' or as separate options."
            )
        self._config = self._parse_config(config, opts)

    @staticmethod
    def _parse_config(
        config: WebsocketApiConfig | WebsocketApiConfigDict | None,
        opts: WebsocketApiConfigDict,
    ) -> WebsocketApiConfig:
        if config is None:
            return WebsocketApiConfig(**opts)
        if isinstance(config, WebsocketApiConfig):
            return config
        if isinstance(config, dict):
            return WebsocketApiConfig(**config)
        raise TypeError(
            "Invalid config type: expected WebsocketApiConfig or dict, "
            f"got {type(config).__name__}"
        )

    @property
    def domain_name(self) -> str | None:
        if self._config.domain:
            return self._config.domain.domain_name
        return self._config.domain_name

    @property
    def config(self) -> WebsocketApiConfig:
        return self._config

    def _check_not_created(self) -> None:
        if self._resources is not None:
            raise RuntimeError(
                f"Cannot modify WebsocketApi '{self.name}' after resources have been created. "
                "Add all routes and authorizers before accessing the .resources property."
            )

    def route(
        self,
        route_key: str,
        handler: str | FunctionConfig | FunctionConfigDict | Function,
        /,
        *,
        auth: _WebsocketLambdaAuthorizer | Literal["IAM"] | None = None,
        **function_options: Unpack[FunctionConfigDict],
    ) -> None:
        """Register a native WebSocket route key and Lambda handler."""
        self._check_not_created()
        if isinstance(handler, Function):
            if function_options:
                raise ValueError("Cannot combine a Function handler with function options.")
            resolved_handler: FunctionConfig | Function = handler
        else:
            resolved_handler = parse_handler_config(handler, function_options)
        ws_route = _WebsocketRoute(route_key, resolved_handler, auth)
        if auth is not None and route_key != "$connect":
            raise ValueError(
                "WebSocket authorization can only be configured on the '$connect' route."
            )
        if isinstance(auth, _WebsocketLambdaAuthorizer) and auth.api is not self:
            raise ValueError(f"Authorizer '{auth.name}' belongs to a different WebsocketApi.")
        if auth is not None and auth != "IAM" and not isinstance(auth, _WebsocketLambdaAuthorizer):
            raise TypeError(
                f"Unsupported auth type for route '{route_key}': {type(auth).__name__}"
            )
        if any(existing.route_key == route_key for existing in self._routes):
            raise ValueError(f"Duplicate route key: '{route_key}'. Each route key must be unique.")
        self._routes.append(ws_route)

    def add_lambda_authorizer(
        self,
        name: str,
        handler: str | FunctionConfig | FunctionConfigDict | Function,
        /,
        *,
        identity_sources: list[str],
        **function_options: Unpack[FunctionConfigDict],
    ) -> _WebsocketLambdaAuthorizer:
        """Register a Lambda REQUEST authorizer for the `$connect` route."""
        self._check_not_created()
        if name in self._authorizers:
            raise ValueError(
                f"Duplicate authorizer name: '{name}'. Authorizer names must be unique."
            )

        if isinstance(handler, Function):
            if function_options:
                raise ValueError("Cannot combine a Function handler with function options.")
            function = handler
        else:
            function = Function(
                f"{self.name}-auth-{name}",
                config=parse_handler_config(handler, function_options),
                tags=self._tags,
                parent=self,
            )

        authorizer = _WebsocketLambdaAuthorizer(
            api=self,
            name=name,
            function=function,
            identity_sources=identity_sources,
        )
        self._authorizers[name] = authorizer
        return authorizer

    @cached_property
    def _api_resource(self) -> apigatewayv2.Api:
        # Created early so route Lambdas can `links=[api]` before full `.resources`.
        return apigatewayv2.Api(
            safe_name(context().prefix(), self.name, 128),
            **self._customizer(
                "api",
                {
                    "protocol_type": "WEBSOCKET",
                    "route_selection_expression": self._config.route_selection_expression,
                    "disable_execute_api_endpoint": self._config.disable_execute_api_endpoint,
                },
                inject_tags=True,
            ),
            opts=self._resource_opts(),
        )

    @property
    def api_id(self) -> Output[str]:
        return self.resources.api.id

    @property
    def arn(self) -> Output[str]:
        return self.resources.api.arn

    @property
    def execution_arn(self) -> Output[str]:
        return self.resources.api.execution_arn

    def _execute_api_url(self, scheme: str) -> Output[str]:
        # Merged customize without creating Stage — reading url must not lock.
        # Callable customizers can return arbitrary dicts; url only needs `name`.
        stage_name = self._customizer("stage", {"name": self._config.stage_name}).get(
            "name", self._config.stage_name
        )
        region = aws_region_of(self)
        return self._api_resource.id.apply(
            lambda api_id: f"{scheme}://{api_id}.execute-api.{region}.amazonaws.com/{stage_name}"
        )

    @property
    def url(self) -> Output[str]:
        domain = self.domain_name
        if domain is not None:
            return build_url("wss", domain, self._config.api_mapping_key)
        # Include the stage path — WebSocket invoke URLs always use the stage name
        # (unlike HTTP APIs, which omit $default). Built from api id so route
        # Lambdas can link to this API before Stage exists.
        return self._execute_api_url("wss")

    @property
    def management_url(self) -> Output[str]:
        # Always execute-api HTTPS — never the custom-domain / wss client URL.
        return self._execute_api_url("https")

    def _create_resources(self) -> WebsocketApiResources:
        if not self._routes:
            raise ValueError(
                f"WebsocketApi '{self.name}' has no routes. "
                "Add at least one route() before deploying."
            )
        self._validate_authorizers_used()
        domain = self._resolve_domain()
        api = self._api_resource

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
        account = _create_api_gateway_account_and_role()

        functions = self._resolve_functions()
        authorizers, _ = self._materialize_authorizers(api)
        integrations = self._create_integrations(api, functions)
        routes = self._create_routes(api, integrations, authorizers)
        self._create_route_permissions(api, functions)
        # Stage after routes: WebSocket auto_deploy fails if the API has no routes yet.
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
            opts=self._resource_opts(depends_on=[*routes, account, log_group]),
        )
        api_mapping = None
        if domain is not None:
            api_mapping = self._create_api_mapping(api, stage, domain)
        self.register_outputs({"url": self.url, "management_url": self.management_url})
        return WebsocketApiResources(
            api=api,
            stage=stage,
            log_group=log_group,
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

    def _create_route_permissions(
        self, api: apigatewayv2.Api, functions: dict[str, Function]
    ) -> list[lambda_.Permission]:
        return [
            lambda_.Permission(
                safe_name(
                    context().prefix(),
                    f"{self.name}-permission-{fn_name_from_key(self.name, key)}",
                    PERMISSION_NAME_MAX_LENGTH,
                ),
                action="lambda:InvokeFunction",
                function=function.function_name,
                principal="apigateway.amazonaws.com",
                source_arn=Output.concat(api.execution_arn, "/*/*"),
                opts=self._resource_opts(),
            )
            for key, function in functions.items()
        ]

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

    def _resolve_functions(self) -> dict[str, Function]:
        grouped = group_routes_by_handler(self._routes)
        group_config_map = get_group_config_map(grouped)
        return {key: self._resolve_group_function(key, group_config_map[key]) for key in grouped}

    def _resolve_group_function(self, key: str, route_with_config: _WebsocketRoute) -> Function:
        if isinstance(route_with_config.handler, Function):
            return route_with_config.handler
        return Function(
            fn_name_from_key(self.name, key),
            config=route_with_config.handler,
            tags=self._tags,
            parent=self,
        )

    @staticmethod
    def _handler_key(handler: FunctionConfig | Function) -> str:
        return handler.name if isinstance(handler, Function) else handler.full_handler_path

    @staticmethod
    def _route_resource_name(route_key: str) -> str:
        if route_key.startswith("$"):
            return f"sys-{route_key[1:]}"
        return route_key.replace("/", "-")

    def _create_integrations(
        self, api: apigatewayv2.Api, functions: dict[str, Function]
    ) -> dict[str, apigatewayv2.Integration]:
        return {
            key: apigatewayv2.Integration(
                context().prefix(f"{self.name}-integration-{fn_name_from_key(self.name, key)}"),
                api_id=api.id,
                integration_type="AWS_PROXY",
                integration_method="POST",
                integration_uri=function.invoke_arn,
                opts=self._resource_opts(),
            )
            for key, function in functions.items()
        }

    def _create_routes(
        self,
        api: apigatewayv2.Api,
        integrations: dict[str, apigatewayv2.Integration],
        authorizers: dict[str, apigatewayv2.Authorizer],
    ) -> list[apigatewayv2.Route]:
        routes = []
        for ws_route in self._routes:
            key = self._handler_key(ws_route.handler)
            route_name = self._route_resource_name(ws_route.route_key)
            route_args: dict[str, Any] = {
                "api_id": api.id,
                "route_key": ws_route.route_key,
                "target": Output.concat("integrations/", integrations[key].id),
            }
            if ws_route.auth == "IAM":
                route_args["authorization_type"] = "AWS_IAM"
            elif isinstance(ws_route.auth, _WebsocketLambdaAuthorizer):
                route_args["authorization_type"] = "CUSTOM"
                route_args["authorizer_id"] = authorizers[ws_route.auth.name].id
            routes.append(
                apigatewayv2.Route(
                    context().prefix(f"{self.name}-route-{route_name}"),
                    **route_args,
                    opts=self._resource_opts(),
                )
            )
        return routes

    def _referenced_authorizer_names(self) -> set[str]:
        return {
            route.auth.name
            for route in self._routes
            if isinstance(route.auth, _WebsocketLambdaAuthorizer)
        }

    def _validate_authorizers_used(self) -> None:
        unused = sorted(set(self._authorizers) - self._referenced_authorizer_names())
        if unused:
            names = ", ".join(repr(name) for name in unused)
            raise ValueError(
                f"WebsocketApi '{self.name}' has unused authorizer(s): {names}. "
                "Attach each authorizer to the '$connect' route via auth=..., "
                "or remove the unused add_lambda_authorizer() call(s)."
            )

    def _materialize_authorizers(
        self, api: apigatewayv2.Api
    ) -> tuple[dict[str, apigatewayv2.Authorizer], list[lambda_.Permission]]:
        authorizers = {}
        permissions = []
        for name, auth in self._authorizers.items():
            authorizer = apigatewayv2.Authorizer(
                context().prefix(f"{self.name}-authorizer-{name}"),
                api_id=api.id,
                authorizer_type="REQUEST",
                authorizer_uri=auth.function.invoke_arn,
                identity_sources=auth.identity_sources,
                name=name,
                opts=self._resource_opts(),
            )
            permissions.append(
                lambda_.Permission(
                    safe_name(
                        context().prefix(),
                        f"{self.name}-auth-permission-{name}",
                        PERMISSION_NAME_MAX_LENGTH,
                    ),
                    action="lambda:InvokeFunction",
                    function=auth.function.function_name,
                    principal="apigateway.amazonaws.com",
                    source_arn=Output.concat(api.execution_arn, "/authorizers/", authorizer.id),
                    opts=self._resource_opts(),
                )
            )
            authorizers[name] = authorizer
        return authorizers, permissions


@link_config_creator(WebsocketApi)
def _websocket_api_link_creator(api: WebsocketApi) -> LinkConfig:
    execution_arn = api._api_resource.execution_arn  # noqa: SLF001
    return LinkConfig(
        properties={
            "api_url": api.url,
            "api_execution_arn": execution_arn,
            "api_management_url": api.management_url,
        },
        permissions=[
            AwsPermission(
                actions=["execute-api:ManageConnections"],
                resources=[Output.concat(execution_arn, "/*/*/@connections/*")],
            ),
        ],
    )
