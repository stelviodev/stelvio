import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pulumi.automation import EngineEvent, OpType
from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.spinner import Spinner
from rich.text import Text

logger = logging.getLogger(__name__)


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


# Default config for unknown operations
DEFAULT_OPERATION_CONFIG = {
    "preview": ("| ", "processing"),
    "active": ("| ", "processing"),
    "completed": ("| ", "processed"),
    "color": "yellow",
}

# Operation display configuration
OPERATION_CONFIG = {
    OpType.CREATE: {
        "preview": ("+ ", "to create"),
        "active": ("| ", "creating"),
        "completed": ("✓ ", "created"),
        "color": "green",
    },
    OpType.DELETE: {
        "preview": ("- ", "to delete"),
        "active": ("| ", "deleting"),
        "completed": ("✓ ", "deleted"),
        "color": "red",
    },
    OpType.UPDATE: {
        "preview": ("~ ", "to update"),
        "active": ("| ", "updating"),
        "completed": ("✓ ", "updated"),
        "color": "yellow",
    },
    OpType.REPLACE: {
        "preview": ("± ", "to replace"),
        "active": ("| ", "replacing"),
        "completed": ("✓ ", "replaced"),
        "color": "blue",
    },
    OpType.CREATE_REPLACEMENT: {
        "preview": ("± ", "to swap"),
        "active": ("| ", "swapping"),
        "completed": ("✓ ", "swapped"),
        "color": "blue",
    },
    OpType.SAME: {
        "static": ("~ ", "unchanged"),
        "color": "dim",
    },
    OpType.REFRESH: {
        "preview": ("~ ", "to refresh"),
        "active": ("| ", "refreshing"),
        "completed": ("✓ ", "refreshed"),
        "color": "sea_green3",
    },
}


def _extract_logical_name(urn: str) -> str:
    # URN format: urn:pulumi:stack::project::type::name
    # We want the 'name' part.
    try:
        parts = urn.split("::")
        if len(parts) >= 4:
            return parts[-1]
        return urn
    except Exception:
        return urn


def _calculate_duration(resource: ResourceInfo) -> str:
    if not resource.start_time:
        return ""

    if resource.end_time:
        duration = resource.end_time - resource.start_time
    else:
        duration = datetime.now().timestamp() - resource.start_time

    return f"({duration:.1f}s)"


def _format_resource_line(
    resource: ResourceInfo, is_preview: bool, duration_str: str = ""
) -> Text:
    # Handle failed state first
    if resource.status == "failed":
        prefix, verb = "✗ ", "failed"
        status_color = "red"
    else:
        # Get operation config with fallback
        op_config = OPERATION_CONFIG.get(resource.operation, DEFAULT_OPERATION_CONFIG)

        # For SAME operation, it's always the same
        if "static" in op_config:
            prefix, verb = op_config["static"]
        elif is_preview:
            prefix, verb = op_config["preview"]
        elif resource.status == "active":
            prefix, verb = op_config["active"]
        else:  # completed (both summary and live view)
            prefix, verb = op_config["completed"]

        status_color = op_config["color"]

    # Pad verb to align resource names
    # Longest verbs: "to replace" (10), "refreshing" (10), "processing" (10)
    verb_padded = verb.ljust(10)

    line = Text()
    line.append(f"{prefix}{verb_padded} ", style=status_color)
    line.append(f"{resource.logical_name}", style="bold")
    line.append(" → ", style="dim")
    line.append(f"{resource.type}", style="dim")

    # Show change summary for drift detection
    if resource.change_summary:
        line.append(f" ({resource.change_summary})", style="dim")

    # Show error message for failed resources
    if resource.error:
        line.append(f" - {resource.error}", style="red")

    # Timing in operation color
    if duration_str:
        line.append(f" {duration_str}", style=status_color)

    return line


def _count_operations(resources: dict[str, ResourceInfo]) -> dict:
    operation_counts = {}

    for resource in resources.values():
        # Don't count failed resources in operation counts
        if resource.status == "failed":
            continue
        operation_counts[resource.operation] = operation_counts.get(resource.operation, 0) + 1

    return operation_counts


def _get_total_duration(start_time: datetime) -> tuple[int, int]:
    """Calculate elapsed time from start_time to now."""
    duration = datetime.now() - start_time
    total_seconds = int(duration.total_seconds())
    minutes = total_seconds // 60
    seconds = total_seconds % 60
    return minutes, seconds


class RichDeploymentHandler:
    """Handle Pulumi events with Rich live updates and resource grouping.

    Pulumi Engine Event System Overview:

    Event Flow: PreludeEvent → [ResourcePreEvent → ResOutputsEvent/ResOpFailedEvent]*
                → SummaryEvent

    Operations by Command:

    DEPLOY (up):
      - ResourcePreEvent (planning=False, op=CREATE/UPDATE/REPLACE) → operation starts
      - ResOutputsEvent → operation completed successfully
      - ResOpFailedEvent → operation failed
      - Timing: Use event.timestamp to calculate duration between Pre and Output events

    PREVIEW (preview):
      - ResourcePreEvent (planning=True) → shows planned changes
      - No ResOutputsEvent (nothing actually happens)
      - detailed_diff shows property-level changes

    DESTROY (destroy):
      - ResourcePreEvent (op=DELETE) → deletion starts
      - ResOutputsEvent → resource deleted successfully
      - ResOpFailedEvent → deletion failed (resource may still exist)
      - Resources deleted in reverse dependency order

    REFRESH (refresh):
      - ResourcePreEvent (op=REFRESH) → reading cloud state
      - ResOutputsEvent → local state synchronized with cloud
      - Updates state without changing actual resources

    Key Event Properties:
      - event.resource_pre_event.metadata.op: Operation type (CREATE, UPDATE, DELETE, etc.)
      - event.resource_pre_event.metadata.urn: Resource identifier
      - event.resource_pre_event.metadata.type: AWS resource type (aws:lambda:Function)
      - event.timestamp: Unix timestamp for duration calculations
    """

    def __init__(
        self,
        app_name: str,
        environment: str,
        operation: Literal["deploy", "preview", "refresh", "destroy"],
    ):
        self.app_name = app_name
        self.environment = environment
        self.console = Console()
        self.start_time = datetime.now()

        # Resource tracking
        self.resources: dict[str, ResourceInfo] = {}
        self.resource_order: list[str] = []  # Maintain order for display
        self.total_resources = 0
        self.completed_count = 0
        self.failed_count = 0

        # Operation state
        self.is_preview = operation == "preview"
        self.is_destroy = operation == "destroy"
        self.operation = operation

        # For spinner text, use different verbs
        self.spinner_operation = {
            "deploy": "Deploying",
            "preview": "Analyzing differences",
            "refresh": "Refreshing",
            "destroy": "Destroying",
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
        }[operation]

        # Always start live display immediately to show spinner
        self.live_started = True
        self.live.start()

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

        # Determine operation verb
        op_map = {
            OpType.CREATE: "Creating",
            OpType.UPDATE: "Updating",
            OpType.DELETE: "Deleting",
            OpType.REPLACE: "Replacing",
            OpType.CREATE_REPLACEMENT: "Swapping",
            OpType.REFRESH: "Refreshing",
            OpType.READ: "Reading",
            OpType.IMPORT: "Importing",
            OpType.SAME: "Unchanged" if self.is_preview else "Skipping",
        }

        # Track the resource
        self.resources[metadata.urn] = ResourceInfo(
            logical_name=logical_name,
            type=metadata.type,
            operation=metadata.op,
            status="active",
            start_time=event.timestamp,
        )
        self.resource_order.append(metadata.urn)
        self.total_resources += 1

    def _handle_res_outputs(self, event: EngineEvent) -> None:
        """Handle ResOutputsEvent - resource operation completed."""
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
                elif len(diffs) <= 3:
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
            # Resource might not be tracked yet, create it ONCE
            elif urn not in self.resource_order:
                # Extract type from URN if possible
                resource_type = "unknown"
                parts = urn.split("::")
                if len(parts) >= 3:
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
                self.resource_order.append(urn)
                self.total_resources += 1
                self.failed_count += 1
            else:
                # Update error message if we get another diagnostic for same resource
                self.resources[urn].error = diagnostic.message

    def _handle_summary(self) -> None:
        # Stop live display
        if self.live_started:
            self.live.stop()

        # Empty line before summary
        self.console.print()

        # Show resources summary if any
        if self.total_resources > 0:
            self._print_resources_summary()

        # Always show completion message
        minutes, seconds = _get_total_duration(self.start_time)
        time_str = f"{minutes}m {seconds}s" if minutes > 0 else f"{seconds}s"

        if self.failed_count > 0:
            self.console.print(f"\n✗ {self.completion_verb} in {time_str} with errors")
        else:
            self.console.print(f"\n✓ {self.completion_verb} in {time_str}")

        # Show operation counts if we have resources
        if self.total_resources > 0:
            counts_text = self._build_operation_counts_text()
            if counts_text:
                self.console.print(counts_text)

    def _render(self) -> RenderableType:
        content = Text()

        if len(self.resources) > 0:
            content.append("\n")

        changing_resources, unchanged_resources, failed_resources = self._group_resources()

        # Show changing resources first
        for resource in changing_resources:
            duration_str = _calculate_duration(resource) if not self.is_preview else ""
            line = _format_resource_line(
                resource, is_preview=self.is_preview, duration_str=duration_str
            )
            content.append(line)
            content.append("\n")

        # Show unchanged resources
        for resource in unchanged_resources:
            line = _format_resource_line(resource, is_preview=self.is_preview)
            content.append(line)
            content.append("\n")

        # Show failed resources last
        for resource in failed_resources:
            duration_str = _calculate_duration(resource) if not self.is_preview else ""
            line = _format_resource_line(
                resource, is_preview=self.is_preview, duration_str=duration_str
            )
            content.append(line)
            content.append("\n")

        # Progress footer with spinner
        minutes, seconds = _get_total_duration(self.start_time)
        total_seconds = minutes * 60 + seconds

        if self.total_resources > 0:
            # Show progress when we have resources
            progress = (
                f"{self.completed_count + self.failed_count}/{self.total_resources} complete"
            )

            # Add spinner if operations are active or at start
            active_count = sum(1 for r in self.resources.values() if r.status == "active")
            if active_count > 0 or self.completed_count == 0:
                # Put spinner with progress text and operation type
                progress_text = f"{self.spinner_operation}  {progress}  {total_seconds}s"
                self.spinner.update(text=progress_text, style="cyan")
                return Group(content, self.spinner)
            content.append(f"{progress}  {total_seconds}s", style="dim")
        else:
            # No resources yet - show spinner with operation text
            progress_text = f"{self.spinner_operation}  {total_seconds}s"
            self.spinner.update(text=progress_text, style="cyan")
            return Group(content, self.spinner)

        return content

    def _group_resources(
        self,
    ) -> tuple[list[ResourceInfo], list[ResourceInfo], list[ResourceInfo]]:
        changing_resources = []
        unchanged_resources = []
        failed_resources = []

        for urn in self.resource_order:
            resource = self.resources[urn]
            if resource.status == "failed":
                failed_resources.append(resource)
            elif resource.operation == OpType.SAME:
                unchanged_resources.append(resource)
            else:
                changing_resources.append(resource)

        return changing_resources, unchanged_resources, failed_resources

    def _print_resources_summary(self) -> None:
        """Print all resources in the final summary."""
        changing_resources, unchanged_resources, failed_resources = self._group_resources()

        for resources in [changing_resources, unchanged_resources, failed_resources]:
            for resource in resources:
                # Add timing for changing resources in non-preview mode
                duration_str = ""
                if not self.is_preview and resource in changing_resources:
                    duration_str = _calculate_duration(resource)

                line = _format_resource_line(resource, self.is_preview, duration_str)
                self.console.print(line)

    def _build_operation_counts_text(self) -> Text | None:
        operation_counts = _count_operations(self.resources)

        # Define tense mappings
        if self.is_preview:
            tense_map = {
                OpType.CREATE: "to create",
                OpType.UPDATE: "to update",
                OpType.DELETE: "to delete",
                OpType.REPLACE: "to replace",
                OpType.CREATE_REPLACEMENT: "to replace",
                OpType.SAME: "unchanged",
            }
        else:
            tense_map = {
                OpType.CREATE: "created",
                OpType.UPDATE: "updated",
                OpType.DELETE: "deleted",
                OpType.REPLACE: "replaced",
                OpType.CREATE_REPLACEMENT: "replaced",
                OpType.SAME: "unchanged",
                OpType.REFRESH: "refreshed",
            }

        # Style mappings from operation config
        style_map = {op: f"bold {config['color']}" for op, config in OPERATION_CONFIG.items()}

        # Build operation parts
        operation_parts = []
        for op, verb in tense_map.items():
            if op in operation_counts and operation_counts[op] > 0:
                count_part = Text(str(operation_counts[op]), style=(style_map.get(op, "bold")))
                verb_part = Text(f" {verb}", style="")
                operation_parts.append(Text.assemble(count_part, verb_part))

        # Add failed count if any
        if self.failed_count > 0:
            count_part = Text(str(self.failed_count), style="bold red")
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
