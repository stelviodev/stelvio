import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Unpack, final

import pulumi
from pulumi import Asset, Input, Output
from pulumi_aws import lambda_
from pulumi_aws.iam import Policy, Role

from stelvio import context
from stelvio.component import Component
from stelvio.link import Link, Linkable
from stelvio.project import get_project_root

from .config import FunctionConfig, FunctionConfigDict  # Import for re-export
from .constants import DEFAULT_ARCHITECTURE, DEFAULT_MEMORY, DEFAULT_RUNTIME, DEFAULT_TIMEOUT
from .iam import _attach_role_policies, _create_function_policy, _create_lambda_role
from .naming import _envar_name
from .packaging import _create_lambda_archive
from .resources_codegen import _create_stlv_resource_file, create_stlv_resource_file_content

logger = logging.getLogger(__name__)


__all__ = ["Function", "FunctionConfig", "FunctionConfigDict", "FunctionResources"]


@dataclass(frozen=True)
class FunctionResources:
    function: lambda_.Function
    role: Role
    policy: Policy | None


@final
class Function(Component[FunctionResources]):
    """AWS Lambda function component with automatic resource discovery.

    Generated environment variables follow pattern: STLV_RESOURCENAME_PROPERTYNAME

    Args:
        name: Function name
        config: Complete function configuration as FunctionConfig or dict
        **opts: Individual function configuration parameters

    You can configure the function in two ways:
        - Provide complete config:
            function = Function(
                name="process-user",
                config={"handler": "functions/orders.index", "timeout": 30}
            )
        - Provide individual parameters:
            function = Function(
                name="process-user",
                handler="functions/orders.index",
                links=[table.default_link(), bucket.readonly_link()]
            )

    """

    _config: FunctionConfig

    def __init__(
        self,
        name: str,
        config: None | FunctionConfig | FunctionConfigDict = None,
        **opts: Unpack[FunctionConfigDict],
    ):
        super().__init__(name)

        self._config = self._parse_config(config, opts)

    @staticmethod
    def _parse_config(
        config: None | FunctionConfig | FunctionConfigDict, opts: FunctionConfigDict
    ) -> FunctionConfig:
        if not config and not opts:
            raise ValueError(
                "Missing function handler: must provide either a complete configuration via "
                "'config' parameter or at least the 'handler' option"
            )
        if config and opts:
            raise ValueError(
                "Invalid configuration: cannot combine 'config' parameter with additional options "
                "- provide all settings either in 'config' or as separate options"
            )
        if config is None:
            return FunctionConfig(**opts)
        if isinstance(config, FunctionConfig):
            return config
        if isinstance(config, dict):
            return FunctionConfig(**config)

        raise TypeError(
            f"Invalid config type: expected FunctionConfig or dict, got {type(config).__name__}"
        )

    @property
    def config(self) -> FunctionConfig:
        return self._config

    @property
    def invoke_arn(self) -> Output[str]:
        return self.resources.function.invoke_arn

    @property
    def function_name(self) -> Output[str]:
        return self.resources.function.name

    def _create_resources(self) -> FunctionResources:
        logger.debug("Creating resources for function '%s'", self.name)
        iam_statements = _extract_links_permissions(self._config.links)
        function_policy = _create_function_policy(self.name, iam_statements)

        lambda_role = _create_lambda_role(self.name)
        _attach_role_policies(self.name, lambda_role, function_policy)

        folder_path = self.config.folder_path or str(Path(self.config.handler_file_path).parent)

        links_props = _extract_links_property_mappings(self._config.links)
        lambda_resource_file_content = create_stlv_resource_file_content(links_props)
        LinkPropertiesRegistry.add(folder_path, links_props)

        ide_resource_file_content = create_stlv_resource_file_content(
            LinkPropertiesRegistry.get_link_properties_map(folder_path)
        )

        extra_assets_map = FunctionAssetsRegistry.get_assets_map(self)
        handler = self.config.handler_format
        if "stlv_routing_handler.py" in extra_assets_map:
            handler = "stlv_routing_handler.lambda_handler"

        # Determine effective runtime and architecture for the function
        function_runtime = self.config.runtime or DEFAULT_RUNTIME
        function_architecture = self.config.architecture or DEFAULT_ARCHITECTURE
        function_resource = lambda_.Function(
            context().prefix(self.name),
            role=lambda_role.arn,
            architectures=[function_architecture],
            runtime=function_runtime,
            code=_create_lambda_archive(
                self.config, lambda_resource_file_content, extra_assets_map
            ),
            handler=handler,
            environment={"variables": _extract_links_env_vars(self._config.links)},
            memory_size=self.config.memory or DEFAULT_MEMORY,
            timeout=self.config.timeout or DEFAULT_TIMEOUT,
            layers=[layer.arn for layer in self.config.layers] if self.config.layers else None,
        )
        pulumi.export(f"function_{self.name}_arn", function_resource.arn)
        pulumi.export(f"function_{self.name}_name", function_resource.name)
        pulumi.export(f"function_{self.name}_role_arn", lambda_role.arn)
        pulumi.export(f"function_{self.name}_role_name", lambda_role.name)

        # Create IDE resource file after successful function creation
        _create_stlv_resource_file(get_project_root() / folder_path, ide_resource_file_content)

        return FunctionResources(function_resource, lambda_role, function_policy)


class LinkPropertiesRegistry:
    _folder_links_properties_map: ClassVar[dict[str, dict[str, list[str]]]] = {}

    @classmethod
    def add(cls, folder: str, link_properties_map: dict[str, list[str]]) -> None:
        cls._folder_links_properties_map.setdefault(folder, {}).update(link_properties_map)

    @classmethod
    def get_link_properties_map(cls, folder: str) -> dict[str, list[str]]:
        return cls._folder_links_properties_map.get(folder, {})


class FunctionAssetsRegistry:
    _functions_assets_map: ClassVar[dict[Function, dict[str, Asset]]] = {}

    @classmethod
    def add(cls, function_: Function, assets_map: dict[str, Asset]) -> None:
        cls._functions_assets_map.setdefault(function_, {}).update(assets_map)

    @classmethod
    def get_assets_map(cls, function_: Function) -> dict[str, Asset]:
        return cls._functions_assets_map.get(function_, {}).copy()


def _extract_links_permissions(linkables: Sequence[Link | Linkable]) -> list[Mapping | Iterable]:
    """Extracts IAM statements from permissions for function's IAM policy"""
    return [
        permission.to_provider_format()
        for linkable in linkables
        if linkable.link().permissions
        for permission in linkable.link().permissions
    ]


def _extract_links_env_vars(linkables: Sequence[Link | Linkable]) -> dict[str, Input[str]]:
    """Creates environment variables with STLV_ prefix for runtime resource discovery.
    The STLV_ prefix in environment variables ensures no conflicts with other env vars
    and makes it clear which variables are managed by Stelvio.
    """
    link_objects = [item.link() for item in linkables]
    return {
        _envar_name(link.name, prop_name): value
        for link in link_objects
        if link.properties
        for prop_name, value in link.properties.items()
    }


def _extract_links_property_mappings(linkables: Sequence[Link | Linkable]) -> dict[str, list[str]]:
    """Maps resource properties to Python class names for code generation of resource
    access classes.
    """
    link_objects = [item.link() for item in linkables]
    return {link.name: list(link.properties) for link in link_objects}
