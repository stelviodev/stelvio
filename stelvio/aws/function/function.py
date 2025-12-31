import asyncio
import json
import logging
import os
import runpy
import sys
import time
import uuid
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import ClassVar, Unpack, final

import pulumi
from awslambdaric.lambda_context import LambdaContext
from pulumi import Input, Output, ResourceOptions
from pulumi_aws import lambda_
from pulumi_aws.iam import GetPolicyDocumentStatementArgs, Policy, Role
from pulumi_aws.lambda_ import FunctionUrl, FunctionUrlCorsArgs

from stelvio import context
from stelvio.aws.function.config import FunctionConfig, FunctionConfigDict, FunctionUrlConfig
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
from stelvio.aws.function.packaging import _create_lambda_archive
from stelvio.aws.function.resources_codegen import (
    _create_stlv_resource_file,
    create_stlv_resource_file_content,
)
from stelvio.aws.permission import AwsPermission
from stelvio.bridge.local.dtos import BridgeInvocationResult
from stelvio.bridge.local.handlers import WebsocketHandlers
from stelvio.bridge.remote.infrastructure import (
    _create_lambda_bridge_archive,
    discover_or_create_appsync,
)
from stelvio.component import BridgeableComponent, Component, safe_name
from stelvio.link import Link, Linkable
from stelvio.project import get_project_root

logger = logging.getLogger("stelvio.aws.function")


@final
@dataclass(frozen=True)
class FunctionResources:
    function: lambda_.Function
    role: Role
    policy: Policy | None
    function_url: FunctionUrl | None = None


@final
class Function(Component[FunctionResources], BridgeableComponent):
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
        self._dev_endpoint_id = f"{self.name}-{sha256(uuid.uuid4().bytes).hexdigest()[:8]}"

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

    def _normalize_url_config(
        self, url_value: str | FunctionUrlConfig | dict
    ) -> FunctionUrlConfig:
        """Normalize url configuration to FunctionUrlConfig.

        Converts shortcuts:
        - 'public' → FunctionUrlConfig(auth=None, cors=True)
        - 'private' → FunctionUrlConfig(auth='iam', cors=None)
        """
        if isinstance(url_value, str):
            if url_value == "public":
                return FunctionUrlConfig(auth=None, cors=True)
            if url_value == "private":
                return FunctionUrlConfig(auth="iam", cors=None)
            raise ValueError(f"Invalid url shortcut: {url_value}")
        if isinstance(url_value, FunctionUrlConfig):
            return url_value
        if isinstance(url_value, dict):
            return FunctionUrlConfig(**url_value)
        raise TypeError(f"Invalid url type: {type(url_value).__name__}")

    @property
    def config(self) -> FunctionConfig:
        return self._config

    @property
    def invoke_arn(self) -> Output[str]:
        return self.resources.function.invoke_arn

    @property
    def function_name(self) -> Output[str]:
        return self.resources.function.name

    @property
    def url(self) -> Output[str | None]:
        """Function URL endpoint if configured, None otherwise."""
        if self.resources.function_url is None:
            return Output.from_input(None)
        return self.resources.function_url.function_url

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

        # Determine effective runtime and architecture for the function
        function_runtime = self.config.runtime or DEFAULT_RUNTIME
        function_architecture = self.config.architecture or DEFAULT_ARCHITECTURE

        # Merge environment variables (user config.environment takes precedence)
        env_vars = {
            **_extract_links_env_vars(self._config.links),
            **FunctionEnvVarsRegistry.get_env_vars(self),
            **self.config.environment,
        }

        if context().dev_mode:
            appsync_bridge = discover_or_create_appsync(
                region=context().aws.region, profile=context().aws.profile
            )

            WebsocketHandlers.register(self)
            env_vars["STLV_APPSYNC_REALTIME"] = appsync_bridge.realtime_endpoint
            env_vars["STLV_APPSYNC_HTTP"] = appsync_bridge.http_endpoint
            env_vars["STLV_APPSYNC_API_KEY"] = appsync_bridge.api_key
            env_vars["STLV_APP_NAME"] = context().name
            env_vars["STLV_STAGE"] = context().env
            env_vars["STLV_FUNCTION_NAME"] = self.name
            env_vars["STLV_DEV_ENDPOINT_ID"] = self._dev_endpoint_id
            function_resource = lambda_.Function(
                safe_name(context().prefix(), self.name, 64),
                role=lambda_role.arn,
                architectures=[function_architecture],
                runtime=function_runtime,
                code=_create_lambda_bridge_archive(),
                handler="stlv_function_stub.handler",
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
                code=_create_lambda_archive(self.config, lambda_resource_file_content),
                handler=self.config.handler_format,
                environment={"variables": env_vars},
                memory_size=self.config.memory or DEFAULT_MEMORY,
                timeout=self.config.timeout or DEFAULT_TIMEOUT,
                layers=[layer.arn for layer in self.config.layers] if self.config.layers else None,
                # Technically this is necessary only for tests as otherwise it's ok if role
                # attachments are created after functions
                opts=ResourceOptions(depends_on=role_attachments),
            )
        pulumi.export(f"function_{self.name}_arn", function_resource.arn)
        pulumi.export(f"function_{self.name}_name", function_resource.name)
        pulumi.export(f"function_{self.name}_role_arn", lambda_role.arn)
        pulumi.export(f"function_{self.name}_role_name", lambda_role.name)

        # Create IDE resource file after successful function creation
        _create_stlv_resource_file(get_project_root() / folder_path, ide_resource_file_content)

        # Create function URL if configured
        function_url = None
        if self.config.url is not None:
            url_config = self._normalize_url_config(self.config.url)
            function_url = _create_function_url(self.name, function_resource, url_config)

        return FunctionResources(function_resource, lambda_role, function_policy, function_url)

    async def _handle_bridge_event(self, data: dict) -> BridgeInvocationResult | None:
        project_root = get_project_root()
        handler_file = self.config.full_handler_python_path
        handler_file_path = project_root / handler_file
        handler_function_name = self.config.handler_function_name

        new_environ = await self._get_environment_for_bridge_event()

        with temporary_environment(new_environ, [handler_file_path.parent]):
            try:
                module = runpy.run_path(str(handler_file_path))
            except FileNotFoundError:
                logger.exception(
                    "Function handler file not found: %s (expected at %s)",
                    handler_file,
                    handler_file_path,
                )
                return None
            function = module.get(handler_function_name)
            if function:
                event = data.get("event", "null")
                event = json.loads(event) if isinstance(event, str) else event
                lambda_context = LambdaContext(**event["context"])

                start_time = time.perf_counter()
                success = None
                error = None
                try:
                    success = await asyncio.get_event_loop().run_in_executor(
                        None, function, event.get("event", {}), lambda_context
                    )
                except Exception as e:
                    error = e
                end_time = time.perf_counter()
                run_time = end_time - start_time
            else:
                return None

        path = event.get("event", {}).get("path")
        raw_path = event.get("event", {}).get("rawPath")
        display_path = path or raw_path or "N/A"

        method = event.get("event", {}).get("httpMethod")
        context_method = (
            event.get("event", {}).get("requestContext", {}).get("http", {}).get("method")
        )
        display_method = method or context_method or "N/A"

        return BridgeInvocationResult(
            success_result=success,
            error_result=error,
            process_time_local=float(run_time * 1000),
            request_path=display_path,
            request_method=display_method,
            status_code=success.get("statusCode", -1) if success else -1,
            handler_name=f"{handler_file}:{handler_function_name}",
        )

    async def _get_environment_for_bridge_event(self) -> dict[str, str]:
        new_environ = {}
        # Inject AWS context into environment for boto3
        if context().aws.region:
            new_environ["AWS_REGION"] = context().aws.region
            new_environ["AWS_DEFAULT_REGION"] = context().aws.region

        if context().aws.profile:
            new_environ["AWS_PROFILE"] = context().aws.profile
        # Inject environment variables from links and config
        env_vars = {
            **_extract_links_env_vars(self._config.links),
            **FunctionEnvVarsRegistry.get_env_vars(self),
            **self.config.environment,
        }
        futures = []
        loop = asyncio.get_running_loop()

        for key, value in env_vars.items():
            if isinstance(value, str):
                new_environ[key] = value
            elif isinstance(value, pulumi.Output):
                future = loop.create_future()
                futures.append(future)

                def set_env_var(v: str, key: str = key, future: asyncio.Future = future) -> None:
                    try:
                        new_environ[key] = str(v)
                        loop.call_soon_threadsafe(future.set_result, None)
                    except Exception as e:
                        loop.call_soon_threadsafe(future.set_exception, e)

                value.apply(set_env_var)
        if futures:
            await asyncio.gather(*futures)
        return new_environ


class LinkPropertiesRegistry:
    _folder_links_properties_map: ClassVar[dict[str, dict[str, list[str]]]] = {}

    @classmethod
    def add(cls, folder: str, link_properties_map: dict[str, list[str]]) -> None:
        cls._folder_links_properties_map.setdefault(folder, {}).update(link_properties_map)

    @classmethod
    def get_link_properties_map(cls, folder: str) -> dict[str, list[str]]:
        return cls._folder_links_properties_map.get(folder, {})


class FunctionEnvVarsRegistry:
    _functions_env_vars_map: ClassVar[dict[Function, dict[str, str]]] = {}

    @classmethod
    def add(cls, function_: Function, env_vars: dict[str, str]) -> None:
        cls._functions_env_vars_map.setdefault(function_, {}).update(env_vars)

    @classmethod
    def get_env_vars(cls, function_: Function) -> dict[str, str]:
        return cls._functions_env_vars_map.get(function_, {}).copy()


def _create_function_url(
    name: str, function: lambda_.Function, url_config: FunctionUrlConfig
) -> FunctionUrl:
    """Create a Function URL with the given configuration.

    For standalone Functions, auth='default' is normalized to None (public access).
    """
    # Normalize auth: 'default' → None for Function, 'iam' → 'AWS_IAM'
    auth_type = "AWS_IAM" if url_config.auth == "iam" else url_config.auth
    if auth_type == "default":
        auth_type = None

    # Build CORS configuration if enabled
    cors_config = None
    normalized_cors = url_config.normalized_cors
    if normalized_cors is not None:
        # Convert string to list for AWS compatibility
        def to_list(value: str | list[str]) -> list[str]:
            return [value] if isinstance(value, str) else value

        cors_config = FunctionUrlCorsArgs(
            allow_origins=to_list(normalized_cors.allow_origins),
            allow_methods=to_list(normalized_cors.allow_methods),
            allow_headers=to_list(normalized_cors.allow_headers),
            allow_credentials=normalized_cors.allow_credentials,
            max_age=normalized_cors.max_age,
            expose_headers=normalized_cors.expose_headers,
        )

    # Determine invoke mode based on streaming
    invoke_mode = "RESPONSE_STREAM" if url_config.streaming else "BUFFERED"

    function_url = FunctionUrl(
        safe_name(context().prefix(), name, 64, suffix="-url"),
        function_name=function.name,
        authorization_type=auth_type or "NONE",
        cors=cors_config,
        invoke_mode=invoke_mode,
    )

    pulumi.export(f"function_{name}_url", function_url.function_url)

    return function_url


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


@contextmanager
def temporary_environment(
    new_environ: dict[str, str], add_paths: list[str]
) -> Generator[None, None, None]:
    """Context manager to temporarily set environment variables and sys.path."""
    original_environ = os.environ.copy()
    original_path = sys.path.copy()
    try:
        os.environ.update(new_environ)
        for path in add_paths:
            if path not in sys.path:
                sys.path.insert(0, str(path))
        yield
    finally:
        os.environ.clear()
        os.environ.update(original_environ)
        sys.path[:] = original_path
