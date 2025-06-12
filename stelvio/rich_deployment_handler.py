from collections.abc import Callable
from datetime import datetime
from typing import Literal

from pulumi.automation import EngineEvent, OpType
from rich import Console
from rich.console import Group, RenderableType
from rich.live import Live
from rich.pretty import pprint
from rich.spinner import Spinner
from rich.text import Text

from stelvio.pulumi import logger


class LiveRenderable:
    def __init__(self, handler) -> None:
        self.handler = handler

    def __rich__(self) -> RenderableType:
        return self.handler._render()


def create_rich_event_handler(
    app_name: str, environment: str, operation: Literal["deploy", "preview", "refresh", "destroy"]
) -> Callable[[EngineEvent], None]:
    handler = RichDeploymentHandler(app_name, environment, operation)
    return handler.handle_event


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
        """Initialize the handler with app context."""
        self.app_name = app_name
        self.environment = environment
        self.console = Console()
        self.start_time = datetime.now()

        # Resource tracking
        self.resources: dict[str, dict[str, any]] = {}
        self.resource_order: list[str] = []  # Maintain order for display
        self.total_resources = 0
        self.completed_count = 0
        self.failed_count = 0

        # Operation counts for summary
        self.operation_counts = {
            OpType.CREATE: 0,
            OpType.UPDATE: 0,
            OpType.DELETE: 0,
            OpType.REPLACE: 0,
            OpType.CREATE_REPLACEMENT: 0,
            "same": 0,
        }

        # Operation state
        self.is_preview = operation == "preview"
        self.is_destroy = operation == "destroy"
        self.operation_type = {
            "deploy": "Deploying",
            "preview": "Diff for",
            "refresh": "Refreshing",
            "destroy": "Destroying",
        }[operation]

        # For spinner text, use different verbs
        self.spinner_operation = {
            "deploy": "Deploying",
            "preview": "Analyzing",
            "refresh": "Refreshing",
            "destroy": "Destroying",
        }[operation]

        # Rich Live display with auto-updating renderable
        self.renderable = LiveRenderable(self)
        self.live = Live(
            self.renderable,
            console=self.console,
            refresh_per_second=10,  # Higher refresh rate for smooth spinner
            transient=True,
        )
        self.live_started = False

        # Create spinner once for reuse
        self.spinner = Spinner("dots", style="cyan")
        # Start live display immediately for deploy/destroy to show spinner while loading
        if operation in ["deploy", "destroy"]:
            self.live_started = True
            self.live.start()

    def handle_event(self, event: EngineEvent) -> None:
        try:
            if not isinstance(event, EngineEvent):
                return

            # Start live display on first real event
            if not self.live_started and (event.prelude_event or event.resource_pre_event):
                self.live_started = True
                self.live.start()

            # Dispatch to specific handlers
            if event.prelude_event:
                self._handle_prelude(event)
            elif event.resource_pre_event:
                self._handle_resource_pre(event)
            elif event.res_outputs_event:
                self._handle_res_outputs(event)
            elif event.res_op_failed_event:
                self._handle_res_op_failed(event)
            elif event.summary_event:
                self._handle_summary(event)
            elif event.diagnostic_event:
                self._handle_diagnostic(event)

            # Rich automatically calls LiveRenderable.__rich__() every refresh cycle

        except Exception as e:
            # Log error but don't crash
            logger.error(f"Error handling event: {e}", exc_info=True)
            # Fall back to simple print
            pprint(event, indent_guides=True)

    def _handle_prelude(self, event) -> None:
        """Handle PreludeEvent - start of operation."""
        # Determine operation type from context
        # This is set by the calling function in reality

    def _handle_resource_pre(self, event) -> None:
        metadata = event.resource_pre_event.metadata
        urn = metadata.urn

        # Skip if already tracking (duplicate events)
        if urn in self.resources:
            return

        # Extract logical name from URN
        logical_name = self._extract_logical_name(urn)

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

        operation_verb = op_map.get(metadata.op, "Processing")

        # Skip Pulumi internal resources (stack, providers)
        if (
            metadata.type.startswith("pulumi:")
            or metadata.type.startswith("pulumi:providers:")
            or "pulumi:pulumi:Stack" in metadata.type
        ):
            return

        # Track the resource
        self.resources[urn] = {
            "logical_name": logical_name,
            "type": metadata.type,
            "operation": metadata.op,
            "operation_verb": operation_verb,
            "status": "active",
            "start_time": event.timestamp,
            "end_time": None,
            "error": None,
        }
        self.resource_order.append(urn)

        # We already know the operation type from the constructor

        # Update total count (include preview resources)
        self.total_resources += 1

    def _handle_res_outputs(self, event) -> None:
        """Handle ResOutputsEvent - resource operation completed."""
        metadata = event.res_outputs_event.metadata
        urn = metadata.urn

        if urn in self.resources:
            resource = self.resources[urn]
            resource["status"] = "completed"
            resource["end_time"] = event.timestamp
            self.completed_count += 1

            # Track operation counts for summary
            operation = resource["operation"]
            if operation in self.operation_counts:
                self.operation_counts[operation] += 1
            elif operation == "same" or str(operation).lower() == "same":
                self.operation_counts["same"] += 1

    def _handle_res_op_failed(self, event) -> None:
        metadata = event.res_op_failed_event.metadata
        urn = metadata.urn

        if urn in self.resources:
            self.resources[urn]["status"] = "failed"
            self.resources[urn]["end_time"] = event.timestamp
            self.failed_count += 1

    def _handle_diagnostic(self, event) -> None:
        # Store error messages for associated resources
        diagnostic = event.diagnostic_event
        if diagnostic.urn and diagnostic.severity == "error" and diagnostic.urn in self.resources:
            self.resources[diagnostic.urn]["error"] = diagnostic.message

    def _handle_summary(self, event: EngineEvent) -> None:
        # Stop live display
        if self.live_started:
            self.live.stop()

        # Show final summary with resources list
        duration = datetime.now() - self.start_time
        total_seconds = int(duration.total_seconds())
        minutes = total_seconds // 60
        seconds = total_seconds % 60

        # Add spacing and print header with highlighting
        self.console.print()  # Empty line before
        header = Text()
        header.append(f"{self.operation_type} ", style="bold")
        header.append(f"{self.app_name}", style="bold cyan")
        header.append(" → ", style="dim")
        header.append(f"{self.environment}", style="bold yellow")
        self.console.print(header)
        self.console.print()  # Empty line after

        # Show resources - group only "same" at the end, others in order
        if self.total_resources > 0:
            if self.is_preview:
                # Separate "same" resources from changing ones
                changing_resources = []
                same_resources = []

                for urn in self.resource_order:
                    resource = self.resources[urn]
                    if (
                        resource["operation"] == "same"
                        or str(resource["operation"]).lower() == "same"
                    ):
                        same_resources.append(resource)
                    else:
                        changing_resources.append(resource)

                # Show changing resources first (in original order)
                for resource in changing_resources:
                    line = self._format_resource_line(resource, is_preview=True)
                    self.console.print(line)

                # Show "same" resources grouped together at the end
                for resource in same_resources:
                    line = self._format_resource_line(resource, is_preview=True)
                    self.console.print(line)
            else:
                # Actual deployment - group changed and unchanged resources
                changing_resources = []
                skipped_resources = []
                failed_resources = []

                for urn in self.resource_order:
                    resource = self.resources[urn]
                    if resource["status"] == "failed":
                        failed_resources.append(resource)
                    elif (
                        resource["operation"] == "same"
                        or str(resource["operation"]).lower() == "same"
                    ):
                        skipped_resources.append(resource)
                    else:
                        changing_resources.append(resource)

                for resource in changing_resources:
                    if resource["status"] == "completed":
                        # Calculate duration for summary with decimal precision
                        duration_str = ""
                        if resource["start_time"] and resource["end_time"]:
                            duration = resource["end_time"] - resource["start_time"]
                            duration_str = f"({duration:.1f}s)"

                        line = self._format_resource_line(
                            resource, is_preview=False, duration_str=duration_str, is_summary=True
                        )
                        self.console.print(line)

                # Show skipped resources grouped at the end - use muted colors like diff
                for resource in skipped_resources:
                    if resource["status"] == "completed":
                        line = self._format_resource_line(
                            resource, is_preview=False, is_summary=True
                        )
                        self.console.print(line)

                # Show failed resources last - use muted colors like diff
                for resource in failed_resources:
                    line = self._format_resource_line(resource, is_preview=False, is_summary=True)
                    self.console.print(line)

        # Completion message with operation counts
        if self.total_resources > 0:
            # Count operations directly from resources instead of tracking separately
            operation_counts = {
                OpType.CREATE: 0,
                OpType.UPDATE: 0,
                OpType.DELETE: 0,
                OpType.REPLACE: 0,
                OpType.CREATE_REPLACEMENT: 0,
                "same": 0,
            }

            for resource in self.resources.values():
                operation = resource["operation"]
                if operation in operation_counts:
                    operation_counts[operation] += 1
                elif operation == "same" or str(operation).lower() == "same":
                    operation_counts["same"] += 1

            # Build operation counts message with correct tense
            operation_parts = []
            if operation_counts[OpType.CREATE] > 0:
                verb = "to create" if self.is_preview else "created"
                count_part = Text(str(operation_counts[OpType.CREATE]), style="bold green")
                verb_part = Text(f" {verb}", style="")
                operation_parts.append(Text.assemble(count_part, verb_part))
            if operation_counts[OpType.UPDATE] > 0:
                verb = "to update" if self.is_preview else "updated"
                count_part = Text(str(operation_counts[OpType.UPDATE]), style="bold yellow")
                verb_part = Text(f" {verb}", style="")
                operation_parts.append(Text.assemble(count_part, verb_part))
            if operation_counts[OpType.DELETE] > 0:
                verb = "to delete" if self.is_preview else "deleted"
                count_part = Text(str(operation_counts[OpType.DELETE]), style="bold red")
                verb_part = Text(f" {verb}", style="")
                operation_parts.append(Text.assemble(count_part, verb_part))
            if operation_counts[OpType.REPLACE] > 0:
                verb = "to replace" if self.is_preview else "replaced"
                count_part = Text(str(operation_counts[OpType.REPLACE]), style="bold blue")
                verb_part = Text(f" {verb}", style="")
                operation_parts.append(Text.assemble(count_part, verb_part))
            if operation_counts[OpType.CREATE_REPLACEMENT] > 0:
                verb = "to swap" if self.is_preview else "swapped"
                count_part = Text(
                    str(operation_counts[OpType.CREATE_REPLACEMENT]), style="bold blue"
                )
                verb_part = Text(f" {verb}", style="")
                operation_parts.append(Text.assemble(count_part, verb_part))
            if operation_counts["same"] > 0:
                count_part = Text(str(operation_counts["same"]), style="bold dim")
                verb_part = Text(" unchanged", style="")
                operation_parts.append(Text.assemble(count_part, verb_part))

            # Show progress summary first
            if minutes > 0:
                time_str = f"{minutes}m {seconds}s"
            else:
                time_str = f"{seconds}s"
            self.console.print(
                f"\n{self.total_resources}/{self.total_resources} done in {time_str}"
            )

            # Show operation counts with colored numbers
            if operation_parts:
                # Create combined text with commas
                final_text = Text()
                for i, part in enumerate(operation_parts):
                    if i > 0:
                        final_text.append(", ")
                    final_text.append(part)
                self.console.print(final_text)
        else:
            if minutes > 0:
                time_str = f"{minutes}m {seconds}s"
            else:
                time_str = f"{seconds}s"
            self.console.print(f"\nCompleted in {time_str}")

    def _render(self) -> RenderableType:
        # Build main content text
        content = Text()

        # Header
        content.append(f"{self.operation_type} ", style="bold")
        content.append(f"{self.app_name}", style="bold cyan")
        content.append(" → ", style="dim")
        content.append(f"{self.environment}", style="bold yellow")
        content.append("\n\n")

        # Resources - show changing resources first, unchanged at bottom
        changing_resources = []
        unchanged_resources = []

        for urn in self.resource_order:
            resource = self.resources[urn]
            is_unchanged = (
                resource["operation"] == "same" or str(resource["operation"]).lower() == "same"
            )
            if is_unchanged:
                unchanged_resources.append(resource)
            else:
                changing_resources.append(resource)

        # Show changing resources first
        for resource in changing_resources:
            # Calculate duration with decimal precision
            if resource["start_time"]:
                if resource["end_time"]:
                    duration = resource["end_time"] - resource["start_time"]
                else:
                    duration = datetime.now().timestamp() - resource["start_time"]
                duration_str = f"({duration:.1f}s)"
            else:
                duration_str = ""

            # Use shared formatting method
            duration = duration_str if not self.is_preview else ""
            line = self._format_resource_line(
                resource, is_preview=self.is_preview, duration_str=duration
            )
            content.append(line)
            content.append("\n")

        # Show unchanged resources at bottom
        for resource in unchanged_resources:
            line = self._format_resource_line(resource, is_preview=self.is_preview)
            content.append(line)
            content.append("\n")

        # Progress footer (show for all operations)
        if self.total_resources > 0:
            # Progress text
            progress = (
                f"{self.completed_count + self.failed_count}/{self.total_resources} complete"
            )
            duration = datetime.now() - self.start_time
            total_seconds = int(duration.total_seconds())

            # Add spinner if operations are active or at start
            active_count = sum(1 for r in self.resources.values() if r["status"] == "active")
            if active_count > 0 or self.completed_count == 0:
                # Put spinner inline with progress text and operation type
                content.append("\n")
                operation_text = self.spinner_operation
                progress_text = f"{operation_text}  {progress}  {total_seconds}s"
                self.spinner.update(text=progress_text, style="cyan")
                return Group(content, self.spinner)
            content.append(f"\n{progress}  {total_seconds}s", style="dim")

        return content

    def _format_resource_line(
        self, resources: dict, is_preview: bool, duration_str: str = "", is_summary: bool = False
    ) -> Text:
        line = Text()

        # Get operation colors
        operation_colors = {
            OpType.CREATE: "green",
            OpType.DELETE: "red",
            OpType.UPDATE: "yellow",
            OpType.REPLACE: "blue",
            OpType.CREATE_REPLACEMENT: "blue",
            "same": "dim",
        }
        op_color = operation_colors.get(resources["operation"], "yellow")

        # Simple unified formatting - always muted colors, only icon/tense changes
        if resources["status"] == "failed":
            prefix, verb = "✗ ", "failed"
            status_color = "red"
        elif resources["operation"] == OpType.CREATE:
            if is_preview:
                prefix, verb = "+ ", "to create"
            elif is_summary:
                prefix, verb = "✓ ", "created"
            elif resources["status"] == "active":
                prefix, verb = "| ", "creating"
            else:  # completed during live
                prefix, verb = "✓ ", "created"
            status_color = op_color
        elif resources["operation"] == OpType.DELETE:
            if is_preview:
                prefix, verb = "- ", "to delete"
            elif is_summary:
                prefix, verb = "✓ ", "deleted"
            elif resources["status"] == "active":
                prefix, verb = "| ", "deleting"
            else:  # completed during live
                prefix, verb = "✓ ", "deleted"
            status_color = op_color
        elif resources["operation"] == OpType.UPDATE:
            if is_preview:
                prefix, verb = "~ ", "to update"
            elif is_summary:
                prefix, verb = "✓ ", "updated"
            elif resources["status"] == "active":
                prefix, verb = "| ", "updating"
            else:  # completed during live
                prefix, verb = "✓ ", "updated"
            status_color = op_color
        elif resources["operation"] == OpType.REPLACE:
            if is_preview:
                prefix, verb = "± ", "to replace"
            elif is_summary:
                prefix, verb = "✓ ", "replaced"
            elif resources["status"] == "active":
                prefix, verb = "| ", "replacing"
            else:  # completed during live
                prefix, verb = "✓ ", "replaced"
            status_color = op_color
        elif resources["operation"] == OpType.CREATE_REPLACEMENT:
            if is_preview:
                prefix, verb = "± ", "to swap"
            elif is_summary:
                prefix, verb = "✓ ", "swapped"
            elif resources["status"] == "active":
                prefix, verb = "| ", "swapping"
            else:  # completed during live
                prefix, verb = "✓ ", "swapped"
            status_color = op_color
        elif resources["operation"] == "same" or str(resources["operation"]).lower() == "same":
            prefix, verb = "~ ", "unchanged"
            status_color = op_color
        else:
            # Fallback
            prefix, verb = "| ", "processing"
            status_color = op_color

        # Always muted colors - only status prefix and timing colored
        line.append(f"{prefix}{verb} ", style=status_color)
        line.append(f"{resources['logical_name']}", style="bold")
        line.append(" → ", style="dim")
        line.append(f"{resources['type']}", style="dim")

        # Timing in operation color
        if duration_str:
            line.append(f" {duration_str}", style=status_color)

        return line

    def _extract_logical_name(self, urn: str) -> str:
        # URN format: urn:pulumi:stack::project::type::name
        # We want the 'name' part.
        try:
            parts = urn.split("::")
            if len(parts) >= 4:
                return parts[-1]
            return urn
        except Exception:
            return urn
