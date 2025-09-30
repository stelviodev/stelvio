from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional, Protocol, final

from pulumi import Input

from stelvio.aws.permission import AwsPermission

if TYPE_CHECKING:
    from stelvio.component import Component


# This protocol is not strictly needed as in Link we use AwsPermission directly and later we'll
# use union types e.g. AwsPermission | GcpPermission but I keep it here for reference and for
# letting people know any new permission types should have to_provider_format method.
@dataclass
class Permission(Protocol):
    def to_provider_format(self) -> Any:  # noqa: ANN401
        """Convert permission to provider-specific format."""
        ...


type ConfigureLink = Callable[[Any], tuple[dict, list[Permission] | Permission]]


# Link has permissions, and each permission has actions and resources
# so permission represents part of statement
@final
@dataclass(frozen=True)
class LinkConfig:
    properties: dict[str, Input[str]] | None = None
    permissions: Sequence[AwsPermission] | None = None


@final
@dataclass(frozen=True)
class Link:
    name: str
    properties: dict[str, Input[str]] | None
    permissions: Sequence[AwsPermission] | None
    component: Optional["Component"] = None

    def link(self) -> "Link":
        return self

    def with_config(
        self,
        *,
        properties: dict[str, Input[str]] | None = None,
        permissions: list[Permission] | None = None,
    ) -> "Link":
        """Replace both properties and permissions at once."""
        return Link(
            name=self.name,
            properties=properties,
            permissions=permissions,
            component=self.component,
        )

    def with_properties(self, **props: Input[str]) -> "Link":
        """Replace all properties."""
        return Link(
            name=self.name,
            properties=props,
            permissions=self.permissions,
            component=self.component,
        )

    def with_permissions(self, *permissions: AwsPermission) -> "Link":
        """Replace all permissions."""
        return Link(
            name=self.name,
            properties=self.properties,
            permissions=list(permissions),
            component=self.component,
        )

    def add_properties(self, **extra_props: Input[str]) -> "Link":
        """Add to existing properties."""
        new_props = {**(self.properties or {}), **extra_props}
        return self.with_properties(**new_props)

    def add_permissions(self, *extra_permissions: AwsPermission) -> "Link":
        """Add to existing permissions."""
        current = self.permissions or []
        return self.with_permissions(*(current + list(extra_permissions)))

    def remove_properties(self, *keys: str) -> "Link":
        """Remove specific properties by key."""
        if not self.properties:
            return self

        new_props = {k: v for k, v in self.properties.items() if k not in keys}
        return self.with_properties(**new_props)


class Linkable(Protocol):
    def link(self) -> Link:
        raise NotImplementedError
