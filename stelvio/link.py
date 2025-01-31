from dataclasses import dataclass
from typing import Protocol, Callable, Any, Tuple, TypeAlias, final, Optional

from pulumi import Output, Input


@dataclass
class Permission(Protocol):
    def to_provider_format(self) -> Any:
        """Convert permission to provider-specific format"""
        ...


ConfigureLink: TypeAlias = Callable[[Any], Tuple[dict, list[Permission] | Permission]]


# Link has permissions, and each permission has actions and resources
# so permission represents part of statement
@final
@dataclass(frozen=True)
class LinkConfig:
    properties: dict[str, Input[str]] | None = None
    permissions: list[Permission] | None = None


@final
@dataclass(frozen=True)
class Link:
    name: str
    properties: dict[str, Input[str]] | None
    permissions: list[Permission] | None
    component: Optional["Component"] = None

    def link(self) -> "Link":
        return self

    def with_config(
        self,
        *,
        properties: dict[str, Input[str]] | None = None,
        permissions: list[Permission] | None = None,
    ) -> "Link":
        """Replace both properties and permissions at once"""
        return Link(
            name=self.name,
            properties=properties,
            permissions=permissions,
            component=self.component,
        )

    def with_properties(self, **props) -> "Link":
        """Replace all properties"""
        return Link(
            name=self.name,
            properties=props,
            permissions=self.permissions,
            component=self.component,
        )

    def with_permissions(self, *permissions: Permission) -> "Link":
        """Replace all permissions"""
        return Link(
            name=self.name,
            properties=self.properties,
            permissions=list(permissions),
            component=self.component,
        )

    def add_properties(self, **extra_props) -> "Link":
        """Add to existing properties"""
        new_props = {**(self.properties or {}), **extra_props}
        return self.with_properties(**new_props)

    def add_permissions(self, *extra_permissions: Permission) -> "Link":
        """Add to existing permissions"""
        current = self.permissions or []
        return self.with_permissions(*(current + list(extra_permissions)))


class Linkable(Protocol):
    def link(self) -> Link:
        raise NotImplemented()
