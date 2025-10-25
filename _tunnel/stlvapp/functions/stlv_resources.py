import os
from dataclasses import dataclass
from typing import Final
from functools import cached_property


@dataclass(frozen=True)
class MessagesResource:
    @cached_property
    def table_arn(self) -> str:
        return os.getenv("STLV_MESSAGES_TABLE_ARN")

    @cached_property
    def table_name(self) -> str:
        return os.getenv("STLV_MESSAGES_TABLE_NAME")


@dataclass(frozen=True)
class LinkedResources:
    messages: Final[MessagesResource] = MessagesResource()


Resources: Final = LinkedResources()