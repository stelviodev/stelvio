"""Data types, constants, and parsers for deployment event handling.

Defines the core data model (ResourceInfo, ComponentInfo, WarningInfo),
resource type maps, URN parsing, and resource/component grouping utilities.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Literal

from pulumi.automation import DiffKind, OpType

if TYPE_CHECKING:
    from collections.abc import Mapping

    from pulumi.automation import PropertyDiff

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
    "aws:apigateway/account:Account": "API Gateway Account",
    "aws:apigateway/restApi:RestApi": "REST API",
    "aws:apigateway/deployment:Deployment": "API Deployment",
    "aws:apigateway/stage:Stage": "API Stage",
    "aws:apigateway/resource:Resource": "API Resource",
    "aws:apigateway/method:Method": "API Method",
    "aws:apigateway/integration:Integration": "API Integration",
    "aws:apigateway/methodResponse:MethodResponse": "API Method Response",
    "aws:apigateway/integrationResponse:IntegrationResponse": "API Integration Response",
    "aws:apigateway/response:Response": "API Gateway Response",
    "aws:apigateway/domainName:DomainName": "API Domain",
    "aws:apigateway/basePathMapping:BasePathMapping": "API Path Mapping",
    "aws:apigateway/authorizer:Authorizer": "API Authorizer",
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


@dataclass(frozen=True)
class WarningInfo:
    """User-facing warning captured from Pulumi diagnostics."""

    message: str
    urn: str | None = None
    hint: str | None = None


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
        return any(resource.has_replacement for resource in self.all_resources)

    @property
    def has_data_loss_replacement(self) -> bool:
        """True if any child replacement is likely destructive to persistent data."""
        return any(resource.has_data_loss_replacement for resource in self.all_resources)

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


# Matches provider warnings like:
# "... urn:pulumi:...::logical-name, interrupted while creating ...".
# Group 1 captures just the resource URN so we can attach warning context.
_INTERRUPTED_CREATE_WARNING_PATTERN = re.compile(
    r"(urn:pulumi:[^,]+),\s*interrupted while creating", re.IGNORECASE
)


def _clean_diagnostic_message(message: str) -> str:
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


def _interrupted_create_warning_urn(message: str) -> str | None:
    """Extract URN from Pulumi interrupted-create warning text."""
    match = _INTERRUPTED_CREATE_WARNING_PATTERN.search(message)
    if not match:
        return None
    return match.group(1)


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
        elif resource.operation in (OpType.SAME, OpType.READ, OpType.REFRESH):
            unchanged_resources.append(resource)
        else:
            changing_resources.append(resource)

    return changing_resources, unchanged_resources, failed_resources


def count_changed_resources(resources: dict[str, ResourceInfo]) -> int:
    """Count resources that actually changed or failed in this operation."""
    changing_resources, _, failed_resources = group_resources(resources)
    return len(changing_resources) + len(failed_resources)


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
        elif comp.operation in (OpType.SAME, OpType.READ, OpType.REFRESH):
            unchanged.append(comp)
        else:
            changing.append(comp)

    return changing, unchanged, failed
