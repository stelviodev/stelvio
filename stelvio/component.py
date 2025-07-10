from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from functools import wraps
from typing import Any, ClassVar

from pulumi import Resource as PulumiResource

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
