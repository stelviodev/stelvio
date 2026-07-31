from typing import Protocol

from pulumi import Input, Resource, ResourceOptions


class DnsProviderNotConfiguredError(AttributeError):
    """Raised when DNS provider is not configured in the context."""


class Record:
    def __init__(self, pulumi_resource: Resource):
        self._pulumi_resource = pulumi_resource

    @property
    def pulumi_resource(self) -> Resource:
        return self._pulumi_resource


class Dns(Protocol):
    def create_record(  # noqa: PLR0913
        self,
        resource_name: str,
        name: str,
        record_type: str,
        value: Input[str],
        ttl: int = 1,
        *,
        opts: ResourceOptions | None = None,
    ) -> Record:
        """
        Create a DNS record with the given name, type, and value.

        Providers SHOULD forward ``opts`` to the underlying Pulumi record
        resource so Stelvio can parent records under components.
        """
        raise NotImplementedError(
            "No DNS provider configured. "
            "Please set up a DNS provider in your Stelvio app configuration."
        )

    def create_caa_record(  # noqa: PLR0913
        self,
        resource_name: str,
        name: str,
        record_type: str,
        content: str,
        ttl: int = 1,
        *,
        opts: ResourceOptions | None = None,
    ) -> Record:
        """
        Create a CAA DNS record with the given name, type, and content.

        Providers SHOULD forward ``opts`` to the underlying Pulumi record
        resource so Stelvio can parent records under components.
        """
        raise NotImplementedError(
            "No DNS provider configured. "
            "Please set up a DNS provider in your Stelvio app configuration."
        )
