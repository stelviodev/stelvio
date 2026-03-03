from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, final

import pulumi
from pulumi_aws import appsync

from stelvio import context
from stelvio.aws.appsync.constants import (
    APPSYNC_JS_RUNTIME,
    APPSYNC_JS_RUNTIME_VERSION,
    DS_TYPE_LAMBDA,
    NONE_PASSTHROUGH_CODE,
)
from stelvio.aws.appsync.file_inputs import read_js_code_input
from stelvio.component import Component, safe_name

if TYPE_CHECKING:
    from stelvio.aws.appsync.appsync import AppSync
    from stelvio.aws.appsync.config import (
        AppSyncPipeFunctionCustomizationDict,
        AppSyncResolverCustomizationDict,
    )
    from stelvio.aws.appsync.data_source import AppSyncDataSource


@final
@dataclass(frozen=True)
class AppSyncResolverResources:
    resolver: "appsync.Resolver"


@final
@dataclass(frozen=True)
class AppSyncPipeFunctionResources:
    function: "appsync.Function"


@final
class AppSyncResolver(Component[AppSyncResolverResources, "AppSyncResolverCustomizationDict"]):
    """A resolver registered with an AppSync API.

    Created by AppSync resolver methods (query, mutation, subscription, resolver).
    """

    def __init__(  # noqa: PLR0913
        self,
        api: "AppSync",
        type_name: str,
        field_name: str,
        data_source: "AppSyncDataSource | list[PipeFunction] | None",
        *,
        code: str | None = None,
        customize: "AppSyncResolverCustomizationDict | None" = None,
    ) -> None:
        super().__init__(
            "stelvio:aws:AppSyncResolver",
            f"{api.name}-resolver-{type_name}-{field_name}",
            customize=customize,
        )
        self._api = api
        self._type_name = type_name
        self._field_name = field_name
        self._data_source = data_source
        self._code = code

    @property
    def type_name(self) -> str:
        return self._type_name

    @property
    def field_name(self) -> str:
        return self._field_name

    def _create_resources(self) -> AppSyncResolverResources:
        prefix = context().prefix
        api_id = self._api.resources.api.id

        resolver_args: dict[str, Any] = {
            "api_id": api_id,
            "type": self._type_name,
            "field": self._field_name,
            "runtime": appsync.ResolverRuntimeArgs(
                name=APPSYNC_JS_RUNTIME,
                runtime_version=APPSYNC_JS_RUNTIME_VERSION,
            ),
        }

        if isinstance(self._data_source, list):
            functions = self._data_source
            resolver_args["kind"] = "PIPELINE"
            resolver_args["pipeline_config"] = appsync.ResolverPipelineConfigArgs(
                functions=[pf.resources.function.function_id for pf in functions],
            )
            resolver_args["code"] = (
                read_js_code_input(self._code) if self._code else NONE_PASSTHROUGH_CODE
            )
            deps = [pf.resources.function for pf in functions]
        else:
            resolver_args["kind"] = "UNIT"
            if self._data_source is None:
                resolver_args["data_source"] = "NONE"
                resolver_args["code"] = (
                    read_js_code_input(self._code) if self._code else NONE_PASSTHROUGH_CODE
                )
                deps = [self._api.none_data_source]
            else:
                ds = self._data_source
                resolver_args["data_source"] = ds.name
                if ds.ds_type == DS_TYPE_LAMBDA and self._code is None:
                    del resolver_args["runtime"]
                elif self._code:
                    resolver_args["code"] = read_js_code_input(self._code)
                deps = [ds.resources.data_source]

        resolver = appsync.Resolver(
            safe_name(prefix(), f"{self._api.name}-{self._type_name}-{self._field_name}", 128),
            **self._customizer("resolver", resolver_args),
            opts=self._resource_opts(depends_on=deps),
        )
        resources = AppSyncResolverResources(resolver=resolver)
        self.register_outputs(
            {"type": self._type_name, "field": self._field_name, "arn": resolver.arn}
        )
        pulumi.export(
            f"appsync_{self._api.name}_{self._type_name}_{self._field_name}_resolver",
            resolver.arn,
        )
        return resources


@final
class PipeFunction(
    Component[AppSyncPipeFunctionResources, "AppSyncPipeFunctionCustomizationDict"]
):
    """A pipeline function (step) registered with an AppSync API.

    Created by AppSync.pipe_function(). Pass a list of PipeFunctions as the
    data_source argument to resolver methods for pipeline resolvers.
    """

    def __init__(
        self,
        api: "AppSync",
        name: str,
        data_source: "AppSyncDataSource | None",
        *,
        code: str,
        customize: "AppSyncPipeFunctionCustomizationDict | None" = None,
    ) -> None:
        super().__init__("stelvio:aws:PipeFunction", f"{api.name}-fn-{name}", customize=customize)
        self._pipe_function_name = name
        self._api = api
        self._data_source = data_source
        self._code = code

    @property
    def name(self) -> str:
        return getattr(self, "_pipe_function_name", self._name)

    @property
    def api_name(self) -> str:
        return self._api.name

    def _create_resources(self) -> AppSyncPipeFunctionResources:
        prefix = context().prefix
        api_id = self._api.resources.api.id

        data_source_name = self._data_source.name if self._data_source is not None else "NONE"
        ds_dep = (
            self._data_source.resources.data_source
            if self._data_source
            else self._api.none_data_source
        )

        fn_args: dict[str, Any] = {
            "api_id": api_id,
            "name": self.name,
            "data_source": data_source_name,
            "code": read_js_code_input(self._code),
            "runtime": appsync.FunctionRuntimeArgs(
                name=APPSYNC_JS_RUNTIME,
                runtime_version=APPSYNC_JS_RUNTIME_VERSION,
            ),
        }
        appsync_fn = appsync.Function(
            safe_name(prefix(), f"{self._api.name}-fn-{self.name}", 128),
            **self._customizer("function", fn_args),
            opts=self._resource_opts(depends_on=[ds_dep]),
        )

        resources = AppSyncPipeFunctionResources(function=appsync_fn)
        self.register_outputs({"name": self.name, "arn": appsync_fn.arn})
        pulumi.export(
            f"appsync_{self._api.name}_{self.name}_pipe_function",
            appsync_fn.arn,
        )
        return resources
