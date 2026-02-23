from dataclasses import dataclass
from typing import TYPE_CHECKING, final

if TYPE_CHECKING:
    from pulumi_aws import appsync

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


class AppSyncResolver:
    """A resolver registered with an AppSync API.

    Created by AppSync resolver methods (query, mutation, subscription, resolver).
    """

    def __init__(
        self,
        type_name: str,
        field_name: str,
        data_source: "AppSyncDataSource | list[PipeFunction] | None",
        *,
        code: str | None = None,
        customize: "AppSyncResolverCustomizationDict | None" = None,
    ) -> None:
        self._type_name = type_name
        self._field_name = field_name
        self._data_source = data_source
        self._code = code
        self._customize = customize or {}
        self._resources: AppSyncResolverResources | None = None

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

    @property
    def resources(self) -> AppSyncResolverResources:
        if self._resources is None:
            raise RuntimeError(
                f"Resolver '{self._type_name}.{self._field_name}' resources have not been "
                "created yet. Access the AppSync component's .resources first."
            )
        return self._resources

    def _set_resources(self, resources: AppSyncResolverResources) -> None:
        self._resources = resources


class PipeFunction:
    """A pipeline function (step) registered with an AppSync API.

    Created by AppSync.pipe_function(). Pass a list of PipeFunctions as the
    data_source argument to resolver methods for pipeline resolvers.
    """

    def __init__(
        self,
        name: str,
        data_source: "AppSyncDataSource | None",
        *,
        code: str,
        customize: "AppSyncPipeFunctionCustomizationDict | None" = None,
    ) -> None:
        self._name = name
        self._data_source = data_source
        self._code = code
        self._customize = customize or {}
        self._resources: AppSyncPipeFunctionResources | None = None
        self._api_name: str | None = None

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
        return self._api_name

    @property
    def resources(self) -> AppSyncPipeFunctionResources:
        if self._resources is None:
            raise RuntimeError(
                f"Pipe function '{self._name}' resources have not been created yet. "
                "Access the AppSync component's .resources first."
            )
        return self._resources

    def _set_resources(self, resources: AppSyncPipeFunctionResources) -> None:
        self._resources = resources

    def set_api_name(self, api_name: str) -> None:
        self._api_name = api_name
