from dataclasses import dataclass, field
from enum import Enum
from typing import Literal, TypedDict, Unpack, final

import pulumi
from pulumi import Output
from pulumi_aws.dynamodb import Table

from stelvio import context
from stelvio.aws.permission import AwsPermission
from stelvio.component import Component, ComponentRegistry, link_config_creator
from stelvio.link import Link, Linkable, LinkConfig


def _convert_projection(
    projections: list[str] | Literal["keys-only", "all"],
) -> dict[str, str | list[str]]:
    """Convert projections to Pulumi format."""
    if projections == "keys-only":
        return {"projection_type": "KEYS_ONLY"}
    if projections == "all":
        return {"projection_type": "ALL"}
    return {"projection_type": "INCLUDE", "non_key_attributes": projections}


def _build_indexes(config: "DynamoTableConfig") -> tuple[list[dict], list[dict]]:
    """Build Pulumi index configurations."""
    local_indexes = []
    for name, index in config.local_indexes.items():
        idx = index if isinstance(index, LocalIndex) else LocalIndex(**index)
        local_indexes.append(
            {"name": name, "range_key": idx.sort_key, **_convert_projection(idx.projections)}
        )

    global_indexes = []
    for name, index in config.global_indexes.items():
        idx = index if isinstance(index, GlobalIndex) else GlobalIndex(**index)
        global_dict = {
            "name": name,
            "hash_key": idx.partition_key,
            **_convert_projection(idx.projections),
        }
        if idx.sort_key:
            global_dict["range_key"] = idx.sort_key
        global_indexes.append(global_dict)

    return local_indexes, global_indexes


FieldTypeLiteral = Literal["S", "N", "B", "string", "number", "binary"]

StreamViewLiteral = Literal["keys-only", "new-image", "old-image", "new-and-old-images"]


class FieldType(Enum):
    STRING = "S"
    NUMBER = "N"
    BINARY = "B"


class StreamView(Enum):
    KEYS_ONLY = "KEYS_ONLY"
    NEW_IMAGE = "NEW_IMAGE"
    OLD_IMAGE = "OLD_IMAGE"
    NEW_AND_OLD_IMAGES = "NEW_AND_OLD_IMAGES"


class LocalIndexDict(TypedDict, total=False):
    sort_key: str
    projections: list[str] | Literal["keys-only", "all"]


class GlobalIndexDict(TypedDict, total=False):
    partition_key: str
    sort_key: str
    projections: list[str] | Literal["keys-only", "all"]


@dataclass(frozen=True)
class LocalIndex:
    sort_key: str
    projections: list[str] | Literal["keys-only", "all"] = "keys-only"


@dataclass(frozen=True)
class GlobalIndex:
    partition_key: str
    sort_key: str | None = None
    projections: list[str] | Literal["keys-only", "all"] = "keys-only"


class DynamoTableConfigDict(TypedDict, total=False):
    fields: dict[str, FieldType | FieldTypeLiteral]
    partition_key: str
    sort_key: str
    local_indexes: dict[str, LocalIndex | LocalIndexDict]
    global_indexes: dict[str, GlobalIndex | GlobalIndexDict]
    stream: StreamView | StreamViewLiteral


@dataclass(frozen=True, kw_only=True)
class DynamoTableConfig:
    fields: dict[str, FieldType | FieldTypeLiteral]
    partition_key: str
    sort_key: str | None = None
    local_indexes: dict[str, LocalIndex | LocalIndexDict] = field(default_factory=dict)
    global_indexes: dict[str, GlobalIndex | GlobalIndexDict] = field(default_factory=dict)
    stream: StreamView | StreamViewLiteral | None = None

    @property
    def normalized_fields(self) -> dict[str, Literal["S", "N", "B"]]:
        """Fields with normalized DynamoDB types."""
        return {k: self._normalize_type(v) for k, v in self.fields.items()}

    def _normalize_type(self, field_type: FieldType | FieldTypeLiteral) -> Literal["S", "N", "B"]:
        """Normalize field type to DynamoDB format."""
        if isinstance(field_type, FieldType):
            return field_type.value

        mapping = {"string": "S", "number": "N", "binary": "B"}
        return mapping.get(field_type.lower(), field_type.upper())

    @property
    def stream_enabled(self) -> bool:
        return self.stream is not None

    @property
    def normalized_stream_view_type(
        self,
    ) -> Literal["KEYS_ONLY", "NEW_IMAGE", "OLD_IMAGE", "NEW_AND_OLD_IMAGES"] | None:
        """Stream view type in DynamoDB format."""
        if self.stream is None:
            return None

        if isinstance(self.stream, StreamView):
            return self.stream.value

        # Convert kebab-case to DynamoDB format
        mapping = {
            "keys-only": "KEYS_ONLY",
            "new-image": "NEW_IMAGE",
            "old-image": "OLD_IMAGE",
            "new-and-old-images": "NEW_AND_OLD_IMAGES",
        }
        return mapping[self.stream]

    def __post_init__(self) -> None:
        if self.partition_key not in self.fields:
            raise ValueError(f"partition_key '{self.partition_key}' not in fields list")

        if self.sort_key and self.sort_key not in self.fields:
            raise ValueError(f"sort_key '{self.sort_key}' not in fields list")

        # Validate local index fields
        for index_name, index in self.local_indexes.items():
            # Convert to dataclass for validation if needed
            local_index = index if isinstance(index, LocalIndex) else LocalIndex(**index)
            if local_index.sort_key not in self.fields:
                raise ValueError(
                    f"Local index '{index_name}' "
                    f"sort_key '{local_index.sort_key}' not in fields list"
                )

        # Validate global index fields
        for index_name, index in self.global_indexes.items():
            # Convert to dataclass for validation if needed
            global_index = index if isinstance(index, GlobalIndex) else GlobalIndex(**index)
            if global_index.partition_key not in self.fields:
                raise ValueError(
                    f"Global index '{index_name}' "
                    f"partition_key '{global_index.partition_key}' not in fields list"
                )

            if global_index.sort_key and global_index.sort_key not in self.fields:
                raise ValueError(
                    f"Global index '{index_name}' "
                    f"sort_key '{global_index.sort_key}' not in fields list"
                )


@dataclass(frozen=True)
class DynamoTableResources:
    table: Table


@final
class DynamoTable(Component[DynamoTableResources], Linkable):
    def __init__(
        self,
        name: str,
        *,
        config: DynamoTableConfig | None = None,
        **opts: Unpack[DynamoTableConfigDict],
    ):
        super().__init__(name)

        if config is not None:
            self._config = config
        else:
            self._config = DynamoTableConfig(**opts)

        self._resources = None

    @property
    def partition_key(self) -> str:
        return self._config.partition_key

    @property
    def sort_key(self) -> str | None:
        return self._config.sort_key

    @property
    def arn(self) -> Output[str]:
        return self.resources.table.arn

    @property
    def stream_arn(self) -> Output[str] | None:
        return self.resources.table.stream_arn if self._config.stream_enabled else None

    def _create_resources(self) -> DynamoTableResources:
        local_indexes, global_indexes = _build_indexes(self._config)

        table = Table(
            context().prefix(self.name),
            billing_mode="PAY_PER_REQUEST",
            hash_key=self.partition_key,
            range_key=self.sort_key,
            attributes=[{"name": k, "type": v} for k, v in self._config.normalized_fields.items()],
            local_secondary_indexes=local_indexes or None,
            global_secondary_indexes=global_indexes or None,
            stream_enabled=self._config.stream_enabled,
            stream_view_type=self._config.normalized_stream_view_type,
        )
        pulumi.export(f"dynamotable_{self.name}_arn", table.arn)
        pulumi.export(f"dynamotable_{self.name}_name", table.name)
        if self._config.stream_enabled:
            pulumi.export(f"dynamotable_{self.name}_stream_arn", table.stream_arn)
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
            # Main table permissions - full CRUD
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
            ),
            # Index permissions - read only (Query/Scan only, no GetItem/writes)
            AwsPermission(
                actions=[
                    "dynamodb:Query",
                    "dynamodb:Scan",
                ],
                resources=[table.arn.apply(lambda arn: f"{arn}/index/*")],
            ),
        ],
    )
