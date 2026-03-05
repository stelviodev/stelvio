"""Tests for RichDeploymentHandler component grouping (Phase 1 CLI redesign)."""

import itertools

import pytest
from pulumi.automation import OpType
from pulumi.automation.events import (
    EngineEvent,
    ResOpFailedEvent,
    ResourcePreEvent,
    ResOutputsEvent,
    StepEventMetadata,
    StepEventStateMetadata,
)

from stelvio.rich_deployment_handler import (
    ComponentInfo,
    ResourceInfo,
    RichDeploymentHandler,
    _parse_stelvio_parent,
    _readable_type,
    format_child_resource_line,
    format_component_header,
    group_components,
)

# ---------------------------------------------------------------------------
# URN helpers
# ---------------------------------------------------------------------------
STACK = "dev"
PROJECT = "myapp"


def _component_urn(component_type: str, name: str) -> str:
    return f"urn:pulumi:{STACK}::{PROJECT}::stelvio:aws:{component_type}::{name}"


def _resource_urn(resource_type: str, name: str, parent_type: str | None = None) -> str:
    """Build a resource URN. If parent_type given, nest under it in the type path."""
    if parent_type:
        return f"urn:pulumi:{STACK}::{PROJECT}::stelvio:aws:{parent_type}${resource_type}::{name}"
    return f"urn:pulumi:{STACK}::{PROJECT}::{resource_type}::{name}"


# ---------------------------------------------------------------------------
# Event factories
# ---------------------------------------------------------------------------
_seq_counter = itertools.count(1)


def _next_seq() -> int:
    return next(_seq_counter)


def _make_state(urn: str, resource_type: str, parent_urn: str = "") -> StepEventStateMetadata:
    return StepEventStateMetadata(
        type=resource_type,
        urn=urn,
        id="some-id",
        parent=parent_urn,
        provider="urn:pulumi:dev::myapp::pulumi:providers:aws::default",
    )


def _pre_event(
    urn: str,
    resource_type: str,
    op: OpType = OpType.CREATE,
    parent_urn: str = "",
    timestamp: int = 1000,
) -> EngineEvent:
    metadata = StepEventMetadata(
        op=op,
        urn=urn,
        type=resource_type,
        provider="urn:pulumi:dev::myapp::pulumi:providers:aws::default",
        new=_make_state(urn, resource_type, parent_urn),
    )
    return EngineEvent(
        sequence=_next_seq(),
        timestamp=timestamp,
        resource_pre_event=ResourcePreEvent(metadata=metadata),
    )


def _outputs_event(  # noqa: PLR0913
    urn: str,
    resource_type: str,
    op: OpType = OpType.CREATE,
    parent_urn: str = "",
    timestamp: int = 1001,
    diffs: list[str] | None = None,
) -> EngineEvent:
    metadata = StepEventMetadata(
        op=op,
        urn=urn,
        type=resource_type,
        provider="urn:pulumi:dev::myapp::pulumi:providers:aws::default",
        new=_make_state(urn, resource_type, parent_urn),
        diffs=diffs,
    )
    return EngineEvent(
        sequence=_next_seq(),
        timestamp=timestamp,
        res_outputs_event=ResOutputsEvent(metadata=metadata),
    )


def _failed_event(
    urn: str,
    resource_type: str,
    timestamp: int = 1001,
) -> EngineEvent:
    metadata = StepEventMetadata(
        op=OpType.CREATE,
        urn=urn,
        type=resource_type,
        provider="urn:pulumi:dev::myapp::pulumi:providers:aws::default",
    )
    return EngineEvent(
        sequence=_next_seq(),
        timestamp=timestamp,
        res_op_failed_event=ResOpFailedEvent(metadata=metadata, status=1, steps=1),
    )


# ---------------------------------------------------------------------------
# Fixture: handler that doesn't start Rich Live display
# ---------------------------------------------------------------------------
@pytest.fixture
def handler(monkeypatch):
    """Create a handler with Live display disabled for testing."""
    # Prevent Live.start() from actually starting terminal output
    from rich.live import Live

    monkeypatch.setattr(Live, "start", lambda self: None)
    monkeypatch.setattr(Live, "stop", lambda self: None)

    return RichDeploymentHandler("myapp", "dev", "deploy")


# ===========================================================================
# _parse_stelvio_parent
# ===========================================================================
class TestParseStelvioParent:
    def test_function_component(self):
        urn = _component_urn("Function", "api-handler")
        result = _parse_stelvio_parent(urn)
        assert result == ("Function", "api-handler")

    def test_dynamo_table_component(self):
        urn = _component_urn("DynamoTable", "users")
        result = _parse_stelvio_parent(urn)
        assert result == ("DynamoTable", "users")

    def test_non_stelvio_urn_returns_none(self):
        urn = f"urn:pulumi:{STACK}::{PROJECT}::aws:lambda:Function::my-func"
        assert _parse_stelvio_parent(urn) is None

    def test_pulumi_stack_returns_none(self):
        urn = f"urn:pulumi:{STACK}::{PROJECT}::pulumi:pulumi:Stack::dev"
        assert _parse_stelvio_parent(urn) is None

    def test_short_urn_returns_none(self):
        assert _parse_stelvio_parent("urn:pulumi:dev") is None


# ===========================================================================
# ComponentInfo properties
# ===========================================================================
class TestComponentInfo:
    def _make_component(self, *statuses: str) -> ComponentInfo:
        children = [
            ResourceInfo(
                logical_name=f"res-{i}",
                type="aws:lambda:Function",
                operation=OpType.CREATE,
                status=s,
                start_time=1000,
                end_time=1001 if s != "active" else None,
            )
            for i, s in enumerate(statuses)
        ]
        return ComponentInfo(
            component_type="Function",
            name="api",
            urn=_component_urn("Function", "api"),
            children=children,
        )

    def test_status_all_completed(self):
        comp = self._make_component("completed", "completed")
        assert comp.status == "completed"

    def test_status_any_active(self):
        comp = self._make_component("completed", "active")
        assert comp.status == "active"

    def test_status_any_failed(self):
        comp = self._make_component("completed", "failed")
        assert comp.status == "failed"

    def test_status_failed_beats_active(self):
        comp = self._make_component("active", "failed")
        assert comp.status == "failed"

    def test_status_no_children(self):
        comp = self._make_component()
        assert comp.status == "active"

    def test_end_time_when_all_done(self):
        comp = self._make_component("completed", "completed")
        comp.children[0].end_time = 1001
        comp.children[1].end_time = 1005
        assert comp.end_time == 1005

    def test_end_time_none_when_active(self):
        comp = self._make_component("completed", "active")
        assert comp.end_time is None

    def test_operation_highest_priority_wins(self):
        comp = self._make_component("completed", "completed")
        comp.children[0].operation = OpType.SAME
        comp.children[1].operation = OpType.UPDATE
        assert comp.operation == OpType.UPDATE

    def test_operation_create_over_same(self):
        comp = self._make_component("completed", "completed")
        comp.children[0].operation = OpType.CREATE
        comp.children[1].operation = OpType.SAME
        assert comp.operation == OpType.CREATE

    def test_error_from_failed_child(self):
        comp = self._make_component("failed")
        comp.children[0].error = "boom"
        assert comp.error == "boom"

    def test_error_none_when_no_failures(self):
        comp = self._make_component("completed")
        assert comp.error is None


# ===========================================================================
# Event handling: component grouping
# ===========================================================================
class TestComponentGrouping:
    def test_resource_grouped_under_parent_component(self, handler):
        parent_urn = _component_urn("Function", "api")
        resource_urn = _resource_urn("aws:iam:Role", "api-role", "Function")

        handler.handle_event(_pre_event(resource_urn, "aws:iam:Role", parent_urn=parent_urn))

        assert parent_urn in handler.components
        comp = handler.components[parent_urn]
        assert comp.component_type == "Function"
        assert comp.name == "api"
        assert len(comp.children) == 1
        assert comp.children[0].type == "aws:iam:Role"

    def test_multiple_children_same_component(self, handler):
        parent_urn = _component_urn("Function", "api")

        handler.handle_event(
            _pre_event(
                _resource_urn("aws:iam:Role", "api-role", "Function"),
                "aws:iam:Role",
                parent_urn=parent_urn,
            )
        )
        handler.handle_event(
            _pre_event(
                _resource_urn("aws:lambda:Function", "api-func", "Function"),
                "aws:lambda:Function",
                parent_urn=parent_urn,
            )
        )

        comp = handler.components[parent_urn]
        assert len(comp.children) == 2

    def test_multiple_components(self, handler):
        func_urn = _component_urn("Function", "api")
        table_urn = _component_urn("DynamoTable", "users")

        handler.handle_event(
            _pre_event(
                _resource_urn("aws:iam:Role", "api-role", "Function"),
                "aws:iam:Role",
                parent_urn=func_urn,
            )
        )
        handler.handle_event(
            _pre_event(
                _resource_urn("aws:dynamodb:Table", "users-table", "DynamoTable"),
                "aws:dynamodb:Table",
                parent_urn=table_urn,
            )
        )

        assert len(handler.components) == 2
        assert handler.components[func_urn].component_type == "Function"
        assert handler.components[table_urn].component_type == "DynamoTable"

    def test_orphan_resource_no_stelvio_parent(self, handler):
        # A raw Pulumi resource with no Stelvio parent
        urn = f"urn:pulumi:{STACK}::{PROJECT}::aws:s3:BucketV2::manual-bucket"
        handler.handle_event(_pre_event(urn, "aws:s3:BucketV2"))

        assert len(handler.components) == 0
        assert len(handler.orphan_resources) == 1
        assert handler.orphan_resources[0].type == "aws:s3:BucketV2"

    def test_pulumi_internals_skipped(self, handler):
        handler.handle_event(
            _pre_event(
                f"urn:pulumi:{STACK}::{PROJECT}::pulumi:pulumi:Stack::dev",
                "pulumi:pulumi:Stack",
            )
        )
        handler.handle_event(
            _pre_event(
                f"urn:pulumi:{STACK}::{PROJECT}::pulumi:providers:aws::default",
                "pulumi:providers:aws",
            )
        )

        assert len(handler.resources) == 0
        assert len(handler.components) == 0

    def test_stelvio_component_resource_itself_skipped(self, handler):
        """The ComponentResource event itself should not appear as a tracked resource."""
        comp_urn = _component_urn("Function", "api")
        handler.handle_event(_pre_event(comp_urn, "stelvio:aws:Function"))

        assert comp_urn not in handler.resources
        assert len(handler.resources) == 0

    def test_component_status_updates_with_child_outputs(self, handler):
        parent_urn = _component_urn("Function", "api")
        role_urn = _resource_urn("aws:iam:Role", "api-role", "Function")
        func_urn = _resource_urn("aws:lambda:Function", "api-func", "Function")

        # Start both resources
        handler.handle_event(_pre_event(role_urn, "aws:iam:Role", parent_urn=parent_urn))
        handler.handle_event(_pre_event(func_urn, "aws:lambda:Function", parent_urn=parent_urn))

        comp = handler.components[parent_urn]
        assert comp.status == "active"

        # Complete one
        handler.handle_event(_outputs_event(role_urn, "aws:iam:Role"))
        assert comp.status == "active"  # still active — func not done

        # Complete the other
        handler.handle_event(_outputs_event(func_urn, "aws:lambda:Function"))
        assert comp.status == "completed"

    def test_component_status_failed_when_child_fails(self, handler):
        parent_urn = _component_urn("Function", "api")
        role_urn = _resource_urn("aws:iam:Role", "api-role", "Function")

        handler.handle_event(_pre_event(role_urn, "aws:iam:Role", parent_urn=parent_urn))
        handler.handle_event(_failed_event(role_urn, "aws:iam:Role"))

        assert handler.components[parent_urn].status == "failed"

    def test_resource_to_component_mapping(self, handler):
        parent_urn = _component_urn("Function", "api")
        role_urn = _resource_urn("aws:iam:Role", "api-role", "Function")

        handler.handle_event(_pre_event(role_urn, "aws:iam:Role", parent_urn=parent_urn))

        assert handler.resource_to_component[role_urn] == parent_urn

    def test_duplicate_events_ignored(self, handler):
        parent_urn = _component_urn("Function", "api")
        role_urn = _resource_urn("aws:iam:Role", "api-role", "Function")

        event = _pre_event(role_urn, "aws:iam:Role", parent_urn=parent_urn)
        handler.handle_event(event)
        handler.handle_event(event)  # duplicate

        assert len(handler.components[parent_urn].children) == 1
        assert handler.total_resources == 1


# ===========================================================================
# Readable type names
# ===========================================================================
class TestReadableType:
    def test_known_types(self):
        assert _readable_type("aws:lambda/function:Function") == "Lambda Function"
        assert _readable_type("aws:iam/role:Role") == "IAM Role"
        assert _readable_type("aws:dynamodb/table:Table") == "DynamoDB Table"
        assert _readable_type("aws:s3/bucketV2:BucketV2") == "S3 Bucket"

    def test_unknown_type_falls_back(self):
        assert _readable_type("aws:custom/thing:Thing") == "aws:custom/thing:Thing"


# ===========================================================================
# Formatting functions
# ===========================================================================
class TestFormatComponentHeader:
    def _make_comp(self, status: str = "completed", op: OpType = OpType.CREATE) -> ComponentInfo:
        child = ResourceInfo(
            logical_name="res",
            type="aws:lambda:Function",
            operation=op,
            status=status,
            start_time=1000,
            end_time=1001 if status != "active" else None,
        )
        return ComponentInfo(
            component_type="Function",
            name="api",
            urn=_component_urn("Function", "api"),
            children=[child],
        )

    def test_completed_create_header(self):
        comp = self._make_comp("completed", OpType.CREATE)
        text = format_component_header(comp, is_preview=False, duration_str="(2.1s)")
        plain = text.plain
        assert "Function" in plain
        assert "api" in plain
        assert "(2.1s)" in plain
        assert plain.startswith("✓")

    def test_active_header(self):
        comp = self._make_comp("active", OpType.CREATE)
        text = format_component_header(comp, is_preview=False)
        assert text.plain.startswith("|")

    def test_failed_header(self):
        comp = self._make_comp("failed", OpType.CREATE)
        text = format_component_header(comp, is_preview=False)
        assert text.plain.startswith("✗")

    def test_preview_header(self):
        comp = self._make_comp("completed", OpType.CREATE)
        text = format_component_header(comp, is_preview=True)
        assert text.plain.startswith("+")


class TestFormatChildResourceLine:
    def test_completed_child(self):
        resource = ResourceInfo(
            logical_name="role",
            type="aws:iam/role:Role",
            operation=OpType.CREATE,
            status="completed",
            start_time=1000,
            end_time=1001,
        )
        text = format_child_resource_line(resource, is_preview=False, duration_str="(1.0s)")
        plain = text.plain
        assert plain.startswith("    ")  # 4-space indent
        assert "IAM Role" in plain  # readable type name
        assert "(1.0s)" in plain

    def test_failed_child(self):
        resource = ResourceInfo(
            logical_name="func",
            type="aws:lambda/function:Function",
            operation=OpType.CREATE,
            status="failed",
            start_time=1000,
        )
        text = format_child_resource_line(resource, is_preview=False)
        plain = text.plain
        assert "✗" in plain
        assert "Lambda Function" in plain


# ===========================================================================
# group_components
# ===========================================================================
class TestGroupComponents:
    def _comp(self, status: str, op: OpType = OpType.CREATE) -> ComponentInfo:
        child = ResourceInfo(
            logical_name="r",
            type="aws:lambda:Function",
            operation=op,
            status=status,
            start_time=1000,
            end_time=1001 if status != "active" else None,
        )
        return ComponentInfo(
            component_type="Function",
            name="f",
            urn=_component_urn("Function", "f"),
            children=[child],
        )

    def test_groups_correctly(self):
        comps = {
            "a": self._comp("completed", OpType.CREATE),
            "b": self._comp("completed", OpType.SAME),
            "c": self._comp("failed"),
        }
        changing, unchanged, failed = group_components(comps)
        assert len(changing) == 1
        assert len(unchanged) == 1
        assert len(failed) == 1


# ===========================================================================
# Progress counter (component-level)
# ===========================================================================
class TestProgressCounter:
    def test_progress_counts_components(self, handler):
        func_urn = _component_urn("Function", "api")
        table_urn = _component_urn("DynamoTable", "users")

        # Add resources to two components
        role_urn = _resource_urn("aws:iam:Role", "api-role", "Function")
        lambda_urn = _resource_urn("aws:lambda:Function", "api-func", "Function")
        table_res_urn = _resource_urn("aws:dynamodb:Table", "users-table", "DynamoTable")

        handler.handle_event(_pre_event(role_urn, "aws:iam:Role", parent_urn=func_urn))
        handler.handle_event(_pre_event(lambda_urn, "aws:lambda:Function", parent_urn=func_urn))
        handler.handle_event(_pre_event(table_res_urn, "aws:dynamodb:Table", parent_urn=table_urn))

        assert len(handler.components) == 2

        # Complete the table component
        handler.handle_event(_outputs_event(table_res_urn, "aws:dynamodb:Table"))
        completed = sum(
            1 for c in handler.components.values() if c.status in ("completed", "failed")
        )
        assert completed == 1  # table done, function still active
