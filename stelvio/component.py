from abc import ABC, abstractmethod
from functools import wraps
from typing import ClassVar, Callable, Any
from typing import Iterator

from pulumi import Resource as PulumiResource

from stelvio.link import LinkConfig


class Component[ResourceT: PulumiResource](ABC):
    # TODO: need to be unique, so validate for uniqueness
    _name: str

    def __init__(
        self,
        name: str,
    ) -> None:
        self._name = name
        print(f"{self._name}")
        ComponentRegistry.add_instance(self)

    @property
    def name(self) -> str:
        return self._name

    @property
    def _resource(self) -> ResourceT:
        if existing := ComponentRegistry.get_output(self):
            return existing

        resource = self._create_resource()
        ComponentRegistry.add_instance_output(self, resource)
        return resource

    def _ensure_resource(self) -> None:
        # Just triggers creation/ensures existence
        _ = self._resource

    @abstractmethod
    def _create_resource(self) -> ResourceT:
        """Implement actual resource creation logic"""
        ...


class ComponentRegistry:
    _instances: ClassVar[dict[type[Component], list[Component]]] = {}
    _instance_output_pairs: ClassVar[dict[Component, PulumiResource]] = {}
    _type_link_creators: ClassVar[dict[type, Callable]] = {}

    @classmethod
    def add_instance(cls, instance: Component[Any]):
        if type(instance) not in cls._instances:
            cls._instances[type(instance)] = []
        cls._instances[type(instance)].append(instance)

    @classmethod
    def add_instance_output[T: PulumiResource](cls, instance: Component[T], output: T):
        cls._instance_output_pairs[instance] = output

    @classmethod
    def get_output[T: PulumiResource](cls, instance: Component[T]) -> T:
        return cls._instance_output_pairs.get(instance)

    @classmethod
    def register_link_config_creator[
        T: PulumiResource
    ](cls, component_type: type[Component[T]], creator_fn: Callable[[T], LinkConfig]):
        cls._type_link_creators[component_type] = creator_fn

    @classmethod
    def get_link_config_creator[
        T: PulumiResource
    ](cls, component_type: type[Component[T]]) -> Callable[[T], LinkConfig] | None:
        return cls._type_link_creators.get(component_type)

    @classmethod
    def all_instances(cls) -> Iterator[Component[Any]]:
        instances = cls._instances.copy()
        for k in instances:
            for i in instances[k]:
                yield i


def link_config_creator[T: PulumiResource](component_type: type[Component[T]]):
    def decorator(func: Callable[[T], LinkConfig]) -> Callable[[T], LinkConfig]:
        @wraps(func)
        def wrapper(resource: T) -> LinkConfig:
            return func(resource)

        ComponentRegistry.register_link_config_creator(component_type, func)
        return wrapper

    return decorator
