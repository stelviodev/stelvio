from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Literal

from pulumi.automation import EngineEvent, OpType, OutputValue, StepEventMetadata

if TYPE_CHECKING:
    from collections.abc import MutableMapping
from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.spinner import Spinner
from rich.text import Text

logger = logging.getLogger(__name__)

# Constants for URN parsing
MIN_URN_PARTS_FOR_NAME = 4  # urn:pulumi:stack::project::type::name
MIN_URN_PARTS_FOR_TYPE = 3  # urn:pulumi:stack::project::type
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
    component: ComponentInfo, is_preview: bool, duration_str: str = ""
) -> Text:
    """Format a component header line: ✓ Function  api-handler  (2.1s)"""
    if component.status == "failed":
        prefix, color = "✗ ", "red"
    else:
        prefix, _, color = get_operation_display(component.operation, component.status, is_preview)

    line = Text()
    line.append(prefix, style=color)
    line.append(component.component_type, style="bold")
    line.append(f"  {component.name}")

    if duration_str:
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

        resource = ResourceInfo(
            logical_name=logical_name,
            type=metadata.type,
            operation=metadata.op,
            status="active",
            start_time=event.timestamp,
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

    def _render(self) -> RenderableType:
        content = Text()

        if len(self.resources) > 0:
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

        if not expanded or comp.status == "completed":
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

    def _render_children(self, content: Text, comp: ComponentInfo, indent: int) -> None:
        """Render children (resources and sub-components) of a component."""
        for child in comp.children:
            if isinstance(child, ComponentInfo):
                self._render_component(content, child, expanded=True, indent=indent)
            else:
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

    def _print_component_summary(self, comp: ComponentInfo, indent: int = 0) -> None:
        """Print a single component in the final summary."""
        duration_str = _calculate_component_duration(comp) if not self.is_preview else ""
        indent_str = "    " * indent
        header = format_component_header(comp, self.is_preview, duration_str)
        self.console.print(Text(indent_str) + header)

        # Show children for failed components (so errors are visible)
        if comp.status == "failed":
            for child in comp.children:
                if isinstance(child, ComponentInfo):
                    if child.status == "failed":
                        self._print_component_summary(child, indent=indent + 1)
                elif child.status == "failed":
                    child_duration = _calculate_duration(child) if not self.is_preview else ""
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
                total_resources=self.total_resources,
                component_count=len(self.components),
                summary_verb=self.summary_verb,
            )
            if counts_text:
                self.console.print(counts_text)
