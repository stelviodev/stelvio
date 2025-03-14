from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from functools import wraps
from typing import Any, ClassVar, cast

from pulumi import Resource as PulumiResource

from stelvio.link import LinkConfig


class Component[ResourceT: PulumiResource](ABC):
    # TODO: need to be unique, so validate for uniqueness
    _name: str

    def __init__(self, name: str):
        self._name = name
        ComponentRegistry.add_instance(self)

    @property
    def name(self) -> str:
        return self._name

    @property
    def _resource(self) -> ResourceT:
        return ComponentRegistry.get_output(self) or self._create_resource()

    def _ensure_resource(self) -> None:
        # Just triggers creation/ensures existence
        _ = self._resource

    @abstractmethod
    def _create_resource(self) -> ResourceT:
        """Implement actual resource creation logic"""
        ...


class ComponentRegistry:
    _instances: ClassVar[dict[type[Component], list[Component]]] = {}
    _instance_output_pairs: ClassVar[dict[Component[PulumiResource], PulumiResource]] = {}

    # Two-tier registry for link creators
    _default_link_creators: ClassVar[dict[type, Callable]] = {}
    _user_link_creators: ClassVar[dict[type, Callable]] = {}

    @classmethod
    def add_instance(cls, instance: Component[Any]) -> None:
        if type(instance) not in cls._instances:
            cls._instances[type(instance)] = []
        cls._instances[type(instance)].append(instance)

    @classmethod
    def add_instance_output[T: PulumiResource](cls, instance: Component[T], output: T) -> None:
        cls._instance_output_pairs[instance] = output

    @classmethod
    def get_output[T: PulumiResource](cls, instance: Component[T]) -> T | None:
        resource = cls._instance_output_pairs.get(instance)
        if resource is not None:
            return cast("T", resource)
        return None

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
        cls, component_type: type[Component[T]]
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


def link_config_creator[T: PulumiResource](
    component_type: type[Component[T]],
) -> Callable[[Callable[[T], LinkConfig]], Callable[[T], LinkConfig]]:
    """Decorator to register a default link creator for a component type"""

    def decorator(func: Callable[[T], LinkConfig]) -> Callable[[T], LinkConfig]:
        @wraps(func)
        def wrapper(resource: T) -> LinkConfig:
            return func(resource)

        ComponentRegistry.register_default_link_creator(component_type, func)
        return wrapper

    return decorator
