from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import logging
import os
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from functools import cached_property
from hashlib import sha256
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, ClassVar, TypedDict, Unpack, final

import pulumi
from pulumi import FileAsset, Input, Output, ResourceOptions
from pulumi_aws import lambda_
from pulumi_aws.iam import (
    GetPolicyDocumentStatementArgs,
    Policy,
    Role,
    get_policy_document,
)
from pulumi_aws.lambda_ import FunctionUrl, FunctionUrlCorsArgs

from stelvio import context
from stelvio.aws.function.config import FunctionConfig, FunctionConfigDict, FunctionUrlConfig
from stelvio.aws.function.constants import (
    DEFAULT_ARCHITECTURE_DEVMODE,
    DEFAULT_MEMORY,
    DEFAULT_TIMEOUT,
)
from stelvio.aws.function.iam import _attach_role_policies, _create_lambda_role
from stelvio.aws.function.naming import _envar_name
from stelvio.aws.function.packaging import (
    _create_lambda_archive,
    _link_file_sources,
)
from stelvio.aws.function.resources_codegen import (
    _create_stlv_resource_file,
    create_stlv_resource_file_content,
)
from stelvio.aws.permission import AwsPermission
from stelvio.aws.types import DEFAULT_ARCHITECTURE, DEFAULT_RUNTIME
from stelvio.aws.vpc import VpcAttachment, normalize_vpc_attachment
from stelvio.bridge.local.dtos import BridgeInvocationResult
from stelvio.bridge.local.handlers import WebsocketHandlers
from stelvio.bridge.local.lambda_context import LambdaContext
from stelvio.bridge.remote.infrastructure import (
    _create_lambda_bridge_archive,
    discover_or_create_appsync,
)
from stelvio.component import (
    BridgeableMixin,
    Component,
    link_config_creator,
    parse_config,
    resource_name,
)
from stelvio.link import Link, Linkable, LinkableMixin, LinkConfig
from stelvio.project import get_project_root, get_stelvio_lib_root
from stelvio.provider import ProviderStore, aws_region_of

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Sequence
    from types import CodeType

    from pulumi_aws.iam import PolicyArgs, RoleArgs
    from pulumi_aws.lambda_ import FunctionArgs, FunctionUrlArgs

    from stelvio.customize import Customization

logger = logging.getLogger("stelvio.aws.function")


@final
@dataclass(frozen=True)
class FunctionResources:
    function: lambda_.Function
    role: Role
    policy: Policy | None
    function_url: FunctionUrl | None = None


class FunctionCustomizationDict(TypedDict, total=False):
    function: Customization[FunctionArgs]
    role: Customization[RoleArgs]
    policy: Customization[PolicyArgs]
    function_url: Customization[FunctionUrlArgs]


@final
class Function(
    Component[FunctionResources, FunctionCustomizationDict], BridgeableMixin, LinkableMixin
):
    """AWS Lambda function component with automatic resource discovery.

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
                links=[table, bucket]
            )

    """

    _config: FunctionConfig

    def __init__(
        self,
        name: str,
        config: None | FunctionConfig | FunctionConfigDict = None,
        *,
        tags: dict[str, str] | None = None,
        customize: FunctionCustomizationDict | None = None,
        parent: pulumi.Resource | None = None,
        **opts: Unpack[FunctionConfigDict],
    ):
        """Create a Lambda function.

        Args:
            name: Unique component name.
            config: Function configuration (handler, memory, timeout, links, etc.).
            tags: AWS tags for this function's resources.
            customize: Per-resource overrides for function, role, policy, or function_url.
            parent: Parent resource for nesting. Used by components like Cron and
                Api that create Functions internally.
            **opts: Inline function config options (alternative to ``config``).
        """
        super().__init__(
            ProviderStore.aws(),
            "stelvio:aws:Function",
            name,
            tags=tags,
            customize=customize,
            parent=parent,
        )

        if not config and not opts:
            raise ValueError(
                "Missing function handler: must provide either a complete configuration via "
                "'config' parameter or at least the 'handler' option"
            )
        self._config = parse_config(FunctionConfig, config, opts)
        self._dev_endpoint_id = f"{self.name}-{sha256(uuid.uuid4().bytes).hexdigest()[:8]}"

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

    def _create_function_policy(
        self, name: str, statements: Sequence[GetPolicyDocumentStatementArgs]
    ) -> Policy | None:
        """Create IAM policy for Lambda if there are any statements."""
        if not statements:
            return None

        policy_document = get_policy_document(statements=statements)

        return Policy(
            resource_name(name, limit=128, suffix="-p"),
            **self._customizer(
                "policy",
                {},
                {"path": "/", "policy": policy_document.json},
                inject_tags=True,
            ),
            opts=self._resource_opts(),
        )

    def _create_resources(self) -> FunctionResources:
        logger.debug("Creating resources for function '%s'", self.name)
        iam_statements = _extract_links_permissions(self._config.links)
        function_policy = self._create_function_policy(self.name, iam_statements)

        lambda_role = _create_lambda_role(
            self.name,
            customizer=lambda resource_key, props: self._customizer(
                resource_key, {}, props, inject_tags=True
            ),
            opts=self._resource_opts(),
        )
        vpc_attachment = normalize_vpc_attachment(self.config.vpc)
        role_attachments = _attach_role_policies(
            self.name,
            lambda_role,
            function_policy,
            opts=self._resource_opts(),
            vpc_access=vpc_attachment is not None,
        )
        # Resolved before the dev-mode split so the Vpc's app security group stays
        # registered in dev mode too: the stub runs outside the VPC (it must reach the
        # bridge), but dropping the group would block on Lambda's slow ENI cleanup.
        vpc_config = _vpc_config(vpc_attachment) if vpc_attachment else None

        # str(Path(...)) drops a trailing or doubled slash: `functions/folder/` and
        # `functions/folder` are one folder, so one registry key and one IDE file.
        folder = self.config.folder_path or Path(self.config.handler_file_path).parent
        folder_path = str(Path(folder))

        links_props = _extract_links_property_mappings(self._config.links)
        # Check if CORS env vars are present
        cors_env_vars = FunctionEnvVarsRegistry.get_env_vars(self)
        has_cors = "STLV_CORS_ALLOW_ORIGIN" in cors_env_vars

        lambda_resource_file_content = create_stlv_resource_file_content(links_props, has_cors)
        LinkPropertiesRegistry.add(folder_path, links_props, has_cors)

        ide_resource_file_content = create_stlv_resource_file_content(
            LinkPropertiesRegistry.get_link_properties_map(folder_path),
            LinkPropertiesRegistry.has_cors(folder_path),
        )

        # Merge environment variables (user config.environment takes precedence)
        env_vars = {
            **_extract_links_env_vars(self._config.links),
            **FunctionEnvVarsRegistry.get_env_vars(self),
            **self.config.environment,
        }

        link_file_map = _link_file_sources(self.name, self._config.links)
        if context().dev_mode:
            # The bridge is shared app-level dev infra: ONE AppSync in the app's
            # default region; stubs in any region reach it over plain HTTPS/WSS.
            # Linked files are validated above but only packaged for normal deploys.
            # Link creators must supply absolute source paths to dev handlers.
            appsync_bridge = discover_or_create_appsync(
                region=ProviderStore.region(), profile=context().aws.profile
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
                resource_name(self.name, limit=64),
                role=lambda_role.arn,
                architectures=[DEFAULT_ARCHITECTURE_DEVMODE],
                runtime=DEFAULT_RUNTIME,
                # Stub deps must match the stub's own arch, not the user's function config.
                code=_create_lambda_bridge_archive(DEFAULT_RUNTIME, DEFAULT_ARCHITECTURE_DEVMODE),
                handler="stlv_function_stub.handler",
                environment={"variables": env_vars},
                memory_size=DEFAULT_MEMORY,
                timeout=self.config.timeout or DEFAULT_TIMEOUT,
                layers=[layer.arn for layer in self.config.layers] if self.config.layers else None,
                tags=self.tags or None,
                # Technically this is necessary only for tests as otherwise
                # it's ok if role attachments are created after functions
                opts=self._resource_opts(depends_on=role_attachments),
            )
        else:
            function_resource = lambda_.Function(
                resource_name(self.name, limit=64),
                **self._customizer(
                    "function",
                    {
                        "architectures": [self.config.architecture]
                        if self.config.architecture
                        else None,
                        "runtime": self.config.runtime,
                        "code": _create_lambda_archive(
                            self.config,
                            lambda_resource_file_content,
                            extra_assets={d: FileAsset(str(p)) for d, p in link_file_map.items()},
                        ),
                        "handler": self.config.handler_format,
                        "environment": {"variables": env_vars},
                        "memory_size": self.config.memory,
                        "timeout": self.config.timeout,
                        "layers": [layer.arn for layer in self.config.layers]
                        if self.config.layers
                        else None,
                        "vpc_config": vpc_config,
                    },
                    default_props={
                        "role": lambda_role.arn,
                        "memory_size": DEFAULT_MEMORY,
                        "timeout": DEFAULT_TIMEOUT,
                        "architectures": [DEFAULT_ARCHITECTURE],
                        "runtime": DEFAULT_RUNTIME,
                    },
                    inject_tags=True,
                ),
                # Technically this is necessary only for tests as otherwise it's ok if role
                # attachments are created after functions
                opts=self._resource_opts(depends_on=role_attachments),
            )
        # Create IDE resource file after successful function creation
        _create_stlv_resource_file(get_project_root() / folder_path, ide_resource_file_content)

        # Create function URL if configured
        function_url = None
        if self.config.url is not None:
            url_config = _normalize_url_config(self.config.url)
            function_url = _create_function_url(
                self.name,
                function_resource,
                url_config,
                self._resource_opts(),
                customizer=self._customizer,
            )
            self.register_outputs({"url": function_url.function_url})

        return FunctionResources(function_resource, lambda_role, function_policy, function_url)

    @cached_property
    def _stlv_resources_code(self) -> CodeType | None:
        """This function's own generated stlv_resources, as its Lambda gets it; dev execs it
        into a fresh module per call instead of importing the folder's on-disk union.
        Read at bridge time only, after every `route()` has registered its CORS env vars."""
        content = create_stlv_resource_file_content(
            _extract_links_property_mappings(self._config.links),
            "STLV_CORS_ALLOW_ORIGIN" in FunctionEnvVarsRegistry.get_env_vars(self),
        )
        if content is None:
            return None
        # dont_inherit: this file's `from __future__ import annotations` must not leak in
        return compile(content, "<stlv_resources>", "exec", dont_inherit=True)

    async def _handle_bridge_event(self, data: dict) -> BridgeInvocationResult | None:
        project_root = get_project_root()
        handler_file = self.config.full_handler_python_path
        handler_function_name = self.config.handler_function_name

        event = data.get("event", "null")
        event = json.loads(event) if isinstance(event, str) else event
        lambda_context = LambdaContext(**event["context"])

        path = event.get("event", {}).get("path")
        raw_path = event.get("event", {}).get("rawPath")
        display_path = path or raw_path or "N/A"
        method = event.get("event", {}).get("httpMethod")
        context_method = (
            event.get("event", {}).get("requestContext", {}).get("http", {}).get("method")
        )
        display_method = method or context_method or "N/A"
        handler_name = f"{handler_file}:{handler_function_name}"

        def _error_result(exc: BaseException) -> BridgeInvocationResult:
            return BridgeInvocationResult(
                success_result=None,
                error_result=exc,
                process_time_local=0.0,
                request_path=display_path,
                request_method=display_method,
                status_code=-1,
                handler_name=handler_name,
            )

        new_environ = await self._get_environment_for_bridge_event()
        # Lambda puts the zip root on sys.path: the folder for folder mode, nothing a
        # single-file handler could import a sibling from
        folder = self.config.folder_path
        add_paths = [str(project_root / folder)] if folder else []

        with temporary_environment(new_environ, add_paths):
            # SystemExit too: a handler calling sys.exit() must not stop `stlv dev`
            try:
                _evict_project_modules(project_root)
                _install_stlv_resources(self._stlv_resources_code)
                module = _import_handler_module(self.config, project_root)
            except (Exception, SystemExit) as e:
                return _error_result(e)
            function = getattr(module, handler_function_name, None)
            if function is None:
                return _error_result(
                    AttributeError(
                        f"Handler function {handler_function_name!r} not found in "
                        f"{project_root / handler_file}"
                    )
                )

            start_time = time.perf_counter()
            success = None
            error = None
            try:
                success = await asyncio.get_running_loop().run_in_executor(
                    None, function, event.get("event", {}), lambda_context
                )
            except (Exception, SystemExit) as e:
                error = e
            end_time = time.perf_counter()
            run_time = end_time - start_time

        return BridgeInvocationResult(
            success_result=success,
            error_result=error,
            process_time_local=float(run_time * 1000),
            request_path=display_path,
            request_method=display_method,
            status_code=success.get("statusCode", -1) if success else -1,
            handler_name=handler_name,
        )

    async def _get_environment_for_bridge_event(self) -> dict[str, str]:
        new_environ = {}
        # Emulate the Lambda runtime: AWS_REGION is always the function's own region
        region = aws_region_of(self)
        new_environ["AWS_REGION"] = region
        new_environ["AWS_DEFAULT_REGION"] = region

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


@final
class LinkPropertiesRegistry:
    """What the IDE file of a handler folder shows: the union over every function built
    in that folder. Each Lambda's own copy only carries its own links and CORS."""

    _folder_links_properties_map: ClassVar[dict[str, dict[str, list[str]]]] = {}
    _cors_folders: ClassVar[set[str]] = set()

    @classmethod
    def add(cls, folder: str, link_properties_map: dict[str, list[str]], has_cors: bool) -> None:
        # Union per link, first-seen order: a sibling that trimmed a link's properties
        # (`remove_properties`) must not hide them from the folder's IDE file.
        folder_map = cls._folder_links_properties_map.setdefault(folder, {})
        for link_name, properties in link_properties_map.items():
            known = folder_map.setdefault(link_name, [])
            known.extend(p for p in properties if p not in known)
        if has_cors:
            cls._cors_folders.add(folder)

    @classmethod
    def get_link_properties_map(cls, folder: str) -> dict[str, list[str]]:
        return cls._folder_links_properties_map.get(folder, {})

    @classmethod
    def has_cors(cls, folder: str) -> bool:
        return folder in cls._cors_folders

    @classmethod
    def reset(cls) -> None:
        cls._folder_links_properties_map.clear()
        cls._cors_folders.clear()


class FunctionEnvVarsRegistry:
    _functions_env_vars_map: ClassVar[dict[Function, dict[str, str]]] = {}

    @classmethod
    def add(cls, function_: Function, env_vars: dict[str, str]) -> None:
        if not env_vars:  # nothing to add, so a built Function is fine here
            return
        current = cls._functions_env_vars_map.setdefault(function_, {})
        if current == env_vars:  # same settings again (another route or API): nothing to add
            return
        if current:
            raise ValueError(
                f"Conflicting environment variables for Function '{function_.name}': "
                f"{current} and {env_vars}. A Function routed from several REST APIs needs "
                "the same CORS settings on each."
            )
        function_._check_not_created("routes that use it")  # noqa: SLF001
        current.update(env_vars)

    @classmethod
    def get_env_vars(cls, function_: Function) -> dict[str, str]:
        return cls._functions_env_vars_map.get(function_, {}).copy()


def _normalize_url_config(url_value: str | FunctionUrlConfig | dict) -> FunctionUrlConfig:
    """Normalize url configuration to FunctionUrlConfig.

    Converts shortcuts:
    - 'public' → FunctionUrlConfig(auth=None, cors=True)
    - 'private' → FunctionUrlConfig(auth='iam', cors=None)
    """
    if url_value == "public":
        return FunctionUrlConfig(auth=None, cors=True)
    if url_value == "private":
        return FunctionUrlConfig(auth="iam", cors=None)
    # FunctionConfig._validate_url has already rejected any other str or type.
    return parse_config(FunctionUrlConfig, url_value, {})


def _create_function_url(
    name: str,
    function: lambda_.Function,
    url_config: FunctionUrlConfig,
    opts: ResourceOptions,
    customizer: Callable[[str, dict, dict], dict],
) -> FunctionUrl:
    """Create a Function URL with the given configuration.

    auth='default' and streaming=None are unset: their values come from `default_props`,
    so an app-wide customize dict can change them.
    `customizer` applies the owning component's `function_url` customization; Router has
    no such key and passes `_as_is`.
    """
    auth_type = {"iam": "AWS_IAM", None: "NONE"}.get(url_config.auth)
    invoke_mode = {True: "RESPONSE_STREAM", False: "BUFFERED"}.get(url_config.streaming)

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

    props = {
        "function_name": function.name,
        "authorization_type": auth_type,
        "cors": cors_config,
        "invoke_mode": invoke_mode,
    }
    default_props = {"authorization_type": "NONE", "invoke_mode": "BUFFERED"}
    props = customizer("function_url", props, default_props)
    return FunctionUrl(resource_name(name, limit=64, suffix="-url"), **props, opts=opts)


def _vpc_config(attachment: VpcAttachment) -> dict[str, Sequence[Input[str]]]:
    """Lambda `vpc_config`: every subnet of the chosen tier, plus the security groups."""
    vpc = attachment.vpc
    resources = vpc.resources
    subnets = (
        resources.private_subnets
        if attachment.subnets == "private"
        else resources.isolated_subnets
    )
    # in-library read of the Vpc's shared group; not user API, hence private
    security_group_ids = attachment.security_groups or [vpc._app_security_group.id]  # noqa: SLF001
    return {
        "subnet_ids": [subnet.id for subnet in subnets],
        "security_group_ids": security_group_ids,
    }


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
    # Like the env vars, only links with properties: a permissions-only Link may share a
    # linked component's name and must not replace that component's entry.
    return {link.name: list(link.properties) for link in link_objects if link.properties}


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
                sys.path.insert(0, path)
        yield
    finally:
        os.environ.clear()
        os.environ.update(original_environ)
        sys.path[:] = original_path


def _evict_project_modules(project_root: Path) -> None:
    """One `stlv dev` interpreter serves every function, and Python caches modules for its
    life: a cached user module would answer for the wrong folder and hide edits. A venv
    inside the project stays (installed packages: numpy cannot load twice; `.venv/bin/stlv`
    is `__main__`), so does a Stelvio checkout; one above the project (`/usr` over
    `/usr/src/app`) would shield the whole project, so it doesn't count. Writes no `.pyc`
    from here on and drops the one an evicted module was loaded from: a same-size edit in
    the same second passes the pyc's mtime-and-size check."""
    sys.dont_write_bytecode = True
    root = f"{project_root}{os.sep}"
    keep = tuple(
        p
        for p in (f"{sys.prefix}{os.sep}", f"{get_stelvio_lib_root()}{os.sep}")
        if p.startswith(root)
    )
    for name, module in list(sys.modules.items()):
        path = getattr(module, "__file__", None) or next(
            iter(getattr(module, "__path__", None) or []), None
        )
        if path and path.startswith(root) and not path.startswith(keep):
            del sys.modules[name]
            # `__spec__.cached`, not `__cached__`: Python 3.15 dropped the module attribute
            if cached := getattr(getattr(module, "__spec__", None), "cached", None):
                Path(cached).unlink(missing_ok=True)
    importlib.invalidate_caches()  # a helper file created since the last call is found


def _install_stlv_resources(code: CodeType | None) -> None:
    """Fresh module per call, so cached_property values follow this function's env. None in
    sys.modules makes the import fail like it does in a Lambda packaged without the file,
    instead of finding the folder's on-disk union."""
    if code is None:
        sys.modules["stlv_resources"] = None
        return
    module = ModuleType("stlv_resources")
    sys.modules["stlv_resources"] = module  # dataclasses look the module up by name
    exec(code, module.__dict__)  # noqa: S102  # Stelvio's own generated code, not user input


def _import_handler_module(config: FunctionConfig, project_root: Path) -> ModuleType:
    """Import the way the Lambda runtime does (`import_module("sub.handler")` from the zip
    root), so nested handlers, relative imports and `__name__` match. A single-file handler
    loads by path with nothing added to sys.path: a sibling import fails here as on Lambda."""
    module_name = config.local_handler_file_path.replace("/", ".")
    top_level = module_name.partition(".")[0]
    if top_level in sys.modules:  # project modules were just evicted, so this is a foreign one
        raise ImportError(
            f"Handler file {config.full_handler_python_path} imports as {module_name!r} but "
            f"{top_level!r} is an already imported module; Lambda would import that module "
            "instead. Rename the file."
        )
    if config.folder_path:
        return importlib.import_module(module_name)
    spec = importlib.util.spec_from_file_location(
        module_name, project_root / config.full_handler_python_path
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module  # dataclasses and pickle look the module up by name
    spec.loader.exec_module(module)
    return module


@link_config_creator(Function)
def default_function_link(function_component: Function) -> LinkConfig:
    function_resource = function_component.resources.function
    return LinkConfig(
        properties={
            "function_arn": function_resource.arn,
            "function_name": function_resource.name,
        },
        permissions=[
            AwsPermission(
                actions=["lambda:InvokeFunction"],
                resources=[function_resource.arn],
            ),
        ],
    )
