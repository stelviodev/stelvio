from typing import Protocol

from pulumi import Resource


class Record:
    def __init__(self, pulumi_resource: Resource):
        self._pulumi_resource = pulumi_resource


class Dns(Protocol):
    def create_record(self, name: str, record_type: str, value: str) -> Record:
        """
        Create a DNS record with the given name, type, and value.
        """
        raise NotImplementedError(
            "No DNS provider configured. "
            "Please set up a DNS provider in your Stelvio app configuration."
        )

    def create_caa_record(self, name: str, record_type: str, content: str) -> Record:
        """
        Create a CAA DNS record with the given name, type, and content.
        """
        raise NotImplementedError(
            "No DNS provider configured. "
            "Please set up a DNS provider in your Stelvio app configuration."
        )
