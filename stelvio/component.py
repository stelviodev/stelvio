from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from functools import wraps
from hashlib import sha256
from typing import TYPE_CHECKING, Any, ClassVar, Protocol, get_args, get_origin

from stelvio import context
from stelvio.pulumi import normalize_pulumi_args_to_dict

_normalize = normalize_pulumi_args_to_dict
logger = logging.getLogger("stelvio.component")

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from stelvio.bridge.local.dtos import BridgeInvocationResult
    from stelvio.link import LinkConfig


class Component[ResourcesT, CustomizationT](ABC):
    _name: str
    _resources: ResourcesT | None
    _customize: CustomizationT | None = None

    def __init__(self, name: str, customize: CustomizationT | None = None):
        self._name = name
        self._resources = None
        self._customize = customize
        if self._customize is None:
            self._customize = {}
        self._validate_customize_keys()
        ComponentRegistry.add_instance(self)

    def _validate_customize_keys(self) -> None:
        """Validate that all keys in customize dict are valid for this component.

        Logs a warning for any unknown keys to help catch typos early.
        """
        if not self._customize:
            return

        # Get the CustomizationT type from __orig_bases__
        valid_keys = self._get_valid_customize_keys()
        if valid_keys is None:
            return  # Could not determine valid keys, skip validation

        provided_keys = set(self._customize.keys())
        unknown_keys = provided_keys - valid_keys

        for key in unknown_keys:
            logger.warning(
                "Unknown customization key '%s' for %s '%s'. Valid keys: %s",
                key,
                type(self).__name__,
                self._name,
                sorted(valid_keys),
            )

    def _get_valid_customize_keys(self) -> set[str] | None:
        """Extract valid customization keys from the CustomizationT TypedDict.

        Returns None if the keys cannot be determined (e.g., generic dict type).
        Uses __annotations__ directly to avoid forward reference resolution issues.
        """
        # Walk up the MRO looking for Component with type args
        for base in type(self).__orig_bases__:
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
    def resources(self) -> ResourcesT:
        if not self._resources:
            self._resources = self._create_resources()
        return self._resources

    @abstractmethod
    def _create_resources(self) -> ResourcesT:
        """Implement actual resource creation logic"""
        raise NotImplementedError

    def _customizer(self, resource_name: str, default_props: dict[str, dict]) -> dict:
        """Merge default props with global and per-instance customizations.

        The merge is intentionally SHALLOW (one level deep). This means:
        - Top-level keys are merged (new keys added, existing keys overwritten)
        - Nested dicts are completely replaced, NOT recursively merged

        Example of shallow merge behavior:
            default_props = {"tags": {"a": 1, "b": 2}}
            global_customize = {"tags": {"c": 3}}
            Result: {"tags": {"c": 3}}  (NOT {"a": 1, "b": 2, "c": 3})

        Precedence (highest to lowest):
            1. Per-instance customize (self._customize)
            2. Global customize from StelvioAppConfig
            3. Stelvio defaults (default_props)

        This shallow merge is also why function-based customization requires
        returning the complete object - partial returns would lose other fields.
        """
        global_customize = context().customize.get(type(self), {})
        return {
            **default_props,
            **_normalize(global_customize.get(resource_name)),
            **_normalize(self._customize.get(resource_name)),
        }


class Bridgeable(Protocol):
    _dev_endpoint_id: str | None

    async def handle_bridge_event(
        self,
        data: dict,
    ) -> BridgeInvocationResult | None:
        """Handle incoming bridge event"""
        raise NotImplementedError


class BridgeableMixin:
    _dev_endpoint_id: str | None = None

    async def handle_bridge_event(
        self,
        data: dict,
    ) -> BridgeInvocationResult | None:
        """Handle incoming bridge event"""
        if not self._dev_endpoint_id:
            return None
        event = data.get("event", "null")
        event = json.loads(event) if isinstance(event, str) else event
        if event.get("endpointId") != self._dev_endpoint_id:
            return None
        return await self._handle_bridge_event(data)


class ComponentRegistry:
    _instances: ClassVar[dict[type[Component], list[Component]]] = {}
    _registered_names: ClassVar[set[str]] = set()

    # Two-tier registry for link creators
    _default_link_creators: ClassVar[dict[type, Callable]] = {}
    _user_link_creators: ClassVar[dict[type, Callable]] = {}

    @classmethod
    def add_instance(cls, instance: Component[Any]) -> None:
        if instance.name in cls._registered_names:
            raise ValueError(
                f"Duplicate Stelvio component name detected: '{instance.name}'. "
                "Component names must be unique across all component types."
            )
        cls._registered_names.add(instance.name)
        if type(instance) not in cls._instances:
            cls._instances[type(instance)] = []
        cls._instances[type(instance)].append(instance)

    @classmethod
    def register_default_link_creator[T: Component](
        cls, component_type: type[Component[T]], creator_fn: Callable[[T], LinkConfig]
    ) -> None:
        """Register a default link creator, which will be used if no user-defined creator exists"""
        cls._default_link_creators[component_type] = creator_fn

    @classmethod
    def register_user_link_creator[T: Component](
        cls, component_type: type[Component[T]], creator_fn: Callable[[T], LinkConfig]
    ) -> None:
        """Register a user-defined link creator, which takes precedence over defaults"""
        cls._user_link_creators[component_type] = creator_fn

    @classmethod
    def get_link_config_creator[T: Component](
        cls, component_type: type[Component]
    ) -> Callable[[T], LinkConfig] | None:
        """Get the link creator for a component type, prioritizing user-defined over defaults"""
        # First check user-defined creators, then fall back to defaults
        return cls._user_link_creators.get(component_type) or cls._default_link_creators.get(
            component_type
        )

    @classmethod
    def all_instances(cls) -> Iterator[Component[Any]]:
        instances = cls._instances.copy()
        for k in instances:
            yield from instances[k]

    @classmethod
    def instances_of[T: Component](cls, component_type: type[T]) -> Iterator[T]:
        yield from cls._instances.get(component_type, [])

    @classmethod
    def get_component_by_name(cls, name: str) -> Component[Any] | None:
        if name not in cls._registered_names:
            return None
        for instance in cls.all_instances():
            if instance.name == name:
                return instance
        return None


def link_config_creator[T: Component](
    component_type: type[Component],
) -> Callable[[Callable[[T], LinkConfig]], Callable[[T], LinkConfig]]:
    """Decorator to register a default link creator for a component type"""

    def decorator(func: Callable[[T], LinkConfig]) -> Callable[[T], LinkConfig]:
        @wraps(func)
        def wrapper(resource: T) -> LinkConfig:
            return func(resource)

        ComponentRegistry.register_default_link_creator(component_type, func)
        return wrapper

    return decorator


def safe_name(
    prefix: str, name: str, max_length: int, suffix: str = "", pulumi_suffix_length: int = 8
) -> str:
    """Create safe AWS resource name accounting for Pulumi suffix and custom suffix.

    Args:
        prefix: The app-env prefix (e.g., "myapp-prod-")
        name: The base name for the resource
        max_length: AWS service limit for the resource type
        suffix: Custom suffix to add (e.g., '-r', '-p')
        pulumi_suffix_length: Length of Pulumi's random suffix (default 8, use 0 if none)

    Returns:
        Safe name that will fit within AWS limits after Pulumi adds its suffix
    """
    # Calculate space available for the base name
    reserved_space = len(prefix) + len(suffix) + pulumi_suffix_length
    available_for_name = max_length - reserved_space

    if available_for_name <= 0:
        raise ValueError(
            f"Cannot create safe name: prefix '{prefix}' ({len(prefix)} chars), "
            f"suffix '{suffix}' ({len(suffix)} chars), and Pulumi suffix "
            f"({pulumi_suffix_length} chars) exceed max_length ({max_length})"
        )

    # Validate name is not empty
    if not name.strip():
        raise ValueError("Name cannot be empty or whitespace-only")

    # Truncate name if needed
    if len(name) <= available_for_name:
        return f"{prefix}{name}{suffix}"

    # Need to truncate - reserve space for 7-char hash + dash
    hash_with_separator = 8  # 7 chars + 1 dash
    if available_for_name <= hash_with_separator:
        raise ValueError(
            f"Not enough space for name truncation: available={available_for_name}, "
            f"need at least {hash_with_separator} chars for hash"
        )

    # Truncate from end and add hash
    truncate_length = available_for_name - hash_with_separator
    name_hash = sha256(name.encode()).hexdigest()[:7]
    safe_name_part = f"{name[:truncate_length]}-{name_hash}"

    return f"{prefix}{safe_name_part}{suffix}"
