from dataclasses import dataclass
from enum import Enum
from typing import final

import pulumi
from pulumi import Output
from pulumi_aws.dynamodb import Table

from stelvio import context
from stelvio.aws.permission import AwsPermission
from stelvio.component import Component, ComponentRegistry, link_config_creator
from stelvio.link import Link, Linkable, LinkConfig


class AttributeType(Enum):
    STRING = "S"
    NUMBER = "N"
    BINARY = "B"


@dataclass(frozen=True)
class DynamoTableResources:
    table: Table


@final
class DynamoTable(Component[DynamoTableResources], Linkable):
    def __init__(
        self,
        name: str,
        *,
        fields: dict[str, AttributeType],
        partition_key: str,
        sort_key: str | None = None,
    ):
        super().__init__(name)
        self._fields = fields
        self._partition_key = partition_key
        self._sort_key = sort_key

        if self._partition_key not in self.fields:
            raise ValueError(f"partition_key '{self._partition_key}' not in fields list")

        if self._sort_key and self.sort_key not in self.fields:
            raise ValueError(f"sort_key '{self.sort_key}' not in fields list")

        self._resources = None

    @property
    def fields(self) -> dict[str, AttributeType]:
        return dict(self._fields)  # Return a copy to prevent modification

    @property
    def partition_key(self) -> str:
        return self._partition_key

    @property
    def sort_key(self) -> str | None:
        return self._sort_key

    @property
    def arn(self) -> Output[str]:
        return self.resources.table.arn

    def _create_resources(self) -> DynamoTableResources:
        table = Table(
            context().prefix(self.name),
            billing_mode="PAY_PER_REQUEST",
            hash_key=self.partition_key,
            range_key=self.sort_key,
            attributes=[{"name": k, "type": v.value} for k, v in self.fields.items()],
        )
        pulumi.export(f"dynamotable_{self.name}_arn", table.arn)
        pulumi.export(f"dynamotable_{self.name}_name", table.name)
        return DynamoTableResources(table)

    # we can also provide other predefined links e.g read only, index etc.
    def link(self) -> Link:
        link_creator_ = ComponentRegistry.get_link_config_creator(type(self))

        link_config = link_creator_(self._resources.table)
        return Link(self.name, link_config.properties, link_config.permissions)


@link_config_creator(DynamoTable)
def default_dynamo_table_link(table: Table) -> LinkConfig:
    # https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_policies_examples_lambda-access-dynamodb.html
    return LinkConfig(
        properties={"table_arn": table.arn, "table_name": table.name},
        permissions=[
            AwsPermission(
                actions=[
                    "dynamodb:Scan",
                    "dynamodb:Query",
                    "dynamodb:GetItem",
                    "dynamodb:PutItem",
                    "dynamodb:UpdateItem",
                    "dynamodb:DeleteItem",
                ],
                resources=[table.arn],
            )
        ],
    )
