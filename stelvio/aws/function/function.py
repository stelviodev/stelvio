import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import ClassVar, Unpack, final

import pulumi
from awslambdaric.lambda_context import LambdaContext
from pulumi import Asset, Input, Output, ResourceOptions
from pulumi_aws import lambda_
from pulumi_aws.iam import GetPolicyDocumentStatementArgs, Policy, Role

from stelvio import context
from stelvio.aws.function.config import FunctionConfig, FunctionConfigDict
from stelvio.aws.function.constants import (
    DEFAULT_ARCHITECTURE,
    DEFAULT_MEMORY,
    DEFAULT_RUNTIME,
    DEFAULT_TIMEOUT,
)
from stelvio.aws.function.iam import (
    _attach_role_policies,
    _create_function_policy,
    _create_lambda_role,
)
from stelvio.aws.function.naming import _envar_name
from stelvio.aws.function.packaging import _create_lambda_archive, _create_lambda_tunnel_archive
from stelvio.aws.function.resources_codegen import (
    _create_stlv_resource_file,
    create_stlv_resource_file_content,
)
from stelvio.aws.permission import AwsPermission
from stelvio.component import TunnelableComponent, safe_name
from stelvio.link import Link, Linkable
from stelvio.project import get_project_root
from stelvio.tunnel.ws import TunnelLogger, WebsocketClient, WebsocketHandlers

logger = logging.getLogger("stelvio.aws.function")


@final
@dataclass(frozen=True)
class FunctionResources:
    function: lambda_.Function
    role: Role
    policy: Policy | None


@final
class Function(TunnelableComponent[FunctionResources]):
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
    _dev_endpoint_id: str | None = None

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

    # Tunnel: Step 2a: Handle incoming tunnel events for Lambda function
    async def _handle_tunnel_event(
        self, data: dict, websocket_client: WebsocketClient, logger: TunnelLogger
    ) -> None:
        project_root = get_project_root()
        from importlib import util

        handler_ = self._todo_handler

        module_path, func_name = handler_.rsplit(".", 1)
        module_file_path = project_root / f"{module_path}.py"

        spec = util.spec_from_file_location(str(module_file_path), str(module_file_path))
        module = util.module_from_spec(spec)
        spec.loader.exec_module(module)
        handler_real = getattr(module, func_name)

        # Tunnel: Step 2b: Reconstruct event and context for Lambda handler
        event = data["payload"]["event"]
        context = LambdaContext(**data["payload"]["context"])
        handler_start = perf_counter()
        # Tunnel: Step 2c: Invoke the actual Lambda handler locally
        payload = handler_real(event, context)
        handler_duration_ms = (perf_counter() - handler_start) * 1000
        # logger.debug("Lambda handler %s executed in %.2f ms", handler_, handler_duration_ms)
        # TODO: Remove debug code
        # import json
        # payload["body"] = json.loads(payload["body"])
        # payload["body"]["module_path"] = module_path
        # payload["body"]["func_name"] = func_name
        # payload["body"]["handler_"] = handler_
        # payload["body"]["keys"] = [str(k) for k in data.get("payload", {}).keys()]
        # payload["body"]["event"] = data["payload"]["event"]  # TODO
        # payload["body"]["context"] = data["payload"]["context"]  # TODO
        # payload["body"]["context_str"] = data["payload"]["context_str"]  # TODO
        # payload["body"] = json.dumps(payload["body"])

        response_message = {
            "payload": payload,
            "requestId": data.get("requestId"),
            "type": "request-processed",
        }
        # Tunnel: Step 3: Send back the processed response to the tunnel service
        await websocket_client.send_json(response_message)
        logger.log(
            data["payload"]["event"]["requestContext"]["protocol"],
            data["payload"]["event"]["httpMethod"],
            data["payload"]["event"]["requestContext"]["path"],
            data["payload"]["event"]["requestContext"]["identity"]["sourceIp"],
            response_message["payload"]["statusCode"],
            handler_duration_ms,
        )

    def _create_resources(self) -> FunctionResources:
        logger.debug("Creating resources for function '%s'", self.name)
        iam_statements = _extract_links_permissions(self._config.links)
        function_policy = _create_function_policy(self.name, iam_statements)

        lambda_role = _create_lambda_role(self.name)
        role_attachments = _attach_role_policies(self.name, lambda_role, function_policy)

        folder_path = self.config.folder_path or str(Path(self.config.handler_file_path).parent)

        links_props = _extract_links_property_mappings(self._config.links)
        # Check if CORS env vars are present
        cors_env_vars = FunctionEnvVarsRegistry.get_env_vars(self)
        has_cors = "STLV_CORS_ALLOW_ORIGIN" in cors_env_vars

        lambda_resource_file_content = create_stlv_resource_file_content(links_props, has_cors)
        LinkPropertiesRegistry.add(folder_path, links_props)

        ide_resource_file_content = create_stlv_resource_file_content(
            LinkPropertiesRegistry.get_link_properties_map(folder_path), has_cors
        )

        extra_assets_map = FunctionAssetsRegistry.get_assets_map(self)
        handler = self.config.handler_format
        if "stlv_routing_handler.py" in extra_assets_map:
            handler = "stlv_routing_handler.lambda_handler"

        self._todo_handler = folder_path + "/" + handler

        # Determine effective runtime and architecture for the function
        function_runtime = self.config.runtime or DEFAULT_RUNTIME
        function_architecture = self.config.architecture or DEFAULT_ARCHITECTURE

        # Merge environment variables (user config.environment takes precedence)
        env_vars = {
            **_extract_links_env_vars(self._config.links),
            **FunctionEnvVarsRegistry.get_env_vars(self),
            **self.config.environment,
        }

        if context().tunnel_mode:
            channel_id = "channel"
            endpoint_id = uuid.uuid4().hex
            self._dev_endpoint_id = endpoint_id

            WebsocketHandlers.register(self.handle_tunnel_event)

            function_resource = lambda_.Function(
                safe_name(context().prefix(), self.name, 64),
                role=lambda_role.arn,
                architectures=[function_architecture],
                runtime=function_runtime,
                code=_create_lambda_tunnel_archive(channel_id, self._dev_endpoint_id),
                handler="replacement.handler",
                environment={"variables": env_vars},
                memory_size=self.config.memory or DEFAULT_MEMORY,
                timeout=self.config.timeout or DEFAULT_TIMEOUT,
                layers=[layer.arn for layer in self.config.layers] if self.config.layers else None,
                # Technically this is necessary only for tests as otherwise
                # it's ok if role attachments are created after functions
                opts=ResourceOptions(depends_on=role_attachments),
            )
        else:
            function_resource = lambda_.Function(
                safe_name(context().prefix(), self.name, 64),
                role=lambda_role.arn,
                architectures=[function_architecture],
                runtime=function_runtime,
                code=_create_lambda_archive(
                    self.config, lambda_resource_file_content, extra_assets_map
                ),
                handler=handler,
                environment={"variables": env_vars},
                memory_size=self.config.memory or DEFAULT_MEMORY,
                timeout=self.config.timeout or DEFAULT_TIMEOUT,
                layers=[layer.arn for layer in self.config.layers] if self.config.layers else None,
                # Technically this is necessary only for tests as otherwise
                # it's ok if role attachments are created after functions
                opts=ResourceOptions(depends_on=role_attachments),
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


class FunctionEnvVarsRegistry:
    _functions_env_vars_map: ClassVar[dict[Function, dict[str, str]]] = {}

    @classmethod
    def add(cls, function_: Function, env_vars: dict[str, str]) -> None:
        cls._functions_env_vars_map.setdefault(function_, {}).update(env_vars)

    @classmethod
    def get_env_vars(cls, function_: Function) -> dict[str, str]:
        return cls._functions_env_vars_map.get(function_, {}).copy()


def _extract_links_permissions(
    linkables: Sequence[Link | Linkable],
) -> Sequence[GetPolicyDocumentStatementArgs]:
    """Extracts IAM statements from permissions for function's IAM policy"""
    permissions = [
        p
        for linkable in linkables
        if linkable.link().permissions
        for p in linkable.link().permissions
    ]
    for p in permissions:
        if not isinstance(p, AwsPermission):
            raise TypeError(
                f"AWS Function requires AwsPermission, got {type(p).__name__}. "
                f"Cannot use permissions from other cloud providers with AWS Lambda."
            )
    return [permission.to_provider_format() for permission in permissions]


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
