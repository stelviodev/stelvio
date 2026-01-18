import logging
from collections import Counter
from collections.abc import MutableMapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pulumi.automation import EngineEvent, OpType, OutputValue
from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.spinner import Spinner
from rich.text import Text

logger = logging.getLogger(__name__)

# Constants for URN parsing
MIN_URN_PARTS_FOR_NAME = 4  # urn:pulumi:stack::project::type::name
MIN_URN_PARTS_FOR_TYPE = 3  # urn:pulumi:stack::project::type
MAX_DIFFS_TO_SHOW = 3  # Maximum number of diff properties to show individually


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


def _calculate_duration(resource: ResourceInfo) -> str:
    if not resource.start_time:
        return ""

    end_time = resource.end_time or datetime.now().timestamp()
    return f"({end_time - resource.start_time:.1f}s)"


def format_resource_line(resource: ResourceInfo, is_preview: bool, duration_str: str = "") -> Text:
    """Format a single resource line for display."""
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


def count_operations(resources: dict[str, ResourceInfo]) -> dict:
    """Count operations by type, excluding failed resources."""
    return Counter(
        resource.operation for resource in resources.values() if resource.status != "failed"
    )


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


def build_operation_counts_text(
    resources: dict[str, ResourceInfo], failed_count: int, is_preview: bool
) -> Text | None:
    """Build the operation counts summary text."""
    operation_counts = count_operations(resources)

    # Define tense mappings
    if is_preview:
        tense_map = {
            OpType.CREATE: "to create",
            OpType.UPDATE: "to update",
            OpType.DELETE: "to delete",
            OpType.DISCARD: "to discard",
            OpType.REPLACE: "to replace",
            OpType.CREATE_REPLACEMENT: "to replace",
            OpType.SAME: "unchanged",
        }
    else:
        tense_map = {
            OpType.CREATE: "created",
            OpType.UPDATE: "updated",
            OpType.DELETE: "deleted",
            OpType.DISCARD: "discarded",
            OpType.REPLACE: "replaced",
            OpType.CREATE_REPLACEMENT: "replaced",
            OpType.SAME: "unchanged",
            OpType.REFRESH: "refreshed",
        }

    # Style mappings using the new function
    style_map = {
        OpType.CREATE: "bold green",
        OpType.UPDATE: "bold yellow",
        OpType.DELETE: "bold red",
        OpType.DISCARD: "bold red",
        OpType.REPLACE: "bold blue",
        OpType.CREATE_REPLACEMENT: "bold blue",
        OpType.SAME: "bold dim",
        OpType.REFRESH: "bold sea_green3",
    }

    # Build operation parts
    operation_parts = []
    for op, verb in tense_map.items():
        if op in operation_counts and operation_counts[op] > 0:
            count_part = Text(str(operation_counts[op]), style=(style_map.get(op, "bold")))
            verb_part = Text(f" {verb}", style="")
            operation_parts.append(Text.assemble(count_part, verb_part))

    # Add failed count if any
    if failed_count > 0:
        count_part = Text(str(failed_count), style="bold red")
        verb_part = Text(" failed", style="")
        operation_parts.append(Text.assemble(count_part, verb_part))

    if not operation_parts:
        return None

    # Create combined text with commas
    final_text = Text("  ")  # Indent
    for i, part in enumerate(operation_parts):
        if i > 0:
            final_text.append(", ")
        final_text.append(part)
    return final_text


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

    def __init__(
        self,
        app_name: str,
        environment: str,
        operation: Literal["deploy", "preview", "refresh", "destroy", "outputs"],
        show_unchanged: bool = False,
        dev_mode: bool = False,
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

        # Operation state
        self.is_preview = operation == "preview"
        self.is_destroy = operation == "destroy"
        self.operation = operation
        self.show_unchanged = show_unchanged
        self.dev_mode = dev_mode

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

        # Extract logical name from URN
        logical_name = _extract_logical_name(metadata.urn)

        # Track the resource
        self.resources[metadata.urn] = ResourceInfo(
            logical_name=logical_name,
            type=metadata.type,
            operation=metadata.op,
            status="active",
            start_time=event.timestamp,
        )
        self.total_resources += 1

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

            if urn in self.resources:
                self.resources[urn].error = diagnostic.message
                # Mark as failed if we get an error diagnostic
                if self.resources[urn].status != "failed":
                    self.resources[urn].status = "failed"
                    self.failed_count += 1
                self.resources[urn].end_time = event.timestamp
            else:
                # Resource not tracked yet, create it as failed
                resource_type = "unknown"
                parts = urn.split("::")
                if len(parts) >= MIN_URN_PARTS_FOR_TYPE:
                    resource_type = parts[2]
                else:
                    logger.info("Couldn't parse type from urn: %s", urn)

                self.resources[urn] = ResourceInfo(
                    logical_name=logical_name,
                    type=resource_type,
                    operation=OpType.CREATE,  # Assume create
                    status="failed",
                    start_time=event.timestamp,
                    end_time=event.timestamp,
                    error=diagnostic.message,
                )
                self.total_resources += 1
                self.failed_count += 1

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

    # Unused method kept for reference
    # def show_cleanup_spinner(self) -> Status:
    #     self.console.print()
    #     return self.console.status("Finalizing...", spinner="dots")

    def _render(self) -> RenderableType:
        content = Text()

        if len(self.resources) > 0:
            content.append("\n")

        changing_resources, unchanged_resources, failed_resources = group_resources(self.resources)

        # Show changing resources first
        for resource in changing_resources:
            duration_str = _calculate_duration(resource) if not self.is_preview else ""
            line = format_resource_line(resource, self.is_preview, duration_str)
            content.append(line)
            content.append("\n")

        # Show unchanged resources only if requested
        if self.show_unchanged:
            for resource in unchanged_resources:
                line = format_resource_line(resource, self.is_preview)
                content.append(line)
                content.append("\n")

        # Show failed resources last
        for resource in failed_resources:
            duration_str = _calculate_duration(resource) if not self.is_preview else ""
            line = format_resource_line(resource, self.is_preview, duration_str)
            content.append(line)
            content.append("\n")

        # Progress footer with spinner
        minutes, seconds = get_total_duration(self.start_time)
        total_seconds = minutes * 60 + seconds

        if self.total_resources > 0:
            # Show progress when we have resources
            progress = (
                f"{self.completed_count + self.failed_count}/{self.total_resources} complete"
            )
            progress_text = f"{self.spinner_operation}  {progress}  {total_seconds}s"
        else:
            # No resources yet - show spinner with operation text
            progress_text = f"{self.spinner_operation}  {total_seconds}s"

        self.spinner.update(text=progress_text, style="cyan")
        return Group(content, self.spinner)

    def _print_resources_summary(self) -> None:
        """Print all resources in the final summary."""
        changing_resources, unchanged_resources, failed_resources = group_resources(self.resources)

        # Choose which resource groups to show based on show_unchanged flag
        resource_groups = [changing_resources, failed_resources]
        if self.show_unchanged:
            resource_groups.insert(1, unchanged_resources)  # Insert between changing and failed

        if not any(resource_groups):
            # Only show "Nothing to deploy" if there are actually no errors
            if not self.error_diagnostics:
                message = "No differences found" if self.is_preview else "Nothing to deploy"
                self.console.print(message)
        else:
            for resources in resource_groups:
                for resource in resources:
                    # Add timing for changing resources in non-preview mode
                    duration_str = ""
                    if not self.is_preview and resource in changing_resources:
                        duration_str = _calculate_duration(resource)

                    line = format_resource_line(resource, self.is_preview, duration_str)
                    self.console.print(line)

    def show_completion(self, outputs: MutableMapping[str, OutputValue] | None = None) -> None:
        """Show outputs and final completion message."""
        # Stop cleanup spinner if running
        if self.cleanup_status is not None:
            self.cleanup_status.stop()

        # Show outputs if any
        if outputs:
            for line in format_outputs(outputs):
                self.console.print(line)

        # Show completion message with timing
        minutes, seconds = get_total_duration(self.start_time)
        time_str = f"{minutes}m {seconds}s" if minutes > 0 else f"{seconds}s"

        status_icon, error_suffix = ("✗", "with errors") if self.failed_count > 0 else ("✓", "")
        self.console.print(f"{status_icon} {self.completion_verb} in {time_str}{error_suffix}")

        # Show operation counts if we have resources
        if self.total_resources > 0:
            counts_text = build_operation_counts_text(
                self.resources, self.failed_count, self.is_preview
            )
            if counts_text:
                self.console.print(counts_text)
