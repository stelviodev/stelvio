from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Literal, cast

from pulumi.automation import (
    DiffKind,
    EngineEvent,
    OpType,
    OutputValue,
    PropertyDiff,
    StepEventMetadata,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, MutableMapping
from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.spinner import Spinner
from rich.text import Text

logger = logging.getLogger(__name__)

type JsonValue = str | int | float | bool | None | list[JsonValue] | dict[str, JsonValue]

# Constants for URN parsing
MIN_URN_PARTS_FOR_NAME = 4  # urn:pulumi:stack::project::type::name
MIN_URN_PARTS_FOR_TYPE = 3  # urn:pulumi:stack::project::type
MIN_TYPE_SEGMENTS_FOR_PARENT = 2
MAX_DIFFS_TO_SHOW = 3  # Maximum number of diff properties to show individually


STELVIO_TYPE_PREFIX = "stelvio:aws:"

# Human-readable names for common AWS resource types.
# Keys use actual Pulumi type tokens (aws:module/resource:ResourceName format).
RESOURCE_TYPE_NAMES: dict[str, str] = {
    "aws:lambda/function:Function": "Lambda Function",
    "aws:lambda/permission:Permission": "Lambda Permission",
    "aws:lambda/layerVersion:LayerVersion": "Lambda Layer",
    "aws:lambda/eventSourceMapping:EventSourceMapping": "Event Source Mapping",
    "aws:lambda/functionUrl:FunctionUrl": "Lambda URL",
    "aws:iam/role:Role": "IAM Role",
    "aws:iam/rolePolicyAttachment:RolePolicyAttachment": "IAM Policy Attachment",
    "aws:iam/policy:Policy": "IAM Policy",
    "aws:dynamodb/table:Table": "DynamoDB Table",
    "aws:s3/bucketV2:BucketV2": "S3 Bucket",
    "aws:s3/bucketPublicAccessBlock:BucketPublicAccessBlock": "S3 Public Access Block",
    "aws:s3/bucketPolicy:BucketPolicy": "S3 Bucket Policy",
    "aws:s3/bucketNotification:BucketNotification": "S3 Bucket Notification",
    "aws:s3/bucketObjectv2:BucketObjectv2": "S3 Object",
    "aws:s3/bucketCorsConfigurationV2:BucketCorsConfigurationV2": "S3 CORS Config",
    "aws:s3/bucketWebsiteConfigurationV2:BucketWebsiteConfigurationV2": "S3 Website Config",
    "aws:apigatewayv2/api:Api": "API Gateway",
    "aws:apigatewayv2/stage:Stage": "API Stage",
    "aws:apigatewayv2/route:Route": "API Route",
    "aws:apigatewayv2/integration:Integration": "API Integration",
    "aws:apigatewayv2/domainName:DomainName": "API Domain",
    "aws:apigatewayv2/apiMapping:ApiMapping": "API Mapping",
    "aws:sqs/queue:Queue": "SQS Queue",
    "aws:sqs/queuePolicy:QueuePolicy": "SQS Queue Policy",
    "aws:sns/topic:Topic": "SNS Topic",
    "aws:sns/topicSubscription:TopicSubscription": "SNS Subscription",
    "aws:cloudwatch/eventRule:EventRule": "CloudWatch Rule",
    "aws:cloudwatch/eventTarget:EventTarget": "CloudWatch Target",
    "aws:cloudfront/distribution:Distribution": "CloudFront Distribution",
    "aws:cloudfront/originAccessControl:OriginAccessControl": "CloudFront OAC",
    "aws:ses/domainIdentity:DomainIdentity": "SES Domain",
    "aws:ses/domainDkim:DomainDkim": "SES DKIM",
    "aws:acm/certificate:Certificate": "ACM Certificate",
    "aws:acm/certificateValidation:CertificateValidation": "ACM Validation",
    "aws:route53/record:Record": "DNS Record",
    "aws:appconfig/application:Application": "AppConfig Application",
    "aws:appconfig/environment:Environment": "AppConfig Environment",
    "aws:appconfig/configurationProfile:ConfigurationProfile": "AppConfig Profile",
    "aws:appsync/graphQLApi:GraphQLApi": "AppSync API",
}


def _readable_type(resource_type: str) -> str:
    """Get human-readable name for a resource type, or fall back to raw type."""
    return RESOURCE_TYPE_NAMES.get(resource_type, resource_type)


_REPLACE_KINDS = frozenset(
    {
        DiffKind.ADD_REPLACE,
        DiffKind.UPDATE_REPLACE,
        DiffKind.DELETE_REPLACE,
    }
)

# Resource types where replacement can directly destroy persistent user data.
# Maintainers: when introducing new Stelvio components backed by persistent data
# stores, update this allowlist so preview warnings stay accurate (warn only when
# there is likely user data to lose).
_DATA_LOSS_REPLACEMENT_TYPES = frozenset(
    {
        "aws:dynamodb/table:Table",
        "aws:s3/bucketV2:BucketV2",
        "aws:sqs/queue:Queue",
    }
)


@dataclass
class ResourceInfo:
    logical_name: str
    type: str
    operation: OpType
    status: Literal["active", "completed", "failed"]
    start_time: float
    end_time: float | None = None
    error: str | None = None
    change_summary: str | None = None
    detailed_diff: Mapping[str, PropertyDiff] | None = None
    old_inputs: dict[str, JsonValue] | None = None
    new_inputs: dict[str, JsonValue] | None = None

    @property
    def has_replacement(self) -> bool:
        """True if any property diff forces a resource replacement."""
        if self.operation in (OpType.REPLACE, OpType.CREATE_REPLACEMENT):
            return True
        if not self.detailed_diff:
            return False
        return any(pd.diff_kind in _REPLACE_KINDS for pd in self.detailed_diff.values())

    @property
    def has_data_loss_replacement(self) -> bool:
        """True when replacement is likely destructive to persistent data."""
        return self.has_replacement and self.type in _DATA_LOSS_REPLACEMENT_TYPES


@dataclass
class ComponentInfo:
    """Tracks a Stelvio component and its child resources/sub-components."""

    component_type: str  # e.g. "Function", "DynamoTable"
    name: str  # user-given name, e.g. "my-function"
    urn: str
    children: list[ResourceInfo | ComponentInfo]
    start_time: float | None = None

    @property
    def all_resources(self) -> list[ResourceInfo]:
        """Recursively collect all ResourceInfo from this component and sub-components."""
        result = []
        for child in self.children:
            if isinstance(child, ResourceInfo):
                result.append(child)
            else:
                result.extend(child.all_resources)
        return result

    @property
    def status(self) -> Literal["active", "completed", "failed"]:
        if not self.children:
            return "active"
        if any(c.status == "failed" for c in self.children):
            return "failed"
        if any(c.status == "active" for c in self.children):
            return "active"
        return "completed"

    @property
    def operation(self) -> OpType:
        """Derive component operation from children. Highest-priority op wins."""
        if not self.children:
            return OpType.CREATE
        priority = {
            OpType.DELETE: 6,
            OpType.REPLACE: 5,
            OpType.CREATE_REPLACEMENT: 5,
            OpType.CREATE: 4,
            OpType.UPDATE: 3,
            OpType.REFRESH: 2,
            OpType.READ: 1,
            OpType.SAME: 0,
        }
        return max(self.children, key=lambda c: priority.get(c.operation, 0)).operation

    @property
    def end_time(self) -> float | None:
        if self.status == "active":
            return None
        end_times = [c.end_time for c in self.children if c.end_time is not None]
        return max(end_times) if end_times else None

    @property
    def error(self) -> str | None:
        errors = [c.error for c in self.children if c.error]
        return errors[0] if errors else None

    @property
    def has_replacement(self) -> bool:
        """True if any child resource forces a replacement."""
        return any(c.has_replacement for c in self.children if isinstance(c, ResourceInfo))

    @property
    def has_data_loss_replacement(self) -> bool:
        """True if any child replacement is likely destructive to persistent data."""
        return any(
            c.has_data_loss_replacement for c in self.children if isinstance(c, ResourceInfo)
        )

    def preview_summary(self, include_resource_word: bool = False) -> str:
        """Build preview summary counts for component headers."""
        all_res = self.all_resources
        counts: dict[str, int] = {}
        for r in all_res:
            if r.operation == OpType.SAME:
                continue
            if r.has_replacement or r.operation in (OpType.REPLACE, OpType.CREATE_REPLACEMENT):
                label = "to replace"
            elif r.operation == OpType.CREATE:
                label = "to create"
            elif r.operation == OpType.UPDATE:
                label = "to update"
            elif r.operation == OpType.DELETE:
                label = "to delete"
            else:
                label = "to change"
            counts[label] = counts.get(label, 0) + 1
        if not counts:
            return ""
        if include_resource_word:
            return ", ".join(
                f"{n} {'resource' if n == 1 else 'resources'} {label}"
                for label, n in counts.items()
            )
        return ", ".join(f"{n} {label}" for label, n in counts.items())


def _parse_stelvio_parent(parent_urn: str) -> tuple[str, str] | None:
    """Extract (component_type, component_name) from a Stelvio parent URN.

    Returns None if the URN is not a Stelvio component.
    URN format: urn:pulumi:stack::project::stelvio:aws:TypeName::component-name
    Nested URN: urn:pulumi:stack::project::stelvio:aws:Parent$stelvio:aws:Child::name
    """
    parts = parent_urn.split("::")
    if len(parts) < MIN_URN_PARTS_FOR_NAME:
        return None
    type_segment = parts[2]  # e.g. "stelvio:aws:Function"
    if not type_segment.startswith(STELVIO_TYPE_PREFIX):
        return None
    # For nested types like "stelvio:aws:TopicSubscription$stelvio:aws:Function",
    # take the last $-separated segment
    leaf_type = type_segment.rsplit("$", 1)[-1]
    component_type = leaf_type[len(STELVIO_TYPE_PREFIX) :]
    component_name = parts[-1]
    return component_type, component_name


def get_operation_display(
    operation: OpType, status: str, is_preview: bool
) -> tuple[str, str, str]:
    """Get prefix, verb, and color for an operation display."""

    if operation == OpType.SAME:
        return "~ ", "unchanged", "dim"

    if is_preview:
        display_map = {
            OpType.CREATE: ("+ ", "to create", "green"),
            OpType.UPDATE: ("~ ", "to update", "yellow"),
            OpType.DELETE: ("- ", "to delete", "red"),
            OpType.DISCARD: ("- ", "to discard", "red"),
            OpType.REPLACE: ("± ", "to replace", "blue"),
            OpType.CREATE_REPLACEMENT: ("± ", "to swap", "blue"),
            OpType.REFRESH: ("~ ", "to refresh", "sea_green3"),
            OpType.READ: ("~ ", "read", "sea_green3"),
        }
    elif status == "active":
        display_map = {
            OpType.CREATE: ("| ", "creating", "green"),
            OpType.UPDATE: ("| ", "updating", "yellow"),
            OpType.DELETE: ("| ", "deleting", "red"),
            OpType.DISCARD: ("| ", "discarding", "red"),
            OpType.REPLACE: ("| ", "replacing", "blue"),
            OpType.CREATE_REPLACEMENT: ("| ", "swapping", "blue"),
            OpType.REFRESH: ("| ", "refreshing", "sea_green3"),
            OpType.READ: ("| ", "reading", "sea_green3"),
        }
    else:  # completed
        display_map = {
            OpType.CREATE: ("✓ ", "created", "green"),
            OpType.UPDATE: ("✓ ", "updated", "yellow"),
            OpType.DELETE: ("✓ ", "deleted", "red"),
            OpType.DISCARD: ("✓ ", "discarded", "red"),
            OpType.REPLACE: ("✓ ", "replaced", "blue"),
            OpType.CREATE_REPLACEMENT: ("✓ ", "swapped", "blue"),
            OpType.REFRESH: ("✓ ", "refreshed", "sea_green3"),
            OpType.READ: ("✓ ", "read", "sea_green3"),
        }

    return display_map.get(operation, ("| ", "processing", "yellow"))


def _extract_logical_name(urn: str) -> str:
    # URN format: urn:pulumi:stack::project::type::name. We want the 'name' part.
    parts = urn.split("::")
    return parts[-1] if len(parts) >= MIN_URN_PARTS_FOR_NAME else urn


def _extract_type_from_urn(urn: str) -> str:
    """Extract the leaf type token from a Pulumi URN."""
    parts = urn.split("::")
    if len(parts) < MIN_URN_PARTS_FOR_TYPE:
        return "unknown"
    return parts[2].rsplit("$", 1)[-1]


def _extract_parent_component_type_from_urn(urn: str) -> str | None:
    """Extract immediate parent Stelvio component type from a resource URN."""
    parts = urn.split("::")
    if len(parts) < MIN_URN_PARTS_FOR_TYPE:
        return None
    type_path = parts[2]
    segments = type_path.split("$")
    if len(segments) < MIN_TYPE_SEGMENTS_FOR_PARENT:
        return None
    parent_segment = segments[-2]
    if not parent_segment.startswith(STELVIO_TYPE_PREFIX):
        return None
    return parent_segment[len(STELVIO_TYPE_PREFIX) :]


def _calculate_duration(resource: ResourceInfo) -> str:
    if not resource.start_time:
        return ""

    end_time = resource.end_time or datetime.now().timestamp()
    return f"({end_time - resource.start_time:.1f}s)"


def format_resource_line(resource: ResourceInfo, is_preview: bool, duration_str: str = "") -> Text:
    """Format a single resource line for display (flat/legacy mode)."""
    # Handle failed state first
    if resource.status == "failed":
        prefix, verb, color = "✗ ", "failed", "red"
    else:
        prefix, verb, color = get_operation_display(
            resource.operation, resource.status, is_preview
        )

    # Build the formatted line
    verb_padded = verb.ljust(10)  # Align to longest verbs (10 chars)
    line = Text()
    line.append(f"{prefix}{verb_padded} ", style=color)
    line.append(resource.logical_name, style="bold")
    line.append(" → ", style="dim")
    line.append(resource.type, style="dim")

    if resource.change_summary:
        line.append(f" ({resource.change_summary})", style="dim")

    if resource.error:
        line.append(f" - {resource.error}", style="red")

    if duration_str:
        line.append(f" {duration_str}", style=color)

    return line


def _calculate_component_duration(component: ComponentInfo) -> str:
    """Calculate duration string for a component."""
    if not component.start_time:
        return ""
    end = component.end_time
    if end is None:
        end = datetime.now().timestamp()
    return f"({end - component.start_time:.1f}s)"


def format_component_header(
    component: ComponentInfo,
    is_preview: bool,
    duration_str: str = "",
    resource_word_in_preview: bool = False,
) -> Text:
    """Format a component header line.

    Live/completed: ✓ Function  api-handler  (2.1s)
    Preview: + Function  api-handler  (4 to create)
    """
    if component.status == "failed":
        prefix, color = "✗ ", "red"
    else:
        prefix, _, color = get_operation_display(component.operation, component.status, is_preview)

    line = Text()
    line.append(prefix, style=color)
    line.append(component.component_type, style="bold")
    line.append(f"  {component.name}")

    if is_preview:
        summary = component.preview_summary(include_resource_word=resource_word_in_preview)
        if summary:
            line.append(f"  ({summary})", style="dim")
    elif duration_str:
        line.append(f"  {duration_str}", style="dim")

    return line


def format_child_resource_line(
    resource: ResourceInfo, is_preview: bool, duration_str: str = "", indent: int = 1
) -> Text:
    """Format a child resource line (indented under component).

    Shows: `    ✓ Lambda Function (0.8s)`
    """
    if resource.status == "failed":
        prefix, color = "✗ ", "red"
    else:
        prefix, _, color = get_operation_display(resource.operation, resource.status, is_preview)

    line = Text()
    line.append("    " * indent)
    line.append(prefix, style=color)
    line.append(_readable_type(resource.type))

    if resource.change_summary:
        line.append(f" ({resource.change_summary})", style="dim")

    if duration_str:
        line.append(f" {duration_str}", style="dim")

    return line


def format_child_error_line(error: str, indent: int = 1) -> Text:
    """Format an error message indented under a child resource."""
    line = Text()
    line.append("    " * (indent + 1))
    line.append(error, style="red")
    return line


MAX_VALUE_LENGTH = 80  # Truncate displayed values longer than this
MAX_UPDATE_VALUE_LENGTH = 24  # Keep old->new lines compact to avoid wrap spam
MAX_DETAIL_VALUE_LENGTH = 36  # Keep detail lines readable without huge wrap spam
ELLIPSIS = "..."
_MISSING_VALUE = object()
_UNKNOWN_STRING_DISPLAY = "output<string>"
_PREVIEW_FINGERPRINT_PATTERN = re.compile(
    r"(?:[0-9a-f]{24,64}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})",
    re.IGNORECASE,
)


def _value_limits_for_width(line_width: int | None, indent: int) -> tuple[int, int]:
    """Compute update/detail truncation limits from terminal width."""
    if line_width is None or line_width <= 0:
        return MAX_UPDATE_VALUE_LENGTH, MAX_DETAIL_VALUE_LENGTH

    # detail line prefix: spaces + "  old: " / "  new: "
    detail_prefix = (indent + 2) * 4 + 7
    detail_len = max(18, min(160, line_width - detail_prefix - 2))

    # update line has two values + arrow and metadata around it.
    update_budget = max(40, line_width - ((indent + 1) * 4) - 20)
    update_len = max(16, min(80, update_budget // 2))
    return update_len, detail_len


def _truncate_middle(value: str, max_length: int) -> str:
    if len(value) <= max_length:
        return value
    ellipsis_len = len(ELLIPSIS)
    if max_length <= ellipsis_len:
        return ELLIPSIS[:max_length]
    left = (max_length - ellipsis_len) // 2
    right = max_length - ellipsis_len - left
    return f"{value[:left]}{ELLIPSIS}{value[-right:]}"


def _format_value(value: JsonValue, max_length: int = MAX_VALUE_LENGTH) -> str:
    """Format a property value for display, truncating if needed."""
    if value is None:
        return ""
    s = str(value)
    s = re.sub(r"\s+", " ", s).strip()
    return _truncate_middle(s, max_length)


def _try_parse_json_value(value: JsonValue) -> JsonValue | None:
    """Parse a string JSON value into a structured object when possible."""
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate.startswith(("{", "[")):
        return None
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, (dict, list, str, int, float, bool)) or parsed is None:
        return parsed
    return None


type DiffPathPart = str | int


def _collect_diff_paths(
    old_value: object, new_value: object, path: tuple[DiffPathPart, ...] = ()
) -> list[tuple[DiffPathPart, ...]]:
    """Collect all changed paths between two JSON-like structures."""
    if type(old_value) is not type(new_value):
        return [path]

    if isinstance(old_value, dict):
        return _collect_diff_paths_in_dict(
            cast("dict[str, object]", old_value),
            cast("dict[str, object]", new_value),
            path,
        )

    if isinstance(old_value, list):
        return _collect_diff_paths_in_list(
            cast("list[object]", old_value),
            cast("list[object]", new_value),
            path,
        )

    if old_value != new_value:
        return [path]
    return []


def _collect_diff_paths_in_dict(
    old_value: dict[str, object],
    new_value: dict[str, object],
    path: tuple[DiffPathPart, ...],
) -> list[tuple[DiffPathPart, ...]]:
    diffs: list[tuple[DiffPathPart, ...]] = []
    for key in sorted(set(old_value) | set(new_value)):
        if key not in old_value or key not in new_value:
            diffs.append((*path, key))
            continue
        diffs.extend(_collect_diff_paths(old_value[key], new_value[key], (*path, key)))
    return diffs


def _collect_diff_paths_in_list(
    old_value: list[object],
    new_value: list[object],
    path: tuple[DiffPathPart, ...],
) -> list[tuple[DiffPathPart, ...]]:
    diffs: list[tuple[DiffPathPart, ...]] = []
    shared_len = min(len(old_value), len(new_value))
    for idx, (old_item, new_item) in enumerate(
        zip(old_value[:shared_len], new_value[:shared_len], strict=False)
    ):
        diffs.extend(_collect_diff_paths(old_item, new_item, (*path, idx)))

    diffs.extend((*path, idx) for idx in range(shared_len, len(old_value)))
    diffs.extend((*path, idx) for idx in range(shared_len, len(new_value)))
    return diffs


def _format_diff_path(path: tuple[DiffPathPart, ...]) -> str:
    """Format a diff path in dot/index notation."""
    if not path:
        return "<root>"
    result = ""
    for part in path:
        if isinstance(part, int):
            result += f"[{part}]"
        elif result:
            result += f".{part}"
        else:
            result = part
    return result


def _get_value_at_path(value: object, path: tuple[DiffPathPart, ...]) -> object:
    """Resolve a nested value by diff path."""
    current = value
    for part in path:
        if isinstance(part, int):
            if not isinstance(current, list) or part >= len(current):
                return _MISSING_VALUE
            current = current[part]
            continue
        if not isinstance(current, dict):
            return _MISSING_VALUE
        if part not in current:
            return _MISSING_VALUE
        current = current[part]
    return current


def _summarize_dict_change(
    old_value: dict[str, JsonValue], new_value: dict[str, JsonValue]
) -> tuple[str, list[tuple[str, str, JsonValue | None, JsonValue | None]]]:
    """Summarize top-level dictionary differences."""
    old_keys = set(old_value)
    new_keys = set(new_value)
    removed_keys = sorted(old_keys - new_keys)
    added_keys = sorted(new_keys - old_keys)
    changed_keys = sorted(key for key in old_keys & new_keys if old_value[key] != new_value[key])

    parts: list[str] = []
    if changed_keys:
        parts.append(f"{len(changed_keys)} changed")
    if added_keys:
        parts.append(f"{len(added_keys)} added")
    if removed_keys:
        parts.append(f"{len(removed_keys)} removed")

    if not parts:
        return "updated", []

    details: list[tuple[str, str, JsonValue | None, JsonValue | None]] = [
        (key, "changed", old_value.get(key), new_value.get(key)) for key in changed_keys
    ]
    details.extend((key, "added", None, new_value.get(key)) for key in added_keys)
    details.extend((key, "removed", old_value.get(key), None) for key in removed_keys)

    return f"keys: {', '.join(parts)}", details


def _build_changed_detail_block(
    detail_indent: str,
    label: str,
    old_item: object,
    new_item: object,
    detail_value_length: int,
) -> list[Text]:
    """Render a changed leaf as 3 short lines for narrow terminals."""
    header = Text()
    header.append(detail_indent)
    header.append("~ ", style="yellow")
    header.append(label, style="dim")

    old_line = Text()
    old_line.append(detail_indent)
    old_line.append("  old: ", style="dim")
    old_line.append(
        _format_detail_value(
            old_item,
            counterpart=new_item,
            detail_value_length=detail_value_length,
        ),
        style="dim",
    )

    new_line = Text()
    new_line.append(detail_indent)
    new_line.append("  new: ", style="dim")
    new_line.append(
        _format_detail_value(
            new_item,
            counterpart=old_item,
            detail_value_length=detail_value_length,
        ),
        style="dim",
    )

    return [header, old_line, new_line]


def _looks_resource_ref(value: object) -> bool:
    if not isinstance(value, str):
        return False
    return value.startswith(("arn:", "http://", "https://"))


def _looks_preview_fingerprint(value: object) -> bool:
    return isinstance(value, str) and bool(_PREVIEW_FINGERPRINT_PATTERN.fullmatch(value))


def _format_detail_value(
    value: object,
    counterpart: object | None = None,
    detail_value_length: int = MAX_DETAIL_VALUE_LENGTH,
) -> str:
    """Format detail-line values with explicit missing/null markers."""
    if value is _MISSING_VALUE:
        return "<missing>"
    if value is None:
        return "null"
    if _looks_preview_fingerprint(value) and _looks_resource_ref(counterpart):
        return _UNKNOWN_STRING_DISPLAY
    return _format_value(value, detail_value_length)


def _format_update_detail_lines(
    base_indent: str,
    prop_path: str,
    old_value: JsonValue | None,
    new_value: JsonValue | None,
    detail_value_length: int,
) -> tuple[str | None, list[Text]]:
    """Return summary/detail lines for complex update values."""
    detail_indent = f"{base_indent}    "

    if isinstance(old_value, dict) and isinstance(new_value, dict):
        summary, details = _summarize_dict_change(old_value, new_value)
        lines: list[Text] = []
        for key, change_kind, old_item, new_item in details:
            if change_kind == "added":
                line = Text()
                line.append(detail_indent)
                line.append("+ ", style="green")
                added_value = _format_value(new_item, detail_value_length)
                line.append(f"{key} = {added_value}", style="dim")
                lines.append(line)
            elif change_kind == "removed":
                line = Text()
                line.append(detail_indent)
                line.append("- ", style="red")
                removed_value = _format_value(old_item, detail_value_length)
                line.append(f"{key} (was {removed_value})", style="dim")
            else:
                lines.extend(
                    _build_changed_detail_block(
                        detail_indent=detail_indent,
                        label=key,
                        old_item=old_item,
                        new_item=new_item,
                        detail_value_length=detail_value_length,
                    )
                )
                continue
            lines.append(line)
        return summary, lines

    old_json = _try_parse_json_value(old_value)
    new_json = _try_parse_json_value(new_value)
    if old_json is not None and new_json is not None:
        diff_paths = _collect_diff_paths(old_json, new_json)
        if not diff_paths:
            return "JSON updated", []

        detail_lines: list[Text] = []
        for diff_path in diff_paths:
            rendered_path = _format_diff_path(diff_path)
            old_leaf = _get_value_at_path(old_json, diff_path)
            new_leaf = _get_value_at_path(new_json, diff_path)
            detail_lines.extend(
                _build_changed_detail_block(
                    detail_indent=detail_indent,
                    label=rendered_path,
                    old_item=old_leaf,
                    new_item=new_leaf,
                    detail_value_length=detail_value_length,
                )
            )
        return f"JSON changed ({len(diff_paths)} paths)", detail_lines

    # For very long strings and complex structures, avoid opaque old/new blobs.
    old_complex = isinstance(old_value, (dict, list))
    new_complex = isinstance(new_value, (dict, list))
    if old_complex or new_complex:
        return f"value changed ({prop_path})", []

    return None, []


def _build_update_diff_lines(  # noqa: PLR0913
    *,
    base_indent: str,
    prop_path: str,
    old_val: JsonValue | None,
    new_val: JsonValue | None,
    forces_replace: bool,
    update_value_length: int,
    detail_value_length: int,
) -> list[Text]:
    line = Text()
    line.append(base_indent)
    line.append("* ", style="yellow")
    line.append(prop_path)

    if old_val is not None or new_val is not None:
        summary, detail_lines = _format_update_detail_lines(
            base_indent=base_indent,
            prop_path=prop_path,
            old_value=old_val,
            new_value=new_val,
            detail_value_length=detail_value_length,
        )
        if summary is not None:
            line.append(f" ({summary})", style="dim")
            if forces_replace:
                line.append(" (forces replacement)", style="red")
            return [line, *detail_lines]

        old_display = _format_value(old_val, max_length=update_value_length)
        new_display = _format_value(new_val, max_length=update_value_length)
        line.append(f" = {old_display} -> {new_display}", style="dim")

    if forces_replace:
        line.append(" (forces replacement)", style="red")

    return [line]


def _clean_diagnostic_error_message(message: str) -> str:
    """Extract the actionable part of noisy provider diagnostics."""
    text = message.strip()
    if not text:
        return text

    bullet_lines = re.findall(r"(?m)^\s*\*\s+(.+)$", text)
    if bullet_lines:
        text = bullet_lines[-1].strip()

    # Collapse multiline diagnostics to one line for inline display.
    text = re.sub(r"\s+", " ", text).strip()

    # Drop common low-signal provider location prefix, keep actionable message.
    return re.sub(r"^[^:]+:\d+:\s*[^:]+:\s*", "", text)


def format_property_diff_lines(
    resource: ResourceInfo, indent: int = 1, line_width: int | None = None
) -> list[Text]:
    """Format property-level diff lines for a resource in preview/diff output.

    Returns a list of Text lines, each showing a property change:
      + key = value              (added)
      * key = "old" -> "new"     (changed)
      - key                      (removed)
    """
    if not resource.detailed_diff:
        return []

    lines: list[Text] = []
    base_indent = "    " * (indent + 1)
    update_value_length, detail_value_length = _value_limits_for_width(line_width, indent)

    for prop_path, prop_diff in sorted(resource.detailed_diff.items()):
        line = Text()
        line.append(base_indent)

        kind = prop_diff.diff_kind
        forces_replace = kind in _REPLACE_KINDS

        if kind in (DiffKind.ADD, DiffKind.ADD_REPLACE):
            line.append("+ ", style="green")
            line.append(prop_path)
            new_val = _get_nested_value(resource.new_inputs, prop_path)
            if new_val is not None:
                line.append(f" = {_format_value(new_val)}", style="dim")
        elif kind in (DiffKind.UPDATE, DiffKind.UPDATE_REPLACE):
            old_val = _get_nested_value(resource.old_inputs, prop_path)
            new_val = _get_nested_value(resource.new_inputs, prop_path)
            lines.extend(
                _build_update_diff_lines(
                    base_indent=base_indent,
                    prop_path=prop_path,
                    old_val=old_val,
                    new_val=new_val,
                    forces_replace=forces_replace,
                    update_value_length=update_value_length,
                    detail_value_length=detail_value_length,
                )
            )
            continue
        elif kind in (DiffKind.DELETE, DiffKind.DELETE_REPLACE):
            line.append("- ", style="red")
            line.append(prop_path)

        if forces_replace:
            line.append(" (forces replacement)", style="red")

        lines.append(line)

    return lines


def format_replacement_warning(indent: int = 1) -> Text:
    """Format a replacement warning line."""
    line = Text()
    line.append("    " * (indent + 1))
    line.append("!! Replacement recreates resource; data may be lost.", style="red bold")
    return line


def _get_nested_value(inputs: dict[str, JsonValue] | None, path: str) -> JsonValue:
    """Get a value from a nested dict using a dot-separated or bracket path.

    Pulumi property paths can be like 'memorySize', 'tags.Name', etc.
    """
    if inputs is None:
        return None
    if path in inputs:
        return inputs[path]
    parts = path.split(".")
    current: JsonValue = inputs
    for part in parts:
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None
    return current


def get_total_duration(start_time: datetime) -> tuple[int, int]:
    """Calculate elapsed time from start_time to now."""
    duration = datetime.now() - start_time
    total_seconds = int(duration.total_seconds())
    minutes = total_seconds // 60
    seconds = total_seconds % 60
    return minutes, seconds


def group_resources(
    resources: dict[str, ResourceInfo],
) -> tuple[list[ResourceInfo], list[ResourceInfo], list[ResourceInfo]]:
    """Group resources into changing, unchanged, and failed categories."""
    changing_resources, unchanged_resources, failed_resources = [], [], []

    for resource in resources.values():
        if resource.status == "failed":
            failed_resources.append(resource)
        elif resource.operation in (OpType.SAME, OpType.READ):
            unchanged_resources.append(resource)
        else:
            changing_resources.append(resource)

    return changing_resources, unchanged_resources, failed_resources


def group_components(
    components: dict[str, ComponentInfo],
) -> tuple[list[ComponentInfo], list[ComponentInfo], list[ComponentInfo]]:
    """Group components into changing, unchanged, and failed categories."""
    changing, unchanged, failed = [], [], []

    for comp in components.values():
        # Component events can arrive before any child resources. Treat these as
        # unchanged placeholders so they don't flash as "to create" in preview.
        if not comp.children:
            unchanged.append(comp)
            continue
        if comp.status == "failed":
            failed.append(comp)
        elif comp.operation in (OpType.SAME, OpType.READ):
            unchanged.append(comp)
        else:
            changing.append(comp)

    return changing, unchanged, failed


def build_operation_counts_text(
    total_resources: int,
    component_count: int,
    summary_verb: str,
) -> Text | None:
    """Build the operation counts summary text.

    With components: "  3 components (7 resources) deployed"
    No components:   "  7 resources deployed"
    """
    if total_resources == 0:
        return None

    resource_word = "resource" if total_resources == 1 else "resources"
    final_text = Text("  ")  # Indent

    if component_count > 0:
        component_word = "component" if component_count == 1 else "components"
        final_text.append(str(component_count), style="bold")
        final_text.append(f" {component_word} ({total_resources} {resource_word}) {summary_verb}")
    else:
        final_text.append(f"{total_resources} {resource_word} {summary_verb}")

    return final_text


def build_preview_counts_text(resources: dict[str, ResourceInfo]) -> Text | None:
    """Build preview summary: '  4 to create, 1 to update, 2 to delete'."""
    counts: dict[str, int] = {}
    op_labels = {
        OpType.CREATE: "to create",
        OpType.UPDATE: "to update",
        OpType.DELETE: "to delete",
        OpType.REPLACE: "to replace",
        OpType.CREATE_REPLACEMENT: "to replace",
    }
    op_colors = {
        "to create": "green",
        "to update": "yellow",
        "to delete": "red",
        "to replace": "blue",
    }

    for r in resources.values():
        if r.operation == OpType.SAME:
            continue
        label = "to replace" if r.has_replacement else op_labels.get(r.operation, "to change")
        counts[label] = counts.get(label, 0) + 1

    if not counts:
        return None

    # Order: create, update, replace, delete
    order = ["to create", "to update", "to replace", "to delete", "to change"]
    text = Text("  ")
    first = True
    for label in order:
        if label in counts:
            if not first:
                text.append(", ")
            color = op_colors.get(label, "white")
            text.append(str(counts[label]), style=color)
            text.append(f" {label}", style=color)
            first = False
    return text


def format_outputs(outputs: MutableMapping[str, OutputValue]) -> list[str]:
    """Format outputs for display."""
    if not outputs:
        return []

    max_key_length = max(len(key) for key in outputs)
    formatted_lines = ["[bold]Outputs:"]

    for key, output in outputs.items():
        value = output.value if not output.secret else "[secret]"
        key_padded = key.ljust(max_key_length)
        formatted_lines.append(f'    {key_padded}: "{value}"')

    formatted_lines.append("")  # Empty line after outputs
    return formatted_lines


class RichDeploymentHandler:
    """Displays Pulumi deployment progress in the terminal with a live, updating view.

    This handler transforms Pulumi's raw events into a user-friendly display that shows:
    - Which resources are being created, updated, or deleted
    - Real-time progress as operations complete
    - Timing information for each resource
    - Final summary with counts and outputs

    How it works:
    1. Pulumi sends events as it processes each resource (start → complete/fail)
    2. We track each resource's state in a dictionary
    3. A live display refreshes continuously to show current progress
    4. After all resources are done, we show a summary and outputs

    The display looks like:
        | creating    my-function → aws:lambda:Function (2.3s)
        ✓ created    my-bucket → aws:s3:Bucket (1.2s)
        ✗ failed     my-table → aws:dynamodb:Table - Access denied

        Deploying  3/5 complete  4s

    Key concepts:
    - Resources start in "active" state when Pulumi begins processing them
    - They move to "completed" or "failed" when done
    - We maintain display order to show resources consistently
    - Different operations (deploy/preview/destroy) show different UI text
    - The spinner shows overall progress at the bottom

    State tracking:
    - self.resources: Dict of all resources by URN (unique ID)
    - Counters for total, completed, and failed resources
    - Timing tracked via timestamps on each event
    """

    def __init__(  # noqa: PLR0913
        self,
        app_name: str,
        environment: str,
        operation: Literal["deploy", "preview", "refresh", "destroy", "outputs"],
        show_unchanged: bool = False,
        dev_mode: bool = False,
        compact: bool = False,
    ):
        self.app_name = app_name
        self.environment = environment
        self.console = Console()
        self.start_time = datetime.now()

        # Resource tracking
        self.resources: dict[str, ResourceInfo] = {}
        self.total_resources = 0
        self.completed_count = 0
        self.failed_count = 0

        # Component tracking (Phase 1: grouped display)
        self.components: dict[str, ComponentInfo] = {}  # top-level component URN → ComponentInfo
        self._components_by_urn: dict[str, ComponentInfo] = {}  # all component URNs
        self.resource_to_component: dict[str, str] = {}  # resource URN → parent URN
        self.orphan_resources: list[ResourceInfo] = []  # resources with no Stelvio parent

        # Operation state
        self.is_preview = operation == "preview"
        self.is_destroy = operation == "destroy"
        self.operation = operation
        self.show_unchanged = show_unchanged
        self.dev_mode = dev_mode
        self.compact = compact

        # For spinner text, use different verbs
        self.spinner_operation = {
            "deploy": "Deploying",
            "preview": "Analyzing differences",
            "refresh": "Refreshing",
            "destroy": "Destroying",
            "outputs": "Showing outputs",
        }[operation]

        self.live = Live(
            self,
            console=self.console,
            transient=True,
        )
        self.live_started = False

        # Create spinner once for reuse
        self.spinner = Spinner("dots", style="cyan")

        # Completion verb for final message
        self.completion_verb = {
            "deploy": "Deployed",
            "preview": "Analyzed",
            "refresh": "Refreshed",
            "destroy": "Destroyed",
            "outputs": "Shown",
        }[operation]

        # Summary verb for the counts line (lowercase)
        self.summary_verb = {
            "deploy": "deployed",
            "preview": "to deploy",
            "refresh": "refreshed",
            "destroy": "destroyed",
            "outputs": "",
        }[operation]

        # Always start live display immediately to show spinner
        self.live_started = True
        self.live.start()

        # For cleanup spinner
        self.cleanup_status = None
        # Collect error diagnostics for later analysis
        self.error_diagnostics = []

    def __rich__(self) -> RenderableType:
        return self._render()

    def handle_event(self, event: EngineEvent) -> None:
        if not isinstance(event, EngineEvent):
            return

        if event.resource_pre_event:
            self._handle_resource_pre(event)
        elif event.res_outputs_event:
            self._handle_res_outputs(event)
        elif event.res_op_failed_event:
            self._handle_res_op_failed(event)
        elif event.summary_event:
            self._handle_summary()
        elif event.diagnostic_event:
            self._handle_diagnostic(event)

    def _handle_resource_pre(self, event: EngineEvent) -> None:
        metadata = event.resource_pre_event.metadata

        # Skip if already tracking (duplicate events)
        if metadata.urn in self.resources:
            return

        # Skip Pulumi internal resources (stack, providers)
        if (
            metadata.type.startswith("pulumi:")
            or metadata.type.startswith("pulumi:providers:")
            or "pulumi:pulumi:Stack" in metadata.type
        ):
            return

        # Stelvio ComponentResource events: don't track as resources, but register
        # in the component tree so nested components form a proper hierarchy
        if metadata.type.startswith(STELVIO_TYPE_PREFIX):
            parent_urn = self._get_parent_urn(metadata)
            # If this component's parent is also a Stelvio component, nest it
            if parent_urn and _parse_stelvio_parent(parent_urn):
                parent_comp = self._get_or_create_component(parent_urn, event.timestamp)
                child_comp = self._get_or_create_component(metadata.urn, event.timestamp)
                # Only add as child if not already nested
                if child_comp not in parent_comp.children:
                    parent_comp.children.append(child_comp)
                    # Remove from top-level components since it's now nested
                    self.components.pop(metadata.urn, None)
            else:
                # Top-level component, ensure it exists in the registry
                self._get_or_create_component(metadata.urn, event.timestamp)
            return

        # Extract logical name from URN
        logical_name = _extract_logical_name(metadata.urn)

        old_inputs = metadata.old.inputs if metadata.old else None
        new_inputs = metadata.new.inputs if metadata.new else None
        detailed_diff = metadata.detailed_diff

        resource = ResourceInfo(
            logical_name=logical_name,
            type=metadata.type,
            operation=metadata.op,
            status="active",
            start_time=event.timestamp,
            detailed_diff=detailed_diff,
            old_inputs=old_inputs,
            new_inputs=new_inputs,
        )

        # Track the resource
        self.resources[metadata.urn] = resource
        self.total_resources += 1

        # Group under parent Stelvio component if one exists
        parent_urn = self._get_parent_urn(metadata)
        if parent_urn and _parse_stelvio_parent(parent_urn):
            component = self._get_or_create_component(parent_urn, event.timestamp)
            component.children.append(resource)
            self.resource_to_component[metadata.urn] = parent_urn
            return

        # No Stelvio parent — orphan resource
        self.orphan_resources.append(resource)

    def _get_or_create_component(self, urn: str, timestamp: int) -> ComponentInfo:
        """Get an existing component or create a new top-level placeholder."""
        if urn in self._components_by_urn:
            return self._components_by_urn[urn]

        parsed = _parse_stelvio_parent(urn)
        if parsed is None:
            raise ValueError(f"Expected Stelvio component URN, got: {urn}")
        component_type, component_name = parsed
        comp = ComponentInfo(
            component_type=component_type,
            name=component_name,
            urn=urn,
            children=[],
            start_time=timestamp,
        )
        self.components[urn] = comp
        self._components_by_urn[urn] = comp
        return comp

    @staticmethod
    def _get_parent_urn(metadata: StepEventMetadata) -> str | None:
        """Extract parent URN from event metadata."""
        # new.parent is available for resource_pre_event
        if metadata.new and metadata.new.parent:
            return metadata.new.parent
        if metadata.old and metadata.old.parent:
            return metadata.old.parent
        return None

    def describe_urn(self, urn: str) -> str | None:
        """Return user-facing resource/component context for a URN."""
        normalized_urn = urn.strip()

        component = self._components_by_urn.get(normalized_urn)
        if component:
            return f"{component.component_type} {component.name}"

        resource = self.resources.get(normalized_urn)
        if resource:
            resource_name = self._short_resource_name(resource.logical_name)
            resource_label = f"{resource_name} ({_readable_type(resource.type)})"
            component_urn = self.resource_to_component.get(normalized_urn)
            if component_urn:
                parent = self._components_by_urn.get(component_urn)
                if parent:
                    return f"{parent.component_type} {parent.name} → {resource_label}"
            return resource_label

        parsed_component = _parse_stelvio_parent(normalized_urn)
        if parsed_component:
            component_type, component_name = parsed_component
            return f"{component_type} {component_name}"

        if normalized_urn:
            logical_name = self._short_resource_name(_extract_logical_name(normalized_urn))
            type_token = _extract_type_from_urn(normalized_urn)
            return f"{logical_name} ({_readable_type(type_token)})"
        return None

    def _short_resource_name(self, logical_name: str) -> str:
        """Strip app/env prefix from generated resource names when present."""
        prefix = f"{self.app_name}-{self.environment}-"
        if logical_name.startswith(prefix):
            return logical_name[len(prefix) :]
        return logical_name

    def _handle_res_outputs(self, event: EngineEvent) -> None:
        metadata = event.res_outputs_event.metadata
        urn = metadata.urn
        if urn not in self.resources:
            logger.warning("Output event for untracked resource: %s", _extract_logical_name(urn))
            return

        resource = self.resources[urn]
        # During refresh, if the output event shows a non-SAME operation (drift detected),
        # update the resource's operation to reflect the actual change
        if self.operation == "refresh" and metadata.op != OpType.SAME:
            resource.operation = metadata.op

            if diffs := event.res_outputs_event.metadata.diffs:
                if len(diffs) == 1:
                    resource.change_summary = f"{diffs[0]} changed"
                elif len(diffs) <= MAX_DIFFS_TO_SHOW:
                    resource.change_summary = f"{', '.join(diffs)} changed"
                else:
                    resource.change_summary = f"{len(diffs)} properties changed"

        if metadata.new and metadata.new.inputs and not resource.new_inputs:
            resource.new_inputs = metadata.new.inputs
        if metadata.old and metadata.old.inputs and not resource.old_inputs:
            resource.old_inputs = metadata.old.inputs
        # Capture detailed_diff and inputs from the outputs event (Pulumi often
        # populates these here rather than in the pre event)
        if metadata.detailed_diff and not resource.detailed_diff:
            resource.detailed_diff = metadata.detailed_diff

        resource.status = "completed"
        resource.end_time = event.timestamp
        self.completed_count += 1

    def _handle_res_op_failed(self, event: EngineEvent) -> None:
        metadata = event.res_op_failed_event.metadata
        urn = metadata.urn.strip()
        logical_name = _extract_logical_name(urn)

        if urn in self.resources:
            if self.resources[urn].status != "failed":
                self.resources[urn].status = "failed"
                self.failed_count += 1
            self.resources[urn].end_time = event.timestamp
        else:
            logger.warning("Failed event for untracked resource: %s", logical_name)

    def _handle_diagnostic(self, event: EngineEvent) -> None:
        # Store error messages for associated resources
        diagnostic = event.diagnostic_event

        # Collect ALL diagnostic events for error analysis
        if diagnostic.severity == "error":
            self.error_diagnostics.append(diagnostic)

        if diagnostic.urn and diagnostic.severity == "error":
            urn = diagnostic.urn.strip()
            logical_name = _extract_logical_name(urn)
            clean_error = _clean_diagnostic_error_message(diagnostic.message)

            if urn in self.resources:
                self.resources[urn].error = clean_error
                # Mark as failed if we get an error diagnostic
                if self.resources[urn].status != "failed":
                    self.resources[urn].status = "failed"
                    self.failed_count += 1
                self.resources[urn].end_time = event.timestamp
            else:
                self._track_untracked_failed_resource(
                    urn=urn,
                    logical_name=logical_name,
                    clean_error=clean_error,
                    timestamp=event.timestamp,
                )

    def _track_untracked_failed_resource(
        self, *, urn: str, logical_name: str, clean_error: str, timestamp: int
    ) -> None:
        """Create and place a failed resource when no pre/output event was tracked."""
        resource_type = _extract_type_from_urn(urn)
        if resource_type == "unknown":
            logger.info("Couldn't parse type from urn: %s", urn)

        failed_resource = ResourceInfo(
            logical_name=logical_name,
            type=resource_type,
            operation=OpType.CREATE,  # Assume create
            status="failed",
            start_time=timestamp,
            end_time=timestamp,
            error=clean_error,
        )
        self.resources[urn] = failed_resource
        self.total_resources += 1
        self.failed_count += 1

        # Best-effort attach untracked failed resource under matching component
        # so users see the error right below that resource/component context.
        attached_to_component = False
        parent_type = _extract_parent_component_type_from_urn(urn)
        candidate_name = self._short_resource_name(logical_name)
        if parent_type:
            for comp in self._components_by_urn.values():
                if comp.component_type == parent_type and comp.name == candidate_name:
                    if failed_resource not in comp.children:
                        comp.children.append(failed_resource)
                    self.resource_to_component[urn] = comp.urn
                    attached_to_component = True
                    break

        # If we couldn't map the resource to a Stelvio component, show it in
        # "Other resources" so inline failure details remain visible.
        if not attached_to_component and failed_resource not in self.orphan_resources:
            self.orphan_resources.append(failed_resource)

    def _handle_summary(self) -> None:
        # Stop live display completely
        if self.live_started:
            self.live.stop()
            self.live_started = False

        # Empty line before summary
        self.console.print()

        # Show resources summary if any
        if self.total_resources > 0:
            self._print_resources_summary()

        # Only show cleanup spinner if no errors occurred
        if not self.error_diagnostics and not self.dev_mode:
            # Start spinner immediately for cleanup phase
            self.console.print()
            self.cleanup_status = self.console.status("Finalizing...", spinner="dots")
            self.cleanup_status.start()

    def _render(self) -> RenderableType:
        content = Text()

        if self.components or self.resources or self.orphan_resources:
            content.append("\n")

        changing_comps, unchanged_comps, failed_comps = group_components(self.components)

        # Show changing components (expanded with children)
        for comp in changing_comps:
            self._render_component(content, comp, expanded=True)

        # Show unchanged components only if requested (collapsed)
        if self.show_unchanged:
            for comp in unchanged_comps:
                self._render_component(content, comp, expanded=False)

        # Show failed components last (expanded with errors)
        for comp in failed_comps:
            self._render_component(content, comp, expanded=True)

        # Show orphan resources (not part of any Stelvio component)
        if self.orphan_resources:
            self._render_orphan_resources(content)

        # Progress footer with spinner
        minutes, seconds = get_total_duration(self.start_time)
        total_seconds = minutes * 60 + seconds

        completed_components = sum(
            1 for c in self.components.values() if c.status in ("completed", "failed")
        )
        total_components = len(self.components)

        if total_components > 0:
            progress = f"{completed_components}/{total_components} complete"
            progress_text = f"{self.spinner_operation}  {progress}  {total_seconds}s"
        elif self.total_resources > 0:
            # Fallback for orphan-only case
            progress = (
                f"{self.completed_count + self.failed_count}/{self.total_resources} complete"
            )
            progress_text = f"{self.spinner_operation}  {progress}  {total_seconds}s"
        else:
            progress_text = f"{self.spinner_operation}  {total_seconds}s"

        self.spinner.update(text=progress_text, style="cyan")
        return Group(content, self.spinner)

    def _render_component(
        self, content: Text, comp: ComponentInfo, *, expanded: bool, indent: int = 0
    ) -> None:
        """Render a single component into the content Text."""
        duration_str = _calculate_component_duration(comp) if not self.is_preview else ""
        indent_str = "    " * indent

        # Compact preview: header only, no children
        if self.compact and self.is_preview:
            header = format_component_header(
                comp,
                self.is_preview,
                duration_str,
                resource_word_in_preview=True,
            )
            content.append(indent_str)
            content.append(header)
            content.append("\n")
            warning_line = self._compact_preview_replacement_warning(comp, indent)
            if warning_line:
                content.append(warning_line)
                content.append("\n")
            return

        if not expanded or (comp.status == "completed" and not self.is_preview):
            # Collapsed: single header line
            header = format_component_header(comp, self.is_preview, duration_str)
            content.append(indent_str)
            content.append(header)
            content.append("\n")
        else:
            # Expanded: header + children
            header = format_component_header(comp, self.is_preview)
            content.append(indent_str)
            content.append(header)
            content.append("\n")

            self._render_children(content, comp, indent=indent + 1)

    def _iter_preview_resource_lines(self, child: ResourceInfo, indent: int) -> list[Text]:
        """Build preview render lines for a single child resource."""
        lines = [format_child_resource_line(child, self.is_preview, "", indent)]
        if child.detailed_diff:
            lines.extend(
                format_property_diff_lines(child, indent, line_width=self.console.size.width)
            )
        if child.has_data_loss_replacement:
            lines.append(format_replacement_warning(indent))
        if child.error:
            lines.append(format_child_error_line(child.error, indent))
        return lines

    def _compact_preview_replacement_warning(
        self, comp: ComponentInfo, indent: int
    ) -> Text | None:
        """Return compact preview replacement warning for a component when needed."""
        if self.compact and self.is_preview and comp.has_data_loss_replacement:
            return format_replacement_warning(indent)
        return None

    def _render_children(self, content: Text, comp: ComponentInfo, indent: int) -> None:
        """Render children (resources and sub-components) of a component."""
        for child in comp.children:
            if isinstance(child, ComponentInfo):
                self._render_component(content, child, expanded=True, indent=indent)
            elif self.is_preview and child.operation == OpType.SAME and not self.show_unchanged:
                continue
            else:
                if self.is_preview:
                    for line in self._iter_preview_resource_lines(child, indent):
                        content.append(line)
                        content.append("\n")
                    continue

                child_duration = _calculate_duration(child) if not self.is_preview else ""
                line = format_child_resource_line(child, self.is_preview, child_duration, indent)
                content.append(line)
                content.append("\n")

                if child.error:
                    content.append(format_child_error_line(child.error, indent))
                    content.append("\n")

    def _render_orphan_resources(self, content: Text) -> None:
        """Render resources that aren't part of any Stelvio component."""
        content.append("\n")
        content.append("  Other resources\n", style="dim")
        for resource in self.orphan_resources:
            duration_str = _calculate_duration(resource) if not self.is_preview else ""
            line = format_child_resource_line(resource, self.is_preview, duration_str)
            content.append(line)
            content.append("\n")
            if resource.error:
                content.append(format_child_error_line(resource.error))
                content.append("\n")

    def _print_resources_summary(self) -> None:
        """Print all resources in the final summary, grouped by component."""
        changing_comps, unchanged_comps, failed_comps = group_components(self.components)

        comp_groups: list[list[ComponentInfo]] = [changing_comps, failed_comps]
        if self.show_unchanged:
            comp_groups.insert(1, unchanged_comps)

        has_any = any(comp_groups) or self.orphan_resources

        if not has_any:
            if not self.error_diagnostics:
                message = "No differences found" if self.is_preview else "Nothing to deploy"
                self.console.print(message)
        else:
            for comps in comp_groups:
                for comp in comps:
                    self._print_component_summary(comp)

            # Print orphan resources
            if self.orphan_resources:
                self.console.print()
                self.console.print("  Other resources", style="dim")
                for resource in self.orphan_resources:
                    duration_str = _calculate_duration(resource) if not self.is_preview else ""
                    line = format_child_resource_line(resource, self.is_preview, duration_str)
                    self.console.print(line)
                    if resource.error:
                        self.console.print(format_child_error_line(resource.error))

    def _print_component_summary(self, comp: ComponentInfo, indent: int = 0) -> None:
        """Print a single component in the final summary."""
        duration_str = _calculate_component_duration(comp) if not self.is_preview else ""
        indent_str = "    " * indent
        header = format_component_header(
            comp,
            self.is_preview,
            duration_str,
            resource_word_in_preview=self.compact and self.is_preview,
        )
        self.console.print(Text(indent_str) + header)

        if self.compact and self.is_preview:
            warning_line = self._compact_preview_replacement_warning(comp, indent)
            if warning_line:
                self.console.print(warning_line)
            return
        if self.is_preview:
            self._print_preview_children(comp, indent)
            return
        if comp.status == "failed":
            self._print_failed_children(comp, indent)

    def _print_preview_children(self, comp: ComponentInfo, indent: int) -> None:
        """Print children with property diffs for preview/diff summary."""
        for child in comp.children:
            if isinstance(child, ComponentInfo):
                self._print_component_summary(child, indent=indent + 1)
            elif child.operation == OpType.SAME and not self.show_unchanged:
                continue
            else:
                for line in self._iter_preview_resource_lines(child, indent + 1):
                    self.console.print(line)

    def _print_failed_children(self, comp: ComponentInfo, indent: int) -> None:
        """Print failed children with errors for the final summary."""
        for child in comp.children:
            if isinstance(child, ComponentInfo):
                if child.status == "failed":
                    self._print_component_summary(child, indent=indent + 1)
            elif child.status == "failed":
                child_duration = _calculate_duration(child)
                line = format_child_resource_line(
                    child, self.is_preview, child_duration, indent + 1
                )
                self.console.print(line)
                if child.error:
                    self.console.print(format_child_error_line(child.error, indent + 1))

    def show_completion(self, outputs: MutableMapping[str, OutputValue] | None = None) -> None:
        """Show outputs and final completion message."""
        # Stop cleanup spinner if running
        if self.cleanup_status is not None:
            self.cleanup_status.stop()

        # Preview/diff should focus on planned changes, not dump current outputs.
        # Outputs remain visible for deploy/refresh/outputs commands.
        if outputs and not self.is_preview:
            for line in format_outputs(outputs):
                self.console.print(line)

        # Show completion message with timing
        minutes, seconds = get_total_duration(self.start_time)
        time_str = f"{minutes}m {seconds}s" if minutes > 0 else f"{seconds}s"

        status_icon, error_suffix = ("✗", "with errors") if self.failed_count > 0 else ("✓", "")
        self.console.print(f"{status_icon} {self.completion_verb} in {time_str}{error_suffix}")

        # Show operation counts if we have resources
        if self.total_resources > 0:
            if self.is_preview:
                counts_text = build_preview_counts_text(self.resources)
            else:
                counts_text = build_operation_counts_text(
                    total_resources=self.total_resources,
                    component_count=len(self.components),
                    summary_verb=self.summary_verb,
                )
            if counts_text:
                self.console.print(counts_text)
