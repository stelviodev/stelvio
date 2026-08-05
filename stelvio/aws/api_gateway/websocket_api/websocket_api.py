from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypedDict, Unpack, final

from pulumi import Output
from pulumi_aws import apigatewayv2, lambda_

from stelvio import context
from stelvio.aws.api_gateway.domain import ApiDomain
from stelvio.aws.api_gateway.validators import validate_api_mapping_key, validate_domain_name
from stelvio.aws.function import Function, FunctionConfig, FunctionConfigDict, parse_handler_config
from stelvio.aws.permission import AwsPermission
from stelvio.component import Component, link_config_creator, safe_name
from stelvio.link import LinkableMixin, LinkConfig

PERMISSION_NAME_MAX_LENGTH = 100
DEFAULT_STAGE_NAME = "$default"


# Lifecycle routes cannot have route responses; message routes need them for
# Lambda proxy return values to reach the client (two-way communication).
_LIFECYCLE_ROUTE_KEYS = frozenset({"$connect", "$disconnect"})


class WebsocketApiConfigDict(TypedDict, total=False):
    domain_name: str
    domain: ApiDomain
    api_mapping_key: str
    disable_execute_api_endpoint: bool


@final
@dataclass(frozen=True, kw_only=True)
class WebsocketApiConfig:
    domain_name: str | None = None
    domain: ApiDomain | None = None
    api_mapping_key: str | None = None
    disable_execute_api_endpoint: bool = False

    def __post_init__(self) -> None:
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


def _build_url(
    *,
    domain: str | None,
    mapping_key: str | None,
    stage_invoke_url: Output[str] | None,
) -> Output[str]:
    """Build the base invoke URL for a WebSocket API (`wss://`)."""
    if domain is not None:
        if mapping_key:
            return Output.from_input(f"wss://{domain}/{mapping_key}")
        return Output.from_input(f"wss://{domain}")
    if stage_invoke_url is None:
        raise ValueError("stage_invoke_url is required when domain is not set")
    return stage_invoke_url


@final
@dataclass(frozen=True)
class WebsocketApiResources:
    api: apigatewayv2.Api
    stage: apigatewayv2.Stage
    integrations: list[apigatewayv2.Integration]
    routes: list[apigatewayv2.Route]
    route_responses: list[apigatewayv2.RouteResponse]
    permissions: list[lambda_.Permission]
    api_mapping: apigatewayv2.ApiMapping | None = None


class WebsocketApiCustomizationDict(TypedDict, total=False):
    api: apigatewayv2.ApiArgs | dict[str, Any] | None
    stage: apigatewayv2.StageArgs | dict[str, Any] | None
    api_mapping: apigatewayv2.ApiMappingArgs | dict[str, Any] | None


@final
class WebsocketApi(
    Component[WebsocketApiResources, WebsocketApiCustomizationDict],
    LinkableMixin,
):
    """AWS API Gateway WebSocket API backed by Lambda proxy integrations."""

    def __init__(
        self,
        name: str,
        config: WebsocketApiConfig | WebsocketApiConfigDict | None = None,
        *,
        tags: dict[str, str] | None = None,
        customize: WebsocketApiCustomizationDict | None = None,
        **opts: Unpack[WebsocketApiConfigDict],
    ) -> None:
        super().__init__("stelvio:aws:WebsocketApi", name, tags=tags, customize=customize)
        self._routes: list[tuple[str, FunctionConfig | Function]] = []
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
                "Add all routes before accessing the .resources property."
            )

    def route(
        self,
        route_key: str,
        handler: str | FunctionConfig | FunctionConfigDict | Function,
        /,
        **function_options: Unpack[FunctionConfigDict],
    ) -> None:
        """Register a native WebSocket route key and Lambda handler."""
        self._check_not_created()
        if any(existing_key == route_key for existing_key, _ in self._routes):
            raise ValueError(f"Duplicate route key: '{route_key}'. Each route key must be unique.")
        if isinstance(handler, Function):
            if function_options:
                raise ValueError("Cannot combine a Function handler with function options.")
            resolved_handler: FunctionConfig | Function = handler
        else:
            resolved_handler = parse_handler_config(handler, function_options)
        self._routes.append((route_key, resolved_handler))

    @property
    def api_id(self) -> Output[str]:
        return self.resources.api.id

    @property
    def arn(self) -> Output[str]:
        return self.resources.api.arn

    @property
    def execution_arn(self) -> Output[str]:
        return self.resources.api.execution_arn

    @property
    def url(self) -> Output[str]:
        domain = self.domain_name
        return _build_url(
            domain=domain,
            mapping_key=self._config.api_mapping_key,
            stage_invoke_url=self.resources.stage.invoke_url if domain is None else None,
        )

    def _create_resources(self) -> WebsocketApiResources:
        domain = self._resolve_domain()
        api = apigatewayv2.Api(
            safe_name(context().prefix(), self.name, 128),
            **self._customizer(
                "api",
                {
                    "protocol_type": "WEBSOCKET",
                    "route_selection_expression": "$request.body.action",
                    "disable_execute_api_endpoint": self._config.disable_execute_api_endpoint,
                },
                inject_tags=True,
            ),
            opts=self._resource_opts(),
        )

        functions = self._resolve_functions()
        integrations = self._create_integrations(api, functions)
        routes = self._create_routes(api, integrations)
        route_responses = self._create_route_responses(api, routes)
        permissions = self._create_permissions(api, functions)
        # Stage is always $default (not configurable in v1; HttpApi allows stage_name).
        stage = apigatewayv2.Stage(
            context().prefix(f"{self.name}-stage"),
            **self._customizer(
                "stage",
                {"api_id": api.id, "name": DEFAULT_STAGE_NAME, "auto_deploy": True},
                inject_tags=True,
            ),
            opts=self._resource_opts(),
        )
        api_mapping = None
        if domain is not None:
            api_mapping = self._create_api_mapping(api, stage, domain)
        output_url = _build_url(
            domain=domain.domain_name if domain is not None else None,
            mapping_key=self._config.api_mapping_key,
            stage_invoke_url=stage.invoke_url if domain is None else None,
        )
        self.register_outputs(
            {
                "url": output_url,
                "management_url": stage.invoke_url.apply(
                    lambda u: u.replace("wss://", "https://").replace("/$default", "")
                ),
            }
        )
        return WebsocketApiResources(
            api=api,
            stage=stage,
            integrations=list(integrations.values()),
            routes=routes,
            route_responses=route_responses,
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

    def _create_api_mapping(
        self,
        api: apigatewayv2.Api,
        stage: apigatewayv2.Stage,
        domain: ApiDomain,
    ) -> apigatewayv2.ApiMapping:
        domain.register_mapping(self.name, self._config.api_mapping_key)
        mapping_args = {
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
        handlers: dict[str, FunctionConfig | Function] = {}
        for _, handler in self._routes:
            key = self._handler_key(handler)
            existing = handlers.get(key)
            if existing is None:
                handlers[key] = handler
            elif isinstance(existing, FunctionConfig) and isinstance(handler, FunctionConfig):
                if not existing.has_only_defaults and not handler.has_only_defaults:
                    raise ValueError(
                        f"Multiple routes trying to configure the same lambda function: {key}"
                    )
                if existing.has_only_defaults:
                    handlers[key] = handler

        return {
            key: handler
            if isinstance(handler, Function)
            else Function(self._function_name(key), config=handler, tags=self._tags, parent=self)
            for key, handler in handlers.items()
        }

    @staticmethod
    def _handler_key(handler: FunctionConfig | Function) -> str:
        return handler.name if isinstance(handler, Function) else handler.full_handler_path

    def _function_name(self, key: str) -> str:
        return f"{self.name}-{key.replace('/', '-').replace('.', '_').replace('::', '-')}"

    @staticmethod
    def _route_resource_name(route_key: str) -> str:
        if route_key.startswith("$"):
            return f"sys-{route_key[1:]}"
        return route_key.replace("/", "-").replace(" ", "-")

    def _create_integrations(
        self, api: apigatewayv2.Api, functions: dict[str, Function]
    ) -> dict[str, apigatewayv2.Integration]:
        return {
            key: apigatewayv2.Integration(
                context().prefix(f"{self.name}-integration-{self._function_name(key)}"),
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
    ) -> list[apigatewayv2.Route]:
        routes = []
        for route_key, handler in self._routes:
            key = self._handler_key(handler)
            route_name = self._route_resource_name(route_key)
            route_args: dict[str, Any] = {
                "api_id": api.id,
                "route_key": route_key,
                "target": Output.concat("integrations/", integrations[key].id),
            }
            if route_key not in _LIFECYCLE_ROUTE_KEYS:
                # Required with RouteResponse for Lambda proxy replies to reach clients.
                route_args["route_response_selection_expression"] = "$default"
            routes.append(
                apigatewayv2.Route(
                    context().prefix(f"{self.name}-route-{route_name}"),
                    **route_args,
                    opts=self._resource_opts(),
                )
            )
        return routes

    def _create_route_responses(
        self, api: apigatewayv2.Api, routes: list[apigatewayv2.Route]
    ) -> list[apigatewayv2.RouteResponse]:
        return [
            apigatewayv2.RouteResponse(
                context().prefix(
                    f"{self.name}-route-response-{self._route_resource_name(route_key)}"
                ),
                api_id=api.id,
                route_id=route.id,
                route_response_key="$default",
                opts=self._resource_opts(),
            )
            for route_key, route in zip((key for key, _ in self._routes), routes, strict=True)
            if route_key not in _LIFECYCLE_ROUTE_KEYS
        ]

    def _create_permissions(
        self, api: apigatewayv2.Api, functions: dict[str, Function]
    ) -> list[lambda_.Permission]:
        return [
            lambda_.Permission(
                safe_name(
                    context().prefix(),
                    f"{self.name}-permission-{self._function_name(key)}",
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


@link_config_creator(WebsocketApi)
def _websocket_api_link_creator(api: WebsocketApi) -> LinkConfig:
    return LinkConfig(
        properties={"api_url": api.url, "api_execution_arn": api.execution_arn},
        permissions=[
            AwsPermission(
                actions=["execute-api:ManageConnections"],
                resources=[Output.concat(api.execution_arn, "/*/@connections/*")],
            ),
        ],
    )
