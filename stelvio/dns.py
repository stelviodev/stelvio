from typing import Protocol

from pulumi import Input, Resource


class DnsProviderNotConfiguredError(AttributeError):
    """Raised when DNS provider is not configured in the context."""


class Record:
    def __init__(self, pulumi_resource: Resource):
        self._pulumi_resource = pulumi_resource

    @property
    def pulumi_resource(self) -> Resource:
        return self._pulumi_resource


class Dns(Protocol):
    def create_record(
        self, resource_name: str, name: str, record_type: str, value: Input[str], ttl: int = 1
    ) -> Record:
        """
        Create a DNS record with the given name, type, and value.
        """
        raise NotImplementedError(
            "No DNS provider configured. "
            "Please set up a DNS provider in your Stelvio app configuration."
        )

    def create_caa_record(
        self, resource_name: str, name: str, record_type: str, content: str, ttl: int = 1
    ) -> Record:
        """
        Create a CAA DNS record with the given name, type, and content.
        """
        raise NotImplementedError(
            "No DNS provider configured. "
            "Please set up a DNS provider in your Stelvio app configuration."
        )
