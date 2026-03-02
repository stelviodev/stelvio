from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, final

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

    @property
    def data_source(self) -> "AppSyncDataSource | list[PipeFunction] | None":
        return self._data_source

    @property
    def code(self) -> str | None:
        return self._code

    @property
    def customize(self) -> "AppSyncResolverCustomizationDict":
        return self._customize

    @property
    def is_pipeline(self) -> bool:
        return isinstance(self._data_source, list)

    def _create_resources(self) -> AppSyncResolverResources:
        if self._api._resources is None:  # noqa: SLF001
            raise RuntimeError(
                f"Resolver '{self.type_name}.{self.field_name}' resources have not been "
                "created yet. Access the AppSync component's .resources first."
            )

        prefix = context().prefix
        api_id = self._api.resources.api.id

        resolver_args: dict[str, Any] = {
            "api_id": api_id,
            "type": self.type_name,
            "field": self.field_name,
            "runtime": appsync.ResolverRuntimeArgs(
                name=APPSYNC_JS_RUNTIME,
                runtime_version=APPSYNC_JS_RUNTIME_VERSION,
            ),
        }

        if self.is_pipeline:
            functions = self.data_source
            resolver_args["kind"] = "PIPELINE"
            resolver_args["pipeline_config"] = appsync.ResolverPipelineConfigArgs(
                functions=[pf.resources.function.function_id for pf in functions],
            )
            resolver_args["code"] = (
                read_js_code_input(self.code) if self.code else NONE_PASSTHROUGH_CODE
            )
            deps = [pf.resources.function for pf in functions]
        else:
            resolver_args["kind"] = "UNIT"
            if self.data_source is None:
                resolver_args["data_source"] = "NONE"
                resolver_args["code"] = (
                    read_js_code_input(self.code) if self.code else NONE_PASSTHROUGH_CODE
                )
                deps = [self._api.none_data_source]
            else:
                ds = self.data_source
                resolver_args["data_source"] = ds.name
                if ds.ds_type == DS_TYPE_LAMBDA and self.code is None:
                    del resolver_args["runtime"]
                elif self.code:
                    resolver_args["code"] = read_js_code_input(self.code)
                deps = [ds.resources.data_source]

        resolver = appsync.Resolver(
            safe_name(prefix(), f"{self._api.name}-{self.type_name}-{self.field_name}", 128),
            **self._customizer("resolver", resolver_args),
            opts=self._resource_opts(depends_on=deps),
        )
        resources = AppSyncResolverResources(resolver=resolver)
        self.register_outputs(
            {"type": self.type_name, "field": self.field_name, "arn": resolver.arn}
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
        self._name = name
        self._api = api
        self._data_source = data_source
        self._code = code

    @property
    def name(self) -> str:
        return self._name

    @property
    def data_source(self) -> "AppSyncDataSource | None":
        return self._data_source

    @property
    def code(self) -> str:
        return self._code

    @property
    def customize(self) -> "AppSyncPipeFunctionCustomizationDict":
        return self._customize

    @property
    def api_name(self) -> str | None:
        return self._api.name

    def _create_resources(self) -> AppSyncPipeFunctionResources:
        if self._api._resources is None:  # noqa: SLF001
            raise RuntimeError(
                f"Pipe function '{self.name}' resources have not been created yet. "
                "Access the AppSync component's .resources first."
            )

        prefix = context().prefix
        data_source_name = self.data_source.name if self.data_source is not None else "NONE"
        ds_dep = (
            self.data_source.resources.data_source
            if self.data_source
            else self._api.none_data_source
        )

        fn_args: dict[str, Any] = {
            "api_id": self._api.resources.api.id,
            "name": self.name,
            "data_source": data_source_name,
            "code": read_js_code_input(self.code),
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
        return resources
