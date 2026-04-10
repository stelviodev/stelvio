"""Pure format functions for deployment progress display.

These functions take data in and return Rich Text objects. They have no
state dependencies and are used by RichDeploymentHandler for rendering.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from pulumi.automation import OpType
from rich.text import Text

from stelvio.rich_deployment_model import ComponentInfo, ResourceInfo, _readable_type

if TYPE_CHECKING:
    from collections.abc import MutableMapping

    from pulumi.automation import OutputValue


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
    line.append(f" {component.name}")

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


def build_operation_counts_text(
    total_resources: int, component_count: int, summary_verb: str
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


def build_preview_counts_text(
    resources: dict[str, ResourceInfo], *, component_count: int = 0
) -> Text | None:
    """Build preview summary: '  3 components: 4 to create, 1 to update'."""
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

    text = Text("  ")
    if component_count > 0:
        component_word = "component" if component_count == 1 else "components"
        text.append(f"{component_count} {component_word}: ")

    # Order: create, update, replace, delete
    order = ["to create", "to update", "to replace", "to delete", "to change"]
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
