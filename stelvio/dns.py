from collections.abc import Callable
from inspect import Parameter, signature
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
    def create_record(
        self, resource_name: str, name: str, record_type: str, value: Input[str], ttl: int = 1
    ) -> Record:
        """
        Create a DNS record with the given name, type, and value.

        New providers should also accept keyword-only
        ``opts: ResourceOptions | None = None`` and forward it to the underlying
        Pulumi record resource so Stelvio can parent records under components.
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

        New providers should also accept keyword-only
        ``opts: ResourceOptions | None = None`` and forward it to the underlying
        Pulumi record resource so Stelvio can parent records under components.
        """
        raise NotImplementedError(
            "No DNS provider configured. "
            "Please set up a DNS provider in your Stelvio app configuration."
        )


def _call_with_optional_resource_options(
    method: Callable[..., Record],
    /,
    *,
    opts: ResourceOptions,
    **kwargs: object,
) -> Record:
    """Call a DNS provider with parenting options when its signature supports them."""
    try:
        parameters = signature(method).parameters
    except (TypeError, ValueError):
        parameters = {}

    supports_opts = "opts" in parameters or any(
        parameter.kind is Parameter.VAR_KEYWORD for parameter in parameters.values()
    )
    if supports_opts:
        return method(**kwargs, opts=opts)
    return method(**kwargs)
