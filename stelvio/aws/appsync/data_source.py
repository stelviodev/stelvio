from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, final

if TYPE_CHECKING:
    from pulumi_aws import appsync, iam

    from stelvio.aws.appsync.config import AppSyncDataSourceCustomizationDict
    from stelvio.aws.function import Function, FunctionConfig


@final
@dataclass(frozen=True)
class AppSyncDataSourceResources:
    data_source: "appsync.DataSource"
    service_role: "iam.Role"
    function: "Function | None" = None


class AppSyncDataSource:
    """A data source registered with an AppSync API.

    Created by AppSync builder methods (data_source_lambda, data_source_dynamo, etc.).
    Pass to resolver methods (query, mutation, etc.) to wire resolvers to data sources.
    """

    def __init__(
        self,
        name: str,
        ds_type: str,
        *,
        config: dict[str, Any] | None = None,
        function_config: "FunctionConfig | None" = None,
        customize: "AppSyncDataSourceCustomizationDict | None" = None,
    ) -> None:
        self._name = name
        self._ds_type = ds_type
        self._config = config or {}
        self._function_config = function_config
        self._customize = customize or {}
        self._resources: AppSyncDataSourceResources | None = None
        self._api_name: str | None = None

    @property
    def name(self) -> str:
        return self._name

    @property
    def ds_type(self) -> str:
        return self._ds_type

    @property
    def config(self) -> dict[str, Any]:
        return self._config

    @property
    def function_config(self) -> "FunctionConfig | None":
        return self._function_config

    @property
    def customize(self) -> "AppSyncDataSourceCustomizationDict":
        return self._customize

    @property
    def resources(self) -> AppSyncDataSourceResources:
        if self._resources is None:
            raise RuntimeError(
                f"Data source '{self._name}' resources have not been created yet. "
                "Access the AppSync component's .resources first."
            )
        return self._resources

    def _set_resources(self, resources: AppSyncDataSourceResources) -> None:
        self._resources = resources
