import os
from dataclasses import dataclass
from functools import cached_property
from typing import Final


@dataclass(frozen=True)
class TodosTableResource:
    @cached_property
    def table_arn(self) -> str:
        return os.environ["STLV_TODOS_TABLE_TABLE_ARN"]

    @cached_property
    def table_name(self) -> str:
        return os.environ["STLV_TODOS_TABLE_TABLE_NAME"]


@dataclass(frozen=True)
class LinkedResources:
    todos_table: Final[TodosTableResource] = TodosTableResource()


Resources: Final = LinkedResources()
