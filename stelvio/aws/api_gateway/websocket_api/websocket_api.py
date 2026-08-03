from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypedDict, Unpack, final

from pulumi import Output
from pulumi_aws import apigatewayv2, lambda_

from stelvio import context
from stelvio.aws.function import Function, FunctionConfig, FunctionConfigDict, parse_handler_config
from stelvio.component import Component, link_config_creator, safe_name
from stelvio.link import LinkableMixin, LinkConfig

PERMISSION_NAME_MAX_LENGTH = 100


@final
@dataclass(frozen=True)
class WebsocketApiResources:
    api: apigatewayv2.Api
    stage: apigatewayv2.Stage
    integrations: list[apigatewayv2.Integration]
    routes: list[apigatewayv2.Route]
    route_responses: list[apigatewayv2.RouteResponse]
    permissions: list[lambda_.Permission]


class WebsocketApiCustomizationDict(TypedDict, total=False):
    api: apigatewayv2.ApiArgs | dict[str, Any] | None
    stage: apigatewayv2.StageArgs | dict[str, Any] | None


@final
class WebsocketApi(
    Component[WebsocketApiResources, WebsocketApiCustomizationDict],
    LinkableMixin,
):
    """AWS API Gateway WebSocket API backed by Lambda proxy integrations."""

    def __init__(
        self,
        name: str,
        *,
        tags: dict[str, str] | None = None,
        customize: WebsocketApiCustomizationDict | None = None,
    ) -> None:
        super().__init__("stelvio:aws:WebsocketApi", name, tags=tags, customize=customize)
        self._routes: list[tuple[str, FunctionConfig | Function]] = []

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
        return self.resources.stage.invoke_url

    def _create_resources(self) -> WebsocketApiResources:
        api = apigatewayv2.Api(
            safe_name(context().prefix(), self.name, 128),
            **self._customizer(
                "api",
                {
                    "protocol_type": "WEBSOCKET",
                    "route_selection_expression": "$request.body.action",
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
        stage = apigatewayv2.Stage(
            context().prefix(f"{self.name}-stage"),
            **self._customizer(
                "stage",
                {"api_id": api.id, "name": "$default", "auto_deploy": True},
                inject_tags=True,
            ),
            opts=self._resource_opts(),
        )
        self.register_outputs({"url": stage.invoke_url, "_arn": api.arn})
        return WebsocketApiResources(
            api=api,
            stage=stage,
            integrations=list(integrations.values()),
            routes=routes,
            route_responses=route_responses,
            permissions=permissions,
        )

    def _resolve_functions(self) -> dict[str, Function]:
        functions: dict[str, Function] = {}
        for _, handler in self._routes:
            key = self._handler_key(handler)
            if key not in functions:
                functions[key] = (
                    handler
                    if isinstance(handler, Function)
                    else Function(
                        self._function_name(key), config=handler, tags=self._tags, parent=self
                    )
                )
        return functions

    @staticmethod
    def _handler_key(handler: FunctionConfig | Function) -> str:
        return handler.name if isinstance(handler, Function) else handler.handler

    def _function_name(self, key: str) -> str:
        return f"{self.name}-{key.replace('/', '-').replace('.', '_').replace('::', '-')}"

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
            route_name = route_key.replace("$", "default-").replace("/", "-")
            routes.append(
                apigatewayv2.Route(
                    context().prefix(f"{self.name}-route-{route_name}"),
                    api_id=api.id,
                    route_key=route_key,
                    target=Output.concat("integrations/", integrations[key].id),
                    opts=self._resource_opts(),
                )
            )
        return routes

    # TODO: Linking
    def _create_route_responses(
        self, api: apigatewayv2.Api, routes: list[apigatewayv2.Route]
    ) -> list[apigatewayv2.RouteResponse]:
        return [
            apigatewayv2.RouteResponse(
                context().prefix(f"{self.name}-route-response-default"),
                api_id=api.id,
                route_id=route.id,
                route_response_key="$default",
                opts=self._resource_opts(),
            )
            for route_key, route in zip((key for key, _ in self._routes), routes, strict=True)
            if route_key == "$default"
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
        permissions=[],
    )
