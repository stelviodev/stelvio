from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal, cast

from pulumi.automation import DiffKind, EngineEvent, OpType
from rich.console import Console, Group
from rich.live import Live
from rich.spinner import Spinner
from rich.text import Text

from stelvio.rich_deployment_diffs import (
    _get_nested_value,
    format_property_diff_lines,
    format_replacement_warning,
)
from stelvio.rich_deployment_format import (
    _calculate_component_duration,
    _calculate_duration,
    build_operation_counts_text,
    build_preview_counts_text,
    format_child_error_line,
    format_child_resource_line,
    format_component_header,
    format_outputs,
)
from stelvio.rich_deployment_model import (
    _REPLACE_KINDS,
    MAX_DIFFS_TO_SHOW,
    STELVIO_TYPE_PREFIX,
    ComponentInfo,
    JsonValue,
    ResourceInfo,
    WarningInfo,
    _clean_diagnostic_message,
    _extract_logical_name,
    _extract_parent_component_type_from_urn,
    _extract_type_from_urn,
    _interrupted_create_warning_urn,
    _parse_stelvio_parent,
    _readable_type,
    count_changed_resources,
    get_total_duration,
    group_components,
)

if TYPE_CHECKING:
    from collections.abc import Callable, MutableMapping

    from pulumi.automation import OutputValue, StepEventMetadata
    from rich.console import RenderableType

logger = logging.getLogger(__name__)


class RichDeploymentHandler:
    """Displays Pulumi deployment progress in the terminal with a live, updating view.

    Transforms Pulumi engine events into a user-friendly display showing which
    resources are being created/updated/deleted, with real-time progress and
    timing. Also produces JSON summaries and stream events for machine-readable
    output modes.
    """

    # API Gateway account-level resources — internal plumbing.
    # Read reference (.get()) is always hidden. Managed resources shown on CREATE
    # (1st deploy) and DELETE during destroy, hidden on .apply() state cleanup.
    _always_hidden_resources = frozenset({"api-gateway-account-ref"})
    _internal_managed_resources = frozenset(
        {
            "api-gateway-account",
            "StelvioAPIGatewayPushToCloudWatchLogsRole",
        }
    )

    # -----------------------------------------------------------------------
    # Initialization
    # -----------------------------------------------------------------------

    def __init__(  # noqa: PLR0913
        self,
        app_name: str,
        environment: str,
        operation: Literal["deploy", "preview", "refresh", "destroy", "outputs"],
        show_unchanged: bool = False,
        dev_mode: bool = False,
        compact: bool = False,
        live_enabled: bool = True,
        stream_writer: Callable[[dict[str, JsonValue]], None] | None = None,
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

        # Component tracking
        self.components: dict[str, ComponentInfo] = {}  # top-level component URN → ComponentInfo
        self._components_by_urn: dict[str, ComponentInfo] = {}  # all component URNs
        self._component_parents: dict[str, str] = {}  # child component URN -> parent component URN
        self.resource_to_component: dict[str, str] = {}  # resource URN → parent URN
        self.orphan_resources: list[ResourceInfo] = []  # resources with no Stelvio parent

        # Operation state
        self.is_preview = operation == "preview"
        self.is_destroy = operation == "destroy"
        self.operation = operation
        self.show_unchanged = show_unchanged
        self.dev_mode = dev_mode
        self.compact = compact
        self.live_enabled = live_enabled
        self.stream_writer = stream_writer

        # For spinner text, use different verbs
        self.spinner_operation = {
            "deploy": "Deploying",
            "preview": "Analyzing differences",
            "refresh": "Refreshing",
            "destroy": "Destroying",
            "outputs": "Showing outputs",
        }[operation]

        self.live = Live(self, console=self.console, transient=False)
        self.live_started = False
        self._summary_reached = False

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

        # Always start live display immediately to show spinner when enabled.
        if self.live_enabled:
            self.live_started = True
            self.live.start()

        # For cleanup spinner
        self.cleanup_status = None
        # Collect error diagnostics for later analysis
        self.error_diagnostics = []
        self.warning_diagnostics: list[WarningInfo] = []
        self._seen_warnings: set[tuple[str | None, str, str | None]] = set()
        self._emitted_stream_resources: set[str] = set()

    # -----------------------------------------------------------------------
    # Event handling
    # -----------------------------------------------------------------------

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
                    self._component_parents[metadata.urn] = parent_urn
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

        # No Stelvio parent — orphan resource.
        # Read references (.get()) are always hidden — pure internal lookups.
        if resource.logical_name in self._always_hidden_resources:
            return
        # Managed internal resources shown on CREATE and DELETE during destroy,
        # hidden on .apply() state cleanup (2nd deploy).
        if resource.logical_name in self._internal_managed_resources:
            if resource.operation == OpType.CREATE or self.is_destroy:
                self.orphan_resources.append(resource)
            return
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
        if metadata.new and metadata.new.parent:
            return metadata.new.parent
        if metadata.old and metadata.old.parent:
            return metadata.old.parent
        return None

    def _handle_res_outputs(self, event: EngineEvent) -> None:  # noqa: C901
        metadata = event.res_outputs_event.metadata
        urn = metadata.urn
        if metadata.type.startswith(STELVIO_TYPE_PREFIX) and urn in self._components_by_urn:
            return
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
        if resource.operation != OpType.SAME:
            self._emit_resource_event(urn, resource, timestamp=event.timestamp)

    def _handle_res_op_failed(self, event: EngineEvent) -> None:
        metadata = event.res_op_failed_event.metadata
        urn = metadata.urn.strip()
        logical_name = _extract_logical_name(urn)
        if metadata.type.startswith(STELVIO_TYPE_PREFIX) and urn in self._components_by_urn:
            return

        if urn in self.resources:
            if self.resources[urn].status != "failed":
                self.resources[urn].status = "failed"
                self.failed_count += 1
            self.resources[urn].end_time = event.timestamp
        else:
            logger.warning("Failed event for untracked resource: %s", logical_name)

    def _handle_diagnostic(self, event: EngineEvent) -> None:
        diagnostic = event.diagnostic_event
        severity = (diagnostic.severity or "").lower()

        if severity == "warning":
            self._record_warning(
                message=_clean_diagnostic_message(diagnostic.message), urn=diagnostic.urn
            )
            return

        if severity == "error":
            self.error_diagnostics.append(diagnostic)

        if diagnostic.urn and severity == "error":
            urn = diagnostic.urn.strip()
            logical_name = _extract_logical_name(urn)
            clean_error = _clean_diagnostic_message(diagnostic.message)

            if urn in self.resources:
                self.resources[urn].error = clean_error
                if self.resources[urn].status != "failed":
                    self.resources[urn].status = "failed"
                    self.failed_count += 1
                self.resources[urn].end_time = event.timestamp
                self.emit_stream_event(
                    "error",
                    timestamp=event.timestamp,
                    error={
                        "resource": self.resources[urn].type,
                        "message": clean_error,
                    },
                )
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
        attached_to_component = False
        parent_type = _extract_parent_component_type_from_urn(urn)
        candidate_name = self._short_resource_name(logical_name)
        if parent_type:
            matching_components = [
                comp
                for comp in self._components_by_urn.values()
                if comp.component_type == parent_type
                and (candidate_name == comp.name or candidate_name.startswith(f"{comp.name}-"))
            ]
            if matching_components:
                matched_component = max(matching_components, key=lambda comp: len(comp.name))
                if failed_resource not in matched_component.children:
                    matched_component.children.append(failed_resource)
                self.resource_to_component[urn] = matched_component.urn
                attached_to_component = True

        if not attached_to_component and failed_resource not in self.orphan_resources:
            self.orphan_resources.append(failed_resource)
        self.emit_stream_event(
            "error", timestamp=timestamp, error=self._resource_stream_json(failed_resource)
        )

    def _record_warning(self, *, message: str, urn: str | None) -> None:
        """Record warning diagnostics once while preserving first-seen order."""
        clean_message = message.strip()
        if not clean_message:
            return

        clean_urn = urn.strip() if urn else None
        hint: str | None = None
        interrupted_urn = _interrupted_create_warning_urn(clean_message)
        if interrupted_urn:
            clean_urn = clean_urn or interrupted_urn
            clean_message = (
                "A previous deploy appears to have been interrupted while creating this resource."
            )
            hint = "Run `stlv state repair` to clear stale pending operations."

        warning_key = (clean_urn, clean_message, hint)
        if warning_key in self._seen_warnings:
            return

        self._seen_warnings.add(warning_key)
        warning = WarningInfo(message=clean_message, urn=clean_urn, hint=hint)
        self.warning_diagnostics.append(warning)
        self.emit_stream_event("warning", **self._warning_json(warning))

    def _handle_summary(self) -> None:
        if self.live_started:
            # Signal _render() to produce the final frame without spinner
            self._summary_reached = True
            self.live.refresh()
            self.live.stop()
            self.live_started = False

        if not self.live_enabled:
            return

        # Print "Nothing to deploy" / "No differences found" when there's nothing to show.
        # The live display handles the resource tree, but this message only comes from here.
        # Count only visible changed resources (excludes hidden internal resources)
        visible_changed = len(
            [
                r
                for r in self.resources.values()
                if r.operation not in (OpType.SAME, OpType.READ, OpType.DISCARD)
                and self._is_resource_visible(r)
            ]
        )
        if visible_changed == 0 and not self.error_diagnostics:
            if self.is_preview:
                message = "No differences found"
            elif self.is_destroy:
                message = "Nothing to destroy"
            else:
                message = "Nothing to deploy"
            self.console.print(message)
            self.console.print()

        if not self.error_diagnostics and not self.dev_mode and not self.is_preview:
            self.cleanup_status = self.console.status("Finalizing...", spinner="dots")
            self.cleanup_status.start()

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

    # -----------------------------------------------------------------------
    # Live terminal rendering
    # -----------------------------------------------------------------------

    def __rich__(self) -> RenderableType:
        return self._render()

    def _render(self) -> RenderableType:  # noqa: C901
        # Final frame: render without spinner so live.stop() keeps clean content
        if self._summary_reached:
            return self._render_final_content()

        content = Text()

        changing_comps, unchanged_comps, failed_comps = group_components(self.components)
        visible_components = [*changing_comps, *failed_comps]
        if self.show_unchanged:
            visible_components = [*changing_comps, *unchanged_comps, *failed_comps]

        visible_orphans = self._visible_orphan_resources()

        if visible_components or visible_orphans:
            content.append("\n")

        for comp in changing_comps:
            self._render_component(content, comp, expanded=True)

        if self.show_unchanged:
            for comp in unchanged_comps:
                self._render_component(content, comp, expanded=False)

        for comp in failed_comps:
            self._render_component(content, comp, expanded=True)

        if visible_orphans:
            has_comps = bool(changing_comps or unchanged_comps or failed_comps)
            self._render_orphan_resources(content, has_components=has_comps)

        # Progress footer with spinner
        minutes, seconds = get_total_duration(self.start_time)
        total_seconds = minutes * 60 + seconds

        completed_components = sum(
            1 for c in visible_components if c.status in ("completed", "failed")
        )
        total_components = len(visible_components)
        visible_orphan_resources = self._visible_orphan_resources()

        if total_components > 0:
            progress = f"{completed_components}/{total_components} complete"
            progress_text = f"{self.spinner_operation}  {progress}  {total_seconds}s"
        elif visible_orphan_resources:
            completed_orphan_resources = sum(
                1
                for resource in visible_orphan_resources
                if resource.status in ("completed", "failed")
            )
            progress = f"{completed_orphan_resources}/{len(visible_orphan_resources)} complete"
            progress_text = f"{self.spinner_operation}  {progress}  {total_seconds}s"
        else:
            progress_text = f"{self.spinner_operation}  {total_seconds}s"

        self.spinner.update(text=progress_text, style="cyan")
        return Group(content, self.spinner)

    def _render_final_content(self) -> RenderableType:
        """Render the final frame without spinner — kept by live.stop() as static output."""
        content = Text()

        changing_comps, unchanged_comps, failed_comps = group_components(self.components)
        visible_orphans = self._visible_orphan_resources()

        for comp in changing_comps:
            self._render_component(content, comp, expanded=True)

        if self.show_unchanged:
            for comp in unchanged_comps:
                self._render_component(content, comp, expanded=False)

        for comp in failed_comps:
            self._render_component(content, comp, expanded=True)

        if visible_orphans:
            has_comps = bool(changing_comps or unchanged_comps or failed_comps)
            self._render_orphan_resources(content, has_components=has_comps)

        # Return empty string if no content, so Rich doesn't render a blank line
        return content if content.plain.strip() else ""

    def _render_component(
        self, content: Text, comp: ComponentInfo, *, expanded: bool, indent: int = 0
    ) -> None:
        """Render a single component into the content Text."""
        duration_str = _calculate_component_duration(comp) if not self.is_preview else ""
        indent_str = "    " * indent

        # Compact preview: header only, no children
        if self.compact and self.is_preview:
            header = format_component_header(
                comp, self.is_preview, duration_str, resource_word_in_preview=True
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

    def _visible_orphan_resources(self) -> list[ResourceInfo]:
        """Orphan resources that should be shown (filter out unchanged/read)."""
        return [
            r
            for r in self.orphan_resources
            if self.show_unchanged
            or r.status == "failed"
            or r.operation not in (OpType.SAME, OpType.READ)
        ]

    def _render_orphan_resources(self, content: Text, *, has_components: bool) -> None:
        """Render resources that aren't part of any Stelvio component."""
        visible = self._visible_orphan_resources()
        if not visible:
            return
        if has_components:
            content.append("\n")
        content.append("Other resources\n", style="dim")
        for resource in visible:
            duration_str = _calculate_duration(resource) if not self.is_preview else ""
            line = format_child_resource_line(resource, self.is_preview, duration_str, indent=0)
            content.append("  ")
            content.append(line)
            content.append("\n")
            if resource.error:
                content.append(format_child_error_line(resource.error))
                content.append("\n")

    # -----------------------------------------------------------------------
    # Summary printing (after live display stops)
    # -----------------------------------------------------------------------

    def _print_resources_summary(self) -> None:
        """Print all resources in the final summary, grouped by component."""
        changing_comps, unchanged_comps, failed_comps = group_components(self.components)

        comp_groups: list[list[ComponentInfo]] = [changing_comps, failed_comps]
        if self.show_unchanged:
            comp_groups.insert(1, unchanged_comps)

        visible_orphans = self._visible_orphan_resources()
        has_any = any(comp_groups) or visible_orphans

        if not has_any:
            if not self.error_diagnostics:
                message = "No differences found" if self.is_preview else "Nothing to deploy"
                self.console.print(message)
        else:
            for comps in comp_groups:
                for comp in comps:
                    self._print_component_summary(comp)

            if visible_orphans:
                if any(comp_groups):
                    self.console.print()
                self.console.print("Other resources", style="dim")
                for resource in visible_orphans:
                    duration_str = _calculate_duration(resource) if not self.is_preview else ""
                    line = format_child_resource_line(
                        resource, self.is_preview, duration_str, indent=0
                    )
                    self.console.print(Text("  ").append(line))
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
        """Print all children under a failed component for full failure context."""
        for child in comp.children:
            if isinstance(child, ComponentInfo):
                self._print_component_summary(child, indent=indent + 1)
            else:
                child_duration = _calculate_duration(child)
                line = format_child_resource_line(
                    child, self.is_preview, child_duration, indent + 1
                )
                self.console.print(line)
                if child.error:
                    self.console.print(format_child_error_line(child.error, indent + 1))

    def _print_warnings_summary(self) -> None:
        """Print collected warning diagnostics with best-effort resource context."""
        warning_count = len(self.warning_diagnostics)
        if warning_count == 0:
            return

        self.console.print()
        noun = "warning" if warning_count == 1 else "warnings"
        self.console.print(f"⚠ {warning_count} {noun}", style="bold yellow")
        for warning in self.warning_diagnostics:
            context = self.describe_urn(warning.urn) if warning.urn else None
            if context:
                self.console.print(f"  {context}:")
                self.console.print(f"    {warning.message}", style="dim")
            else:
                self.console.print(f"  {warning.message}", style="dim")
            if warning.hint:
                self.console.print(f"    Hint: {warning.hint}", style="yellow")

    def show_completion(
        self,
        outputs: MutableMapping[str, OutputValue] | None = None,
        *,
        output_lines: list[str] | None = None,
    ) -> None:
        """Show outputs and final completion message."""
        if self.cleanup_status is not None:
            self.cleanup_status.stop()

        minutes, seconds = get_total_duration(self.start_time)
        time_str = f"{minutes}m {seconds}s" if minutes > 0 else f"{seconds}s"

        status_icon, error_suffix = ("✗", "with errors") if self.failed_count > 0 else ("✓", "")
        self.console.print(f"{status_icon} {self.completion_verb} in {time_str}{error_suffix}")

        if self.total_resources > 0:
            changing_comps, _, failed_comps = group_components(self.components)
            visible_resources = {
                k: v for k, v in self.resources.items() if self._is_resource_visible(v)
            }
            if self.is_preview:
                counts_text = build_preview_counts_text(
                    visible_resources,
                    component_count=len(changing_comps) + len(failed_comps),
                )
            else:
                counts_text = build_operation_counts_text(
                    total_resources=count_changed_resources(visible_resources),
                    component_count=len(changing_comps) + len(failed_comps),
                    summary_verb=self.summary_verb,
                )
            if counts_text:
                self.console.print(counts_text)

        self._print_warnings_summary()

        if output_lines and not self.is_preview:
            for line in output_lines:
                self.console.print(line)
        elif outputs and not self.is_preview:
            for line in format_outputs(outputs):
                self.console.print(line)

    # -----------------------------------------------------------------------
    # JSON serialization and stream events
    # -----------------------------------------------------------------------

    @property
    def stream_enabled(self) -> bool:
        return self.stream_writer is not None

    def emit_stream_event(
        self, event_type: str, *, timestamp: float | None = None, **payload: JsonValue
    ) -> None:
        if self.stream_writer is None:
            return
        event_payload: dict[str, JsonValue] = {
            "event": event_type,
            "operation": "diff" if self.is_preview else self.operation,
            "app": self.app_name,
            "env": self.environment,
            "timestamp": self._format_stream_timestamp(timestamp),
        }
        event_payload.update(payload)
        self.stream_writer(event_payload)

    @staticmethod
    def _format_stream_timestamp(timestamp: float | None) -> str:
        when = (
            datetime.now().astimezone()
            if timestamp is None
            else datetime.fromtimestamp(timestamp, tz=UTC).astimezone()
        )
        return when.isoformat()

    @staticmethod
    def _component_ref(component: ComponentInfo) -> dict[str, JsonValue]:
        return {"type": component.component_type, "name": component.name}

    def _emit_resource_event(self, urn: str, resource: ResourceInfo, *, timestamp: float) -> None:
        if urn in self._emitted_stream_resources:
            return
        if not self._is_resource_visible(resource):
            return
        self._emitted_stream_resources.add(urn)
        payload: dict[str, JsonValue] = {
            "resource": self._resource_stream_json(resource),
        }
        component_urn = self.resource_to_component.get(urn)
        if component_urn and component_urn in self._components_by_urn:
            payload["component"] = self._component_ref(self._components_by_urn[component_urn])
        self.emit_stream_event("resource", timestamp=timestamp, **payload)

    @staticmethod
    def _duration_seconds(start_time: float | None, end_time: float | None) -> float | None:
        if start_time is None or end_time is None:
            return None
        return round(end_time - start_time, 1)

    @staticmethod
    def _diff_kind_name(kind: DiffKind) -> str:
        return {
            DiffKind.ADD: "add",
            DiffKind.UPDATE: "update",
            DiffKind.DELETE: "delete",
            DiffKind.ADD_REPLACE: "add_replace",
            DiffKind.UPDATE_REPLACE: "update_replace",
            DiffKind.DELETE_REPLACE: "delete_replace",
        }.get(kind, "update")

    def _operation_name(self, operation: OpType, *, has_replacement: bool = False) -> str:
        if has_replacement or operation in (OpType.REPLACE, OpType.CREATE_REPLACEMENT):
            return "replace"
        if self.operation == "refresh" and operation == OpType.REFRESH:
            return "unchanged"
        return {
            OpType.CREATE: "create",
            OpType.UPDATE: "update",
            OpType.DELETE: "delete",
            OpType.DISCARD: "delete",
            OpType.REFRESH: "refresh",
            OpType.READ: "read",
            OpType.SAME: "unchanged",
        }.get(operation, "change")

    def _resource_changes_json(self, resource: ResourceInfo) -> list[dict[str, JsonValue]]:
        if not resource.detailed_diff:
            return []

        changes: list[dict[str, JsonValue]] = []
        for prop_path, prop_diff in sorted(resource.detailed_diff.items()):
            kind = prop_diff.diff_kind
            change: dict[str, JsonValue] = {
                "path": prop_path,
                "kind": self._diff_kind_name(kind),
            }
            old_val = _get_nested_value(resource.old_inputs, prop_path)
            new_val = _get_nested_value(resource.new_inputs, prop_path)
            if kind in (
                DiffKind.UPDATE,
                DiffKind.UPDATE_REPLACE,
                DiffKind.DELETE,
                DiffKind.DELETE_REPLACE,
            ):
                change["old"] = old_val
            if kind in (
                DiffKind.ADD,
                DiffKind.ADD_REPLACE,
                DiffKind.UPDATE,
                DiffKind.UPDATE_REPLACE,
            ):
                change["new"] = new_val
            if kind in _REPLACE_KINDS:
                change["forces_replacement"] = True
            changes.append(change)
        return changes

    def _resource_json(self, resource: ResourceInfo) -> dict[str, JsonValue]:
        data: dict[str, JsonValue] = {
            "name": resource.logical_name,
            "type": resource.type,
            "operation": self._operation_name(
                resource.operation,
                has_replacement=resource.has_replacement,
            ),
        }
        if resource.error:
            data["error"] = resource.error
        changes = self._resource_changes_json(resource)
        if changes:
            data["changes"] = changes
        return data

    def _resource_stream_json(self, resource: ResourceInfo) -> dict[str, JsonValue]:
        data: dict[str, JsonValue] = {
            "name": resource.logical_name,
            "type": resource.type,
            "operation": self._operation_name(
                resource.operation,
                has_replacement=resource.has_replacement,
            ),
        }
        if resource.error:
            data["error"] = resource.error
        return data

    def _component_json(self, component: ComponentInfo) -> dict[str, JsonValue]:
        resources: list[dict[str, JsonValue]] = []
        child_components: list[dict[str, JsonValue]] = []
        for child in component.children:
            if isinstance(child, ResourceInfo):
                if child.operation == OpType.SAME and not self.show_unchanged:
                    continue
                resources.append(self._resource_json(child))
            else:
                child_payload = self._component_json(child)
                if (
                    not child_payload.get("resources")
                    and not child_payload.get("components")
                    and not self.show_unchanged
                ):
                    continue
                child_components.append(child_payload)

        data: dict[str, JsonValue] = {
            "type": component.component_type,
            "name": component.name,
            "operation": self._operation_name(
                component.operation,
                has_replacement=component.has_replacement,
            ),
            "resources": resources,
        }
        if child_components:
            data["components"] = child_components
        if component.error:
            data["error"] = component.error
        return data

    def _is_resource_visible(self, resource: ResourceInfo) -> bool:
        """Check if a resource should be included in output (human and JSON).

        Filters API Gateway internal resources using the same rules as the
        human-readable display: always-hidden resources are excluded, managed
        internal resources are shown only on CREATE (and on destroy).
        """
        if resource.logical_name in self._always_hidden_resources:
            return False
        if resource.logical_name in self._internal_managed_resources:
            return resource.operation == OpType.CREATE or self.is_destroy
        return True

    def _other_resources_json(self) -> list[dict[str, JsonValue]]:
        return [
            self._resource_json(resource)
            for resource in self.orphan_resources
            if self._is_resource_visible(resource)
            and (self.show_unchanged or resource.operation != OpType.SAME)
        ]

    def _preview_summary_counts_json(self) -> dict[str, int]:
        counts = {
            "to_create": 0,
            "to_update": 0,
            "to_delete": 0,
            "to_replace": 0,
        }
        for resource in self.resources.values():
            if resource.operation == OpType.SAME:
                continue
            if not self._is_resource_visible(resource):
                continue
            if resource.has_replacement:
                counts["to_replace"] += 1
            elif resource.operation == OpType.CREATE:
                counts["to_create"] += 1
            elif resource.operation == OpType.UPDATE:
                counts["to_update"] += 1
            elif resource.operation in (OpType.DELETE, OpType.DISCARD):
                counts["to_delete"] += 1
        return counts

    def _operation_summary_counts_json(self) -> dict[str, int]:
        counts = {
            "created": 0,
            "updated": 0,
            "deleted": 0,
            "replaced": 0,
            "failed": 0,
            "unchanged": 0,
        }
        for resource in self.resources.values():
            if not self._is_resource_visible(resource):
                continue
            if resource.status == "failed":
                counts["failed"] += 1
            elif resource.has_replacement:
                counts["replaced"] += 1
            elif resource.operation == OpType.CREATE:
                counts["created"] += 1
            elif resource.operation == OpType.UPDATE:
                counts["updated"] += 1
            elif resource.operation in (OpType.DELETE, OpType.DISCARD):
                counts["deleted"] += 1
            else:
                counts["unchanged"] += 1
        return counts

    def _summary_counts_json(self) -> dict[str, int]:
        if self.is_preview:
            return self._preview_summary_counts_json()
        return self._operation_summary_counts_json()

    def _warning_json(self, warning: WarningInfo) -> dict[str, JsonValue]:
        data: dict[str, JsonValue] = {"message": warning.message}
        if warning.hint:
            data["hint"] = warning.hint
        if not warning.urn:
            return data

        component = self._components_by_urn.get(warning.urn)
        if component:
            data["component"] = component.component_type
            data["name"] = component.name
            return data

        resource = self.resources.get(warning.urn)
        if resource:
            data["resource"] = resource.type
            component_urn = self.resource_to_component.get(warning.urn)
            if component_urn and component_urn in self._components_by_urn:
                parent = self._components_by_urn[component_urn]
                data["component"] = parent.component_type
                data["name"] = parent.name
            return data

        parsed_component = _parse_stelvio_parent(warning.urn)
        if parsed_component:
            data["component"] = parsed_component[0]
            data["name"] = parsed_component[1]
        return data

    def _errors_json(self, fallback_error: str | None = None) -> list[dict[str, JsonValue]]:
        errors: list[dict[str, JsonValue]] = []
        seen: set[tuple[str | None, str | None, str | None, str]] = set()

        for urn, resource in self.resources.items():
            if not resource.error:
                continue
            error_data: dict[str, JsonValue] = {
                "resource": resource.type,
                "message": resource.error,
            }
            component_urn = self.resource_to_component.get(urn)
            component = self._components_by_urn.get(component_urn) if component_urn else None
            if component:
                error_data["component"] = component.component_type
                error_data["name"] = component.name
            key = (
                cast("str | None", error_data.get("component")),
                cast("str | None", error_data.get("name")),
                cast("str | None", error_data.get("resource")),
                resource.error,
            )
            if key not in seen:
                seen.add(key)
                errors.append(error_data)

        if not errors and fallback_error:
            errors.append({"message": fallback_error})
        return errors

    def build_json_summary(
        self,
        *,
        status: Literal["success", "failed"] = "success",
        outputs: dict[str, JsonValue] | None = None,
        exit_code: int = 0,
        fallback_error: str | None = None,
        message: str | None = None,
    ) -> dict[str, JsonValue]:
        components = [self._component_json(component) for component in self.components.values()]
        if self.is_preview and status == "failed":
            components = [
                component
                for component in components
                if component.get("resources")
                or component.get("components")
                or component.get("error")
            ]

        payload: dict[str, JsonValue] = {
            "operation": "diff" if self.is_preview else self.operation,
            "app": self.app_name,
            "env": self.environment,
            "status": status,
            "duration": round((datetime.now() - self.start_time).total_seconds(), 1),
            "exit_code": exit_code,
            "components": components,
            "summary": self._summary_counts_json(),
            "warnings": [self._warning_json(warning) for warning in self.warning_diagnostics],
            "errors": self._errors_json(fallback_error=fallback_error),
        }
        other_resources = self._other_resources_json()
        if other_resources:
            payload["other_resources"] = other_resources
        if outputs is not None and not self.is_preview:
            payload["outputs"] = outputs
        if message is not None:
            payload["message"] = message
        return payload
