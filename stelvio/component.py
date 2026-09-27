from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from collections.abc import Mapping
from functools import wraps
from hashlib import sha256
from types import get_original_bases
from typing import TYPE_CHECKING, Any, ClassVar, Protocol, get_args, get_origin

import pulumi

from stelvio import context
from stelvio.pulumi import normalize_pulumi_args_to_dict

_normalize = normalize_pulumi_args_to_dict
logger = logging.getLogger("stelvio.component")

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from stelvio.bridge.local.dtos import BridgeInvocationResult
    from stelvio.link import LinkConfig


class Component[ResourcesT, CustomizationT](pulumi.ComponentResource, ABC):
    _name: str
    _resources: ResourcesT | None
    _created: bool
    _customize: CustomizationT
    _tags: dict[str, str]
    _provider: pulumi.ProviderResource

    def __init__(  # noqa: PLR0913
        self,
        provider: pulumi.ProviderResource,
        type_name: str,
        name: str,
        *,
        tags: dict[str, str] | None = None,
        customize: CustomizationT | None = None,
        parent: pulumi.Resource | None = None,
    ):
        """Initialize a Stelvio component.

        Args:
            provider: Provider this component's resources deploy through.
            type_name: Pulumi type URN (e.g., ``"stelvio:aws:Function"``).
            name: Globally unique component name.
            tags: AWS tags applied to taggable child resources.
            customize: Per-resource overrides (shallow-merged with defaults).
            parent: Parent Pulumi resource for nesting. Used internally by
                components that create child components (e.g., Cron creating a
                Function). Sets up the proper resource tree hierarchy and adds an
                alias for migration from the root stack.
        """
        resource_opts = pulumi.ResourceOptions(providers=[provider], parent=parent)
        if parent is not None:
            # Allow migration from previously top-level components when introducing
            # parent relationships in composed components.
            resource_opts.aliases = [pulumi.Alias(parent=pulumi.ROOT_STACK_RESOURCE)]
        super().__init__(type_name, name, None, resource_opts)
        self._name = name
        self._provider = provider
        self._resources = None
        self._created = False
        self._customize = customize or {}
        self._tags = tags or {}
        self._validate_tags()
        self._validate_customize_keys()
        ComponentRegistry.add_instance(self)

    def _validate_tags(self) -> None:
        if not isinstance(self._tags, dict):
            raise TypeError(f"tags must be a dict[str, str], got {type(self._tags).__name__}")
        for key, value in self._tags.items():
            if not isinstance(key, str):
                raise TypeError(
                    f"Tag key must be str, got {type(key).__name__} in tags={self._tags!r}"
                )
            if not isinstance(value, str):
                raise TypeError(
                    f"Tag value for key '{key}' must be str, got {type(value).__name__}"
                )

    def _validate_customize_keys(self) -> None:
        """Validate that all keys in the `_customize` dict are valid for this component.

        Raises ValueError for any unknown keys to catch typos early.
        """
        if not self._customize:
            return

        # Get the CustomizationT type from __orig_bases__
        valid_keys = self._get_valid_customize_keys()
        if valid_keys is None:
            return  # Could not determine valid keys, skip validation

        provided_keys = set(self._customize.keys())
        unknown_keys = provided_keys - valid_keys

        if unknown_keys:
            unknown_list = sorted(unknown_keys)
            valid_list = sorted(valid_keys)
            raise ValueError(
                f"Unknown customization key(s) {unknown_list} for {type(self).__name__} "
                f"'{self._name}'. Valid keys are: {valid_list}"
            )

    def _get_valid_customize_keys(self) -> set[str] | None:
        """Extract valid customization keys from the CustomizationT TypedDict.

        Returns None if the keys cannot be determined (e.g., generic dict type).
        Uses __annotations__ directly to avoid forward reference resolution issues.
        """
        # Walk up the MRO looking for Component with type args
        for base in get_original_bases(type(self)):
            origin = get_origin(base)
            if origin is Component or (isinstance(origin, type) and issubclass(origin, Component)):
                args = get_args(base)
                # Component[ResourcesT, CustomizationT] - need at least 2 type args
                if len(args) >= 2:  # noqa: PLR2004
                    customization_type = args[1]
                    # Handle Union types (e.g., CustomizationDict | None)
                    if get_origin(customization_type) is not None:
                        union_args = get_args(customization_type)
                        for arg in union_args:
                            if arg is not type(None) and hasattr(arg, "__annotations__"):
                                return set(arg.__annotations__.keys())
                    # Direct TypedDict
                    if hasattr(customization_type, "__annotations__"):
                        return set(customization_type.__annotations__.keys())
        return None

    @property
    def name(self) -> str:
        return self._name

    @property
    def registry_name(self) -> str:
        return self._name

    @property
    def tags(self) -> dict[str, str]:
        return dict(self._tags)

    @property
    def resources(self) -> ResourcesT:
        if self._resources is None:
            self._created = True
            try:
                self._resources = self._create_resources()
            except BaseException:
                self._created = False  # a failed creation must not lock the component
                raise
        return self._resources

    def _check_not_created(self, what: str) -> None:
        """Guard for builder methods (`route()`, `add_*()`); `what` names what the caller
        adds. The flag flips before `_create_resources` runs, so a call from inside
        creation fails too."""
        if self._created:
            raise RuntimeError(
                f"Cannot modify {type(self).__name__} '{self._name}' after resources have "
                f"been created. Add all {what} before reading .resources or a property "
                "built on it."
            )

    @abstractmethod
    def _create_resources(self) -> ResourcesT:
        """Implement actual resource creation logic"""
        raise NotImplementedError

    def _resource_opts(
        self,
        *,
        depends_on: list[pulumi.Resource] | None = None,
        provider: pulumi.ProviderResource | None = None,
        old_name: str | None = None,
    ) -> pulumi.ResourceOptions:
        """Create ResourceOptions that parent a sub-resource under this component.

        Includes an alias from ROOT_STACK_RESOURCE so existing deployments
        migrate transparently (resources move from stack root into the
        component tree without delete/recreate). ``old_name`` is the Pulumi name a
        renamed resource had before; it adds an alias so deployed stacks keep the
        resource instead of replacing it.
        """
        aliases = [pulumi.Alias(parent=pulumi.ROOT_STACK_RESOURCE)]
        if old_name:
            aliases.append(pulumi.Alias(name=old_name))
        return pulumi.ResourceOptions(
            parent=self, aliases=aliases, depends_on=depends_on, provider=provider
        )

    def _customizer(
        self,
        resource_key: str,
        computed_props: dict[str, Any],
        default_props: dict[str, Any] | None = None,
        *,
        inject_tags: bool = False,
    ) -> dict[str, Any]:
        """Apply global and per-instance customizations to resource props.

        Global customize dict acts as *defaults*: it fills in or overrides
        Stelvio's built-in defaults but does not override values set explicitly
        on the component. Global customize callable is an override: whatever it
        returns is used. Per-instance customize is applied last and overrides
        everything.

        Args:
            resource_key: Key identifying which resource of this component we
                are customizing.
            computed_props: Properties computed by Stelvio for this resource. A
                `None` value means "not set explicitly" (use a default); a
                non-`None` value is explicit and takes precedence over global
                dict customize, but only if there is no global callable customize
                (the callable sees the explicit value and decides if it is respected).
            default_props: Stelvio's default values for this resource. When
                omitted, `computed_props` is treated as the full set of props.
            inject_tags: If `True`, merge `self._tags` into the `tags` key of
                `computed_props` before customizing.

        Customization forms (both global and per-instance):
            - dict: shallow-merged (one level deep — nested dicts are replaced,
              NOT recursively merged).
            - callable: receives a props dict and returns the props to use.

        Global customize:
            - dict (acts as defaults): merged over `default_props`; explicit
              (non-`None`) `computed_props` still win over it.
            - callable (override): receives `computed_props` and returns a dict.
              The callable decides whether to respect explicit values (by checking
              e.g. `props.get(key) is None`). Non-`None` values from the return
              are merged over `default_props`, potentially overriding defaults and
              explicit values.

        Per-instance customize (highest precedence, applied last):
            - dict: shallow-merged over the current props.
            - callable: receives the current props; its return fully replaces them,
              so it must return everything that should be used.

        Precedence (highest to lowest):
            1. Per-instance customize (`self._customize`)
            2. Global callable customize from `StelvioAppConfig` (if present)
            3. Explicit (non-`None`) values in `computed_props`
            4. Global dict customize from `StelvioAppConfig` (if present)
            5. Stelvio defaults (`default_props`)

        Examples (`default_props = {"memory": 128, "timeout": 30}`):
            global dict `{"memory": 256}` + computed `{"memory": None}`
                → `{"memory": 256, "timeout": 30}` (global default is used)
            global dict `{"memory": 256}` + computed `{"memory": 512}`
                → `{"memory": 512, "timeout": 30}` (explicit value wins)
            global callable `lambda p: {**p, "memory": 512}` + computed
                `{"memory": 512}` → `{"memory": 512, "timeout": 30}` (callable
                can see explicit value and choose to preserve or override)
        """

        if inject_tags and self._tags:
            computed_props = {
                **computed_props,
                "tags": (computed_props.get("tags") or {}) | self._tags,
            }

        global_component_customize = context().customize.get(type(self), {})
        global_customize = global_component_customize.get(resource_key)
        local_customize = self._customize.get(resource_key)

        if default_props is None:
            default_props = {}

        explicit_props = {k: v for k, v in computed_props.items() if v is not None}

        if not global_customize:
            final_props = default_props | explicit_props
        elif callable(global_customize):
            global_result = _normalize(global_customize(computed_props))
            final_props = default_props | {k: v for k, v in global_result.items() if v is not None}
        else:
            final_props = default_props | _normalize(global_customize) | explicit_props

        if local_customize:
            if callable(local_customize):
                final_props = _normalize(local_customize(final_props))
            else:
                final_props |= _normalize(local_customize)

        return final_props


class Bridgeable(Protocol):
    _dev_endpoint_id: str | None

    async def handle_bridge_event(self, data: dict) -> BridgeInvocationResult | None:
        """Handle incoming bridge event"""
        raise NotImplementedError


class BridgeableMixin(ABC):
    _dev_endpoint_id: str | None = None

    async def handle_bridge_event(self, data: dict) -> BridgeInvocationResult | None:
        """Handle incoming bridge event"""
        if not self._dev_endpoint_id:
            return None
        raw = data.get("event")
        event = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(event, dict):
            return None
        if event.get("endpointId") != self._dev_endpoint_id:
            return None
        return await self._handle_bridge_event(data)

    @abstractmethod
    async def _handle_bridge_event(self, data: dict) -> BridgeInvocationResult | None:
        """Component-specific bridge handling, implemented by the host component."""


type ChildLabel = Callable[[str], str | None]


class ComponentRegistry:
    _instances: ClassVar[dict[type[Component], list[Component]]] = {}
    _registered_names: ClassVar[set[str]] = set()

    # Two-tier registry for link creators
    _default_link_creators: ClassVar[dict[type, Callable]] = {}
    _user_link_creators: ClassVar[dict[type, Callable]] = {}

    # CLI child-line labels, keyed by the component's URN type leaf ("Vpc")
    _child_labels: ClassVar[dict[str, ChildLabel]] = {}

    @classmethod
    def add_instance(cls, instance: Component[Any, Any]) -> None:
        registered_name = instance.registry_name
        if registered_name in cls._registered_names:
            raise ValueError(
                f"Duplicate Stelvio component name detected: '{registered_name}'. "
                "Component names must be unique across all component types."
            )
        cls._registered_names.add(registered_name)
        if type(instance) not in cls._instances:
            cls._instances[type(instance)] = []
        cls._instances[type(instance)].append(instance)

    @classmethod
    def register_default_link_creator[T: Component](
        cls, component_type: type[T], creator_fn: Callable[[T], LinkConfig]
    ) -> None:
        """Register a default link creator, which will be used if no user-defined creator exists"""
        cls._default_link_creators[component_type] = creator_fn

    @classmethod
    def register_user_link_creator[T: Component](
        cls, component_type: type[T], creator_fn: Callable[[T], LinkConfig]
    ) -> None:
        """Register a user-defined link creator, which takes precedence over defaults"""
        cls._user_link_creators[component_type] = creator_fn

    @classmethod
    def get_link_config_creator[T: Component](
        cls, component_type: type[T]
    ) -> Callable[[T], LinkConfig] | None:
        """Get the link creator for a component type, prioritizing user-defined over defaults"""
        # First check user-defined creators, then fall back to defaults
        return cls._user_link_creators.get(component_type) or cls._default_link_creators.get(
            component_type
        )

    @classmethod
    def register_child_label(cls, component_type: str, label_fn: ChildLabel) -> None:
        cls._child_labels[component_type] = label_fn

    @classmethod
    def get_child_label(cls, component_type: str) -> ChildLabel | None:
        return cls._child_labels.get(component_type)

    @classmethod
    def all_instances(cls) -> Iterator[Component[Any, Any]]:
        instances = cls._instances.copy()
        for k in instances:
            yield from instances[k]

    @classmethod
    def instances_of[T: Component](cls, component_type: type[T]) -> Iterator[T]:
        yield from cls._instances.get(component_type, [])

    @classmethod
    def get_component_by_name(cls, name: str) -> Component[Any, Any] | None:
        if name not in cls._registered_names:
            return None
        for instance in cls.all_instances():
            if instance.registry_name == name:
                return instance
        return None


def link_config_creator[T: Component](
    component_type: type[T],
) -> Callable[[Callable[[T], LinkConfig]], Callable[[T], LinkConfig]]:
    """Decorator to register a default link creator for a component type"""

    def decorator(func: Callable[[T], LinkConfig]) -> Callable[[T], LinkConfig]:
        @wraps(func)
        def wrapper(resource: T) -> LinkConfig:
            return func(resource)

        ComponentRegistry.register_default_link_creator(component_type, func)
        return wrapper

    return decorator


def child_label(component_type: str) -> Callable[[ChildLabel], ChildLabel]:
    """Register the label the CLI prints after same-type children of a component.

    When a component has several children of one resource type, deploy/diff output tells
    them apart with the child's name minus the app, env and component prefixes:
    `Subnet (public-subnet-a)`. The decorated function gets that short name and returns
    a shorter label (`public-a`), or `None` to keep the short name.

    Args:
        component_type: The leaf of the component's Pulumi type, e.g. ``"Vpc"`` for
            ``"stelvio:aws:Vpc"``.
    """

    def decorator(func: ChildLabel) -> ChildLabel:
        ComponentRegistry.register_child_label(component_type, func)
        return func

    return decorator


def resource_name(
    base: str, *, limit: int, suffix: str = "", pulumi_suffix_length: int = 8
) -> str:
    """App-env prefix + base + suffix, truncated with a 7-char hash to fit `limit`.

    `limit` is the AWS cap for the resource type, or the provider's autoname cap when
    that is lower. `pulumi_suffix_length` reserves room for Pulumi's random suffix; 0 when
    the string is passed as the AWS name. `suffix` survives truncation.
    """
    # Prefix can't be forgotten and the guard can't be skipped (#122, #230 did);
    # any AWS-facing name not built here is greppable-wrong. A plain function, not
    # a Component method: module-level helpers build names too.
    prefix = context().prefix()
    available = limit - len(prefix) - len(suffix) - pulumi_suffix_length
    if available <= 0:
        raise ValueError(
            f"Cannot build resource name: prefix '{prefix}' ({len(prefix)} chars), "
            f"suffix '{suffix}' ({len(suffix)} chars) and Pulumi suffix "
            f"({pulumi_suffix_length} chars) exceed limit ({limit})"
        )
    if not base.strip():
        raise ValueError("Name cannot be empty or whitespace-only")
    if len(base) <= available:
        return f"{prefix}{base}{suffix}"

    hash_with_separator = 8  # 7 chars + 1 dash
    if available <= hash_with_separator:
        raise ValueError(
            f"Not enough space for name truncation: available={available}, "
            f"need at least {hash_with_separator} chars for hash"
        )
    name_hash = sha256(base.encode()).hexdigest()[:7]
    return f"{prefix}{base[: available - hash_with_separator]}-{name_hash}{suffix}"


def parse_config[C](
    config_cls: type[C], config: C | Mapping[str, Any] | None, opts: Mapping[str, Any]
) -> C:
    """Resolve a component's config from its `config=` argument or its spread kwargs.

    One or the other, never both: raising beats guessing which value the user meant.
    """
    if config is not None and opts:
        raise ValueError(
            "Invalid configuration: cannot combine 'config' parameter with additional options "
            "- provide all settings either in 'config' or as separate options"
        )
    if config is None:
        return config_cls(**opts)
    if isinstance(config, config_cls):
        return config
    if isinstance(config, Mapping):
        return config_cls(**config)
    raise TypeError(
        f"Invalid config type: expected {config_cls.__name__} or dict, got {type(config).__name__}"
    )
