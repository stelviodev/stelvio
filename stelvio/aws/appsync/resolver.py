from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, final

from pulumi import ResourceOptions
from pulumi_aws import appsync

from stelvio import context
from stelvio.aws.appsync.config import (
    AppSyncPipeFunctionCustomizationDict,
    AppSyncResolverCustomizationDict,
)
from stelvio.aws.appsync.constants import (
    APPSYNC_JS_RUNTIME,
    APPSYNC_JS_RUNTIME_VERSION,
    DS_TYPE_LAMBDA,
    NONE_PASSTHROUGH_CODE,
)
from stelvio.component import Component, safe_name
from stelvio.project import get_project_root
from stelvio.pulumi import normalize_pulumi_args_to_dict as _normalize

if TYPE_CHECKING:
    from stelvio.aws.appsync.appsync import AppSync
    from stelvio.aws.appsync.data_source import AppSyncDataSource


def _read_code_or_inline(value: str) -> str:
    """Read JS resolver code from file or treat as inline code.

    Treats value as a file path if it ends in .js.
    Otherwise returns the value as inline code.
    """
    if value.endswith(".js"):
        file_path = (Path(get_project_root()) / value).resolve()
        if not file_path.is_file():
            raise FileNotFoundError(f"File '{value}' not found (resolved to '{file_path}').")
        return file_path.read_text()
    return value


@final
@dataclass(frozen=True)
class AppSyncResolverResources:
    resolver: appsync.Resolver


@final
@dataclass(frozen=True)
class AppSyncPipeFunctionResources:
    function: appsync.Function


@final
class AppSyncResolver(Component[AppSyncResolverResources, AppSyncResolverCustomizationDict]):
    """A resolver registered with an AppSync API.

    Created by AppSync resolver methods (query, mutation, subscription, resolver).
    """

    def __init__(  # noqa: PLR0913
        self,
        name: str,
        api: "AppSync",
        type_name: str,
        field_name: str,
        data_source: "AppSyncDataSource | list[PipeFunction] | None",
        *,
        code: str | None = None,
        customize: AppSyncResolverCustomizationDict | None = None,
    ) -> None:
        self._api = api
        self._type_name = type_name
        self._field_name = field_name
        self._data_source = data_source
        self._code = code
        super().__init__(name, customize=customize)

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
    def is_pipeline(self) -> bool:
        return isinstance(self._data_source, list)

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

        if self.is_pipeline:
            self._build_pipeline_args(resolver_args)
            deps: list[Any] = [pf.resources.function for pf in self._data_source]
        else:
            self._build_unit_args(resolver_args)
            if self._data_source is not None:
                deps = [self._data_source.resources.data_source]
            else:
                deps = [self._api.resources.none_data_source]

        pulumi_resolver = appsync.Resolver(
            safe_name(prefix(), f"{self._api.name}-{self._type_name}-{self._field_name}", 128),
            **{
                **self._api._customizer("resolver", resolver_args),  # noqa: SLF001
                **_normalize(self._customize.get("resolver")),
            },
            opts=ResourceOptions(depends_on=deps),
        )

        return AppSyncResolverResources(resolver=pulumi_resolver)

    def _build_pipeline_args(self, resolver_args: dict[str, Any]) -> None:
        functions = self._data_source
        resolver_args["kind"] = "PIPELINE"
        resolver_args["pipeline_config"] = appsync.ResolverPipelineConfigArgs(
            functions=[pf.resources.function.function_id for pf in functions],
        )
        resolver_args["code"] = (
            _read_code_or_inline(self._code) if self._code else NONE_PASSTHROUGH_CODE
        )

    def _build_unit_args(self, resolver_args: dict[str, Any]) -> None:
        resolver_args["kind"] = "UNIT"

        if self._data_source is None:
            resolver_args["data_source"] = "NONE"
            resolver_args["code"] = (
                _read_code_or_inline(self._code) if self._code else NONE_PASSTHROUGH_CODE
            )
        else:
            ds = self._data_source
            resolver_args["data_source"] = ds.ds_name

            if ds.ds_type == DS_TYPE_LAMBDA and self._code is None:
                # Direct Lambda Resolver — no code, no runtime
                del resolver_args["runtime"]
            elif self._code:
                resolver_args["code"] = _read_code_or_inline(self._code)


@final
class PipeFunction(Component[AppSyncPipeFunctionResources, AppSyncPipeFunctionCustomizationDict]):
    """A pipeline function (step) registered with an AppSync API.

    Created by AppSync.pipe_function(). Pass a list of PipeFunctions as the
    data_source argument to resolver methods for pipeline resolvers.
    """

    def __init__(
        self,
        name: str,
        api: "AppSync",
        data_source: "AppSyncDataSource | None",
        *,
        code: str,
        customize: AppSyncPipeFunctionCustomizationDict | None = None,
    ) -> None:
        self._api = api
        self._pf_name = name
        self._data_source = data_source
        self._code = code
        super().__init__(f"{api.name}-fn-{name}", customize=customize)

    @property
    def pf_name(self) -> str:
        """The pipe function name within the AppSync API."""
        return self._pf_name

    @property
    def data_source(self) -> "AppSyncDataSource | None":
        return self._data_source

    @property
    def code(self) -> str:
        return self._code

    def _create_resources(self) -> AppSyncPipeFunctionResources:
        prefix = context().prefix
        api_id = self._api.resources.api.id

        ds_api_name = self._data_source.ds_name if self._data_source is not None else "NONE"

        if self._data_source is not None:
            ds_dep = self._data_source.resources.data_source
        else:
            ds_dep = self._api.resources.none_data_source

        fn_args: dict[str, Any] = {
            "api_id": api_id,
            "name": self._pf_name,
            "data_source": ds_api_name,
            "code": _read_code_or_inline(self._code),
            "runtime": appsync.FunctionRuntimeArgs(
                name=APPSYNC_JS_RUNTIME,
                runtime_version=APPSYNC_JS_RUNTIME_VERSION,
            ),
        }
        appsync_fn = appsync.Function(
            safe_name(prefix(), f"{self._api.name}-fn-{self._pf_name}", 128),
            **{
                **self._api._customizer("function", fn_args),  # noqa: SLF001
                **_normalize(self._customize.get("function")),
            },
            opts=ResourceOptions(depends_on=[ds_dep]),
        )

        return AppSyncPipeFunctionResources(function=appsync_fn)
