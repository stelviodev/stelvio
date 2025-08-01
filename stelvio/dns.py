from typing import Protocol


class Record:
    def __init__(self, pulumi_resource):
        self._pulumi_resource = pulumi_resource


class Dns(Protocol):
    def create_record(self, name: str, type: str, value: str) -> Record:
        """
        Create a DNS record with the given name, type, and value.
        """
        raise NotImplementedError(
            "No DNS provider configured. Please set up a DNS provider in your Stelvio app configuration."
        )

    def create_caa_record(self, name: str, type: str, content: str) -> Record:
        """
        Create a CAA DNS record with the given name, type, and content.
        """
        raise NotImplementedError(
            "No DNS provider configured. Please set up a DNS provider in your Stelvio app configuration."
        )
