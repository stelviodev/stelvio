from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, cast

from pulumi.automation import DiffKind
from rich.text import Text

if TYPE_CHECKING:
    from stelvio.rich_deployment_handler import JsonValue, ResourceInfo

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
_REPLACE_KINDS = frozenset(
    {
        DiffKind.ADD_REPLACE,
        DiffKind.UPDATE_REPLACE,
        DiffKind.DELETE_REPLACE,
    }
)

type DiffPathPart = str | int


def _value_limits_for_width(line_width: int | None, indent: int) -> tuple[int, int]:
    """Compute update/detail truncation limits from terminal width."""
    if line_width is None or line_width <= 0:
        return MAX_UPDATE_VALUE_LENGTH, MAX_DETAIL_VALUE_LENGTH

    detail_prefix = (indent + 2) * 4 + 7
    detail_len = max(18, min(160, line_width - detail_prefix - 2))

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


def format_property_diff_lines(
    resource: ResourceInfo, indent: int = 1, line_width: int | None = None
) -> list[Text]:
    """Format property-level diff lines for a resource in preview/diff output."""
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


def _split_property_path(path: str) -> list[str | int]:
    """Split Pulumi property paths like `tags.Name` or `Statement[0].Resource`."""
    parts: list[str | int] = []
    for segment in path.split("."):
        if not segment:
            continue
        token_start = 0
        for match in re.finditer(r"\[(\d+)\]", segment):
            if match.start() > token_start:
                parts.append(segment[token_start : match.start()])
            parts.append(int(match.group(1)))
            token_start = match.end()
        if token_start < len(segment):
            parts.append(segment[token_start:])
    return parts


def _get_nested_value(inputs: dict[str, JsonValue] | None, path: str) -> JsonValue:
    """Get a value from a nested dict using a dot-separated or bracket path."""
    if inputs is None:
        return None
    if path in inputs:
        return inputs[path]

    current: JsonValue = inputs
    for part in _split_property_path(path):
        if isinstance(part, int):
            if isinstance(current, list) and 0 <= part < len(current):
                current = current[part]
            else:
                return None
        elif isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None
    return current
