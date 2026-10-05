import os
from dataclasses import dataclass
from typing import Final
from functools import cached_property


@dataclass(frozen=True)
class DocdbResource:
    @cached_property
    def host(self) -> str:
        return os.environ["STLV_DOCDB_HOST"]

    @cached_property
    def reader_host(self) -> str:
        return os.environ["STLV_DOCDB_READER_HOST"]

    @cached_property
    def port(self) -> str:
        return os.environ["STLV_DOCDB_PORT"]

    @cached_property
    def username(self) -> str:
        return os.environ["STLV_DOCDB_USERNAME"]

    @cached_property
    def secret_arn(self) -> str:
        return os.environ["STLV_DOCDB_SECRET_ARN"]

    @cached_property
    def replica_set(self) -> str:
        return os.environ["STLV_DOCDB_REPLICA_SET"]

    @cached_property
    def ca_file(self) -> str:
        return os.environ["STLV_DOCDB_CA_FILE"]

    @cached_property
    def connection_uri(self) -> str:
        return os.environ["STLV_DOCDB_CONNECTION_URI"]


@dataclass(frozen=True)
class LinkedResources:
    docdb: Final[DocdbResource] = DocdbResource()


Resources: Final = LinkedResources()