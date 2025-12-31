import json
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from functools import wraps
from hashlib import sha256
from typing import Any, ClassVar

from pulumi import Resource as PulumiResource

from stelvio.bridge.local.dtos import BridgeInvocationResult
from stelvio.link import LinkConfig


class Component[ResourcesT](ABC):
    _name: str
    _resources: ResourcesT | None

    def __init__(self, name: str):
        self._name = name
        self._resources = None
        ComponentRegistry.add_instance(self)

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


class BridgeableComponent(ABC):
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

    @abstractmethod
    async def _handle_bridge_event(
        self,
        data: dict,
    ) -> BridgeInvocationResult | None:
        """Handle incoming bridge event"""
        raise NotImplementedError


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
    def register_default_link_creator[T: PulumiResource](
        cls, component_type: type[Component[T]], creator_fn: Callable[[T], LinkConfig]
    ) -> None:
        """Register a default link creator, which will be used if no user-defined creator exists"""
        cls._default_link_creators[component_type] = creator_fn

    @classmethod
    def register_user_link_creator[T: PulumiResource](
        cls, component_type: type[Component[T]], creator_fn: Callable[[T], LinkConfig]
    ) -> None:
        """Register a user-defined link creator, which takes precedence over defaults"""
        cls._user_link_creators[component_type] = creator_fn

    @classmethod
    def get_link_config_creator[T: PulumiResource](
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


def link_config_creator[T: PulumiResource](
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
