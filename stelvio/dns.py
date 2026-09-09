from dataclasses import dataclass
from typing import Protocol

from pulumi import Input, Resource, ResourceOptions


class DnsProviderNotConfiguredError(AttributeError):
    """Raised when DNS provider is not configured in the context."""


@dataclass(frozen=True)
class Record:
    pulumi_resource: Resource
    name: Input[str]


class Dns(Protocol):
    def create_record(  # noqa: PLR0913
        self,
        resource_name: str,
        name: Input[str],
        record_type: Input[str],
        value: Input[str],
        ttl: int = 1,
        *,
        opts: ResourceOptions | None = None,
    ) -> Record:
        """
        Create a DNS record with the given name, type, and value.

        Forward `opts` to the Pulumi record so Stelvio can parent it under the component.
        """
        ...
