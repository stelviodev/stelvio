from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, Protocol

from pulumi import Output, ProviderResource, ResourceOptions
from pulumi_aws import apigatewayv2, cloudwatch, lambda_

from stelvio import context
from stelvio.aws.api_gateway.domain import ApiDomain
from stelvio.aws.api_gateway.validators import PERMISSION_NAME_MAX_LENGTH
from stelvio.component import safe_name

if TYPE_CHECKING:
    from stelvio.aws.function import Function


class _V2ApiConfig(Protocol):
    domain: ApiDomain | None
    domain_name: str | None
    api_mapping_key: str | None
    access_log_retention_days: int | Literal["forever"]
    stage_name: str


class _V2Api(Protocol):
    name: str
    config: _V2ApiConfig
    _tags: dict[str, str]

    def _customizer(
        self,
        resource_name: str,
        computed_props: dict[str, Any],
        default_props: dict[str, Any] | None = None,
        *,
        inject_tags: bool = False,
    ) -> dict[str, Any]: ...

    def _resource_opts(
        self,
        *,
        depends_on: list[Any] | None = None,
        provider: ProviderResource | None = None,
    ) -> ResourceOptions: ...


def fn_name_from_key(api_name: str, key: str) -> str:
    safe = key.replace("/", "-").replace(".", "_").replace("::", "-")
    return f"{api_name}-{safe}"


def resolve_domain(component: _V2Api) -> ApiDomain | None:
    config = component.config
    if config.domain is not None:
        return config.domain
    if config.domain_name is not None:
        return ApiDomain(
            f"{component.name}-domain",
            domain_name=config.domain_name,
            tags=component._tags,  # noqa: SLF001
            parent=component,  # type: ignore[arg-type]
        )
    return None


def create_log_group(component: _V2Api, api: apigatewayv2.Api) -> cloudwatch.LogGroup:
    log_group_args: dict[str, Any] = {
        "name": Output.concat("/aws/apigateway/", api.id),
    }
    if component.config.access_log_retention_days != "forever":
        log_group_args["retention_in_days"] = component.config.access_log_retention_days
    return cloudwatch.LogGroup(
        context().prefix(f"{component.name}-logs"),
        **component._customizer("log_group", log_group_args, inject_tags=True),  # noqa: SLF001
        opts=component._resource_opts(),  # noqa: SLF001
    )


def create_stage(
    component: _V2Api,
    api: apigatewayv2.Api,
    log_group: cloudwatch.LogGroup,
    *,
    access_log_format: str,
    depends_on: list[Any],
) -> apigatewayv2.Stage:
    return apigatewayv2.Stage(
        context().prefix(f"{component.name}-stage"),
        **component._customizer(  # noqa: SLF001
            "stage",
            {
                "api_id": api.id,
                "name": component.config.stage_name,
                "auto_deploy": True,
                "access_log_settings": {
                    "destination_arn": log_group.arn,
                    "format": access_log_format,
                },
            },
            inject_tags=True,
        ),
        opts=component._resource_opts(depends_on=depends_on),  # noqa: SLF001
    )


def create_api_mapping(
    component: _V2Api,
    api: apigatewayv2.Api,
    stage: apigatewayv2.Stage,
    domain: ApiDomain,
) -> apigatewayv2.ApiMapping:
    domain.register_mapping(component.name, component.config.api_mapping_key)
    mapping_args: dict[str, Any] = {
        "api_id": api.id,
        "domain_name": domain.resources.custom_domain.domain_name,
        "stage": stage.id,
    }
    if component.config.api_mapping_key is not None:
        mapping_args["api_mapping_key"] = component.config.api_mapping_key
    return apigatewayv2.ApiMapping(
        context().prefix(f"{component.name}-api-mapping"),
        **component._customizer("api_mapping", mapping_args),  # noqa: SLF001
        opts=component._resource_opts(),  # noqa: SLF001
    )


def create_route_permissions(
    component: _V2Api,
    api: apigatewayv2.Api,
    functions: dict[str, Function],
) -> list[lambda_.Permission]:
    return [
        lambda_.Permission(
            safe_name(
                context().prefix(),
                f"{component.name}-permission-{fn_name_from_key(component.name, key)}",
                PERMISSION_NAME_MAX_LENGTH,
            ),
            action="lambda:InvokeFunction",
            function=function.function_name,
            principal="apigateway.amazonaws.com",
            source_arn=Output.concat(api.execution_arn, "/*/*"),
            opts=component._resource_opts(),  # noqa: SLF001
        )
        for key, function in functions.items()
    ]
