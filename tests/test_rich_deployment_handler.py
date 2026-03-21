"""Tests for RichDeploymentHandler (Phase 1 + Phase 2 CLI redesign)."""

import itertools
import sys
from unittest.mock import Mock

import pytest
from pulumi.automation import DiffKind, OpType, PropertyDiff
from pulumi.automation.events import (
    DiagnosticEvent,
    EngineEvent,
    ResOpFailedEvent,
    ResourcePreEvent,
    ResOutputsEvent,
    StepEventMetadata,
    StepEventStateMetadata,
)
from rich.console import Console

from stelvio.rich_deployment_handler import (
    ComponentInfo,
    ResourceInfo,
    RichDeploymentHandler,
    WarningInfo,
    _clean_diagnostic_message,
    _get_nested_value,
    _parse_stelvio_parent,
    _readable_type,
    build_preview_counts_text,
    format_child_resource_line,
    format_component_header,
    format_property_diff_lines,
    format_replacement_warning,
    group_components,
)

# ---------------------------------------------------------------------------
# URN helpers
# ---------------------------------------------------------------------------
STACK = "dev"
PROJECT = "myapp"
STACK_URN = f"urn:pulumi:{STACK}::{PROJECT}::pulumi:pulumi:Stack::{STACK}"


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


@pytest.fixture(autouse=True)
def reset_sequence_counter(monkeypatch):
    """Reset event sequence counter to avoid inter-test coupling."""
    monkeypatch.setattr(sys.modules[__name__], "_seq_counter", itertools.count(1))


def _make_state(
    urn: str,
    resource_type: str,
    parent_urn: str = "",
    inputs: dict | None = None,
) -> StepEventStateMetadata:
    return StepEventStateMetadata(
        type=resource_type,
        urn=urn,
        id="some-id",
        parent=parent_urn,
        provider="urn:pulumi:dev::myapp::pulumi:providers:aws::default",
        inputs=inputs,
    )


def _pre_event(  # noqa: PLR0913
    urn: str,
    resource_type: str,
    op: OpType = OpType.CREATE,
    parent_urn: str = "",
    timestamp: int = 1000,
    diffs: list[str] | None = None,
    keys: list[str] | None = None,
    detailed_diff: dict[str, PropertyDiff] | None = None,
    old_inputs: dict | None = None,
    new_inputs: dict | None = None,
) -> EngineEvent:
    metadata = StepEventMetadata(
        op=op,
        urn=urn,
        type=resource_type,
        provider="urn:pulumi:dev::myapp::pulumi:providers:aws::default",
        new=_make_state(urn, resource_type, parent_urn, inputs=new_inputs),
        old=_make_state(urn, resource_type, parent_urn, inputs=old_inputs) if old_inputs else None,
        diffs=diffs,
        keys=keys,
        detailed_diff=detailed_diff,
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
    keys: list[str] | None = None,
    detailed_diff: dict[str, PropertyDiff] | None = None,
    old_inputs: dict | None = None,
    new_inputs: dict | None = None,
) -> EngineEvent:
    metadata = StepEventMetadata(
        op=op,
        urn=urn,
        type=resource_type,
        provider="urn:pulumi:dev::myapp::pulumi:providers:aws::default",
        new=_make_state(urn, resource_type, parent_urn, inputs=new_inputs),
        old=_make_state(urn, resource_type, parent_urn, inputs=old_inputs) if old_inputs else None,
        keys=keys,
        diffs=diffs,
        detailed_diff=detailed_diff,
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


def _render_content_text(handler: RichDeploymentHandler) -> str:
    """Return the textual content portion of the live render output."""
    renderable = handler._render()
    return renderable.renderables[0].plain


def _render_spinner_text(handler: RichDeploymentHandler) -> str:
    """Return the spinner footer text from the live render output."""
    handler._render()
    return str(handler.spinner.text)


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


@pytest.fixture
def preview_handler(monkeypatch):
    """Create a preview handler with Live display disabled for testing."""
    from rich.live import Live

    monkeypatch.setattr(Live, "start", lambda self: None)
    monkeypatch.setattr(Live, "stop", lambda self: None)

    return RichDeploymentHandler("myapp", "dev", "preview")


@pytest.fixture
def compact_preview_handler(monkeypatch):
    """Create a compact preview handler with Live display disabled for testing."""
    from rich.live import Live

    monkeypatch.setattr(Live, "start", lambda self: None)
    monkeypatch.setattr(Live, "stop", lambda self: None)

    return RichDeploymentHandler("myapp", "dev", "preview", compact=True)


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

    def test_nested_component_urn_extracts_leaf_type(self):
        """Nested URNs use $ separator — should extract the leaf type only."""
        urn = (
            f"urn:pulumi:{STACK}::{PROJECT}"
            "::stelvio:aws:TopicSubscription$stelvio:aws:Function::on-notify"
        )
        result = _parse_stelvio_parent(urn)
        assert result == ("Function", "on-notify")


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
        assert isinstance(comp.children[0], ResourceInfo)
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

    def test_duplicate_top_level_component_events_ignored(self, handler):
        """Duplicate Stelvio component events should not create duplicates."""
        comp_urn = _component_urn("Function", "api")
        event = _pre_event(comp_urn, "stelvio:aws:Function", parent_urn=STACK_URN)

        handler.handle_event(event)
        handler.handle_event(event)

        assert len(handler.components) == 1
        assert set(handler.components.keys()) == {comp_urn}

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


# --- Nested component parent resolution ---
class TestNestedComponentParentResolution:
    """When a component creates a Function internally with parent=self,
    the Function appears as a sub-component in the tree.
    E.g. TopicSubscription has a Function child, which has Lambda/IAM children.
    """

    def _register_component(
        self,
        handler: RichDeploymentHandler,
        comp_type: str,
        name: str,
        parent_urn: str = STACK_URN,
    ) -> str:
        """Register a Stelvio component event and return its URN."""
        urn = _component_urn(comp_type, name)
        handler.handle_event(_pre_event(urn, f"stelvio:aws:{comp_type}", parent_urn=parent_urn))
        return urn

    def _setup_subscription_with_function(self, handler: RichDeploymentHandler) -> tuple[str, str]:
        """Register a TopicSubscription with a nested Function child."""
        sub_urn = self._register_component(handler, "TopicSubscription", "on-notify-sub")
        func_urn = self._register_component(handler, "Function", "on-notify", parent_urn=sub_urn)
        return sub_urn, func_urn

    def test_nested_function_is_child_component_of_subscription(self, handler):
        """Function created by TopicSubscription should appear as a child ComponentInfo."""
        sub_urn, func_urn = self._setup_subscription_with_function(handler)

        role_urn = _resource_urn("aws:iam/role:Role", "on-notify-role", "Function")
        lambda_urn = _resource_urn("aws:lambda/function:Function", "on-notify-fn", "Function")
        handler.handle_event(_pre_event(role_urn, "aws:iam/role:Role", parent_urn=func_urn))
        handler.handle_event(
            _pre_event(lambda_urn, "aws:lambda/function:Function", parent_urn=func_urn)
        )

        # Function removed from top-level, only subscription remains
        assert func_urn not in handler.components
        assert len(handler.components) == 1

        comp = handler.components[sub_urn]
        assert comp.component_type == "TopicSubscription"

        # Subscription has one child: the nested Function component
        assert len(comp.children) == 1
        nested_func = comp.children[0]
        assert isinstance(nested_func, ComponentInfo)
        assert nested_func.component_type == "Function"
        assert nested_func.name == "on-notify"

        # Function has 2 resource children
        assert len(nested_func.children) == 2
        assert {c.type for c in nested_func.children} == {
            "aws:iam/role:Role",
            "aws:lambda/function:Function",
        }

    def test_out_of_order_child_component_before_parent_still_nests(self, handler):
        """Child component events may arrive first (e.g. destroy ordering)."""
        sub_urn = _component_urn("TopicSubscription", "on-notify-sub")
        func_urn = _component_urn("Function", "on-notify")

        # Child arrives before parent component event.
        handler.handle_event(_pre_event(func_urn, "stelvio:aws:Function", parent_urn=sub_urn))
        handler.handle_event(
            _pre_event(
                _resource_urn("aws:lambda/function:Function", "on-notify-fn", "Function"),
                "aws:lambda/function:Function",
                parent_urn=func_urn,
            )
        )
        handler.handle_event(
            _pre_event(sub_urn, "stelvio:aws:TopicSubscription", parent_urn=STACK_URN)
        )

        assert len(handler.components) == 1
        assert set(handler.components.keys()) == {sub_urn}
        sub_comp = handler.components[sub_urn]
        assert len(sub_comp.children) == 1
        assert isinstance(sub_comp.children[0], ComponentInfo)
        assert sub_comp.children[0].component_type == "Function"
        assert len(sub_comp.children[0].children) == 1

    def test_duplicate_nested_component_events_ignored(self, handler):
        """Duplicate nested component events should not duplicate tree nodes."""
        sub_urn = self._register_component(handler, "TopicSubscription", "on-notify-sub")
        func_urn = _component_urn("Function", "on-notify")
        event = _pre_event(func_urn, "stelvio:aws:Function", parent_urn=sub_urn)

        handler.handle_event(event)
        handler.handle_event(event)

        sub_components = [
            c
            for c in handler.components[sub_urn].children
            if isinstance(c, ComponentInfo) and c.component_type == "Function"
        ]
        assert len(sub_components) == 1

    def test_subscription_own_resources_still_grouped(self, handler):
        """Direct children of TopicSubscription should group directly under it."""
        sub_urn = self._register_component(handler, "TopicSubscription", "on-notify-sub")

        sns_sub_urn = _resource_urn(
            "aws:sns/topicSubscription:TopicSubscription", "on-notify-sub", "TopicSubscription"
        )
        handler.handle_event(
            _pre_event(
                sns_sub_urn, "aws:sns/topicSubscription:TopicSubscription", parent_urn=sub_urn
            )
        )

        assert len(handler.components) == 1
        children = handler.components[sub_urn].children
        assert len(children) == 1
        assert isinstance(children[0], ResourceInfo)
        assert children[0].type == "aws:sns/topicSubscription:TopicSubscription"

    def test_mixed_direct_and_nested_children(self, handler):
        """TopicSubscription should have both direct resources
        and a nested Function sub-component."""
        sub_urn, func_urn = self._setup_subscription_with_function(handler)

        # Direct child of subscription
        sns_sub_urn = _resource_urn(
            "aws:sns/topicSubscription:TopicSubscription", "on-notify-sub", "TopicSubscription"
        )
        handler.handle_event(
            _pre_event(
                sns_sub_urn, "aws:sns/topicSubscription:TopicSubscription", parent_urn=sub_urn
            )
        )

        # Nested child (Function's resource)
        lambda_urn = _resource_urn("aws:lambda/function:Function", "on-notify-fn", "Function")
        handler.handle_event(
            _pre_event(lambda_urn, "aws:lambda/function:Function", parent_urn=func_urn)
        )

        comp = handler.components[sub_urn]
        assert len(comp.children) == 2

        # One ResourceInfo (SNS subscription) and one ComponentInfo (Function)
        resources = [c for c in comp.children if isinstance(c, ResourceInfo)]
        sub_components = [c for c in comp.children if isinstance(c, ComponentInfo)]
        assert len(resources) == 1
        assert resources[0].type == "aws:sns/topicSubscription:TopicSubscription"
        assert len(sub_components) == 1
        assert sub_components[0].component_type == "Function"
        assert len(sub_components[0].children) == 1

    def test_all_resources_recursive(self, handler):
        """all_resources should collect resources from nested sub-components."""
        sub_urn, func_urn = self._setup_subscription_with_function(handler)

        # Direct resource
        sns_sub_urn = _resource_urn(
            "aws:sns/topicSubscription:TopicSubscription", "on-notify-sub", "TopicSubscription"
        )
        handler.handle_event(
            _pre_event(
                sns_sub_urn, "aws:sns/topicSubscription:TopicSubscription", parent_urn=sub_urn
            )
        )

        # Nested resource
        lambda_urn = _resource_urn("aws:lambda/function:Function", "on-notify-fn", "Function")
        handler.handle_event(
            _pre_event(lambda_urn, "aws:lambda/function:Function", parent_urn=func_urn)
        )

        all_res = handler.components[sub_urn].all_resources
        assert len(all_res) == 2
        assert {r.type for r in all_res} == {
            "aws:sns/topicSubscription:TopicSubscription",
            "aws:lambda/function:Function",
        }

    def test_api_route_function_nested_under_api(self, handler):
        """Function created by Api for a route should be a child component of Api."""
        api_urn = self._register_component(handler, "Api", "my-api")
        self._register_component(handler, "Function", "my-api-get-users", parent_urn=api_urn)

        handler.handle_event(
            _pre_event(
                _resource_urn("aws:iam/role:Role", "get-users-role", "Function"),
                "aws:iam/role:Role",
                parent_urn=_component_urn("Function", "my-api-get-users"),
            )
        )

        assert len(handler.components) == 1
        api_comp = handler.components[api_urn]
        assert api_comp.component_type == "Api"
        assert len(api_comp.children) == 1
        assert isinstance(api_comp.children[0], ComponentInfo)
        assert api_comp.children[0].component_type == "Function"
        assert len(api_comp.children[0].children) == 1

    def test_top_level_function_not_affected(self, handler):
        """User-created top-level Function should still group normally."""
        func_urn = self._register_component(handler, "Function", "api")

        role_urn = _resource_urn("aws:iam/role:Role", "api-role", "Function")
        handler.handle_event(_pre_event(role_urn, "aws:iam/role:Role", parent_urn=func_urn))

        assert len(handler.components) == 1
        comp = handler.components[func_urn]
        assert comp.component_type == "Function"
        assert comp.name == "api"
        assert len(comp.children) == 1
        assert isinstance(comp.children[0], ResourceInfo)

    def test_component_event_still_not_tracked_as_resource(self, handler):
        """Stelvio component events should still be skipped from resource tracking."""
        sub_urn, func_urn = self._setup_subscription_with_function(handler)

        assert sub_urn not in handler.resources
        assert func_urn not in handler.resources
        assert len(handler.resources) == 0

    def test_cron_function_nested_under_cron(self, handler):
        """Function created by Cron should be a child component of Cron."""
        cron_urn = self._register_component(handler, "Cron", "daily-cleanup")
        func_urn = self._register_component(
            handler, "Function", "daily-cleanup-fn", parent_urn=cron_urn
        )

        lambda_urn = _resource_urn("aws:lambda/function:Function", "cleanup-fn", "Function")
        handler.handle_event(
            _pre_event(lambda_urn, "aws:lambda/function:Function", parent_urn=func_urn)
        )

        assert func_urn not in handler.components
        assert len(handler.components) == 1
        cron_comp = handler.components[cron_urn]
        assert cron_comp.component_type == "Cron"
        assert len(cron_comp.children) == 1
        assert isinstance(cron_comp.children[0], ComponentInfo)
        assert cron_comp.children[0].component_type == "Function"

    def test_top_level_component_count_excludes_nested(self, handler):
        """Only top-level components should be in handler.components."""
        sub_urn, func_urn = self._setup_subscription_with_function(handler)

        handler.handle_event(
            _pre_event(
                _resource_urn("aws:lambda/function:Function", "on-notify-fn", "Function"),
                "aws:lambda/function:Function",
                parent_urn=func_urn,
            )
        )

        top_func_urn = self._register_component(handler, "Function", "api")
        handler.handle_event(
            _pre_event(
                _resource_urn("aws:lambda/function:Function", "api-fn", "Function"),
                "aws:lambda/function:Function",
                parent_urn=top_func_urn,
            )
        )

        assert len(handler.components) == 2
        assert set(handler.components.keys()) == {sub_urn, top_func_urn}

    def test_nested_function_completion_updates_outer_status(self, handler):
        """When nested Function's child resources complete,
        the outer component status should transition to completed."""
        sub_urn, func_urn = self._setup_subscription_with_function(handler)

        lambda_urn = _resource_urn("aws:lambda/function:Function", "on-notify-fn", "Function")
        handler.handle_event(
            _pre_event(lambda_urn, "aws:lambda/function:Function", parent_urn=func_urn)
        )

        assert handler.components[sub_urn].status == "active"

        handler.handle_event(
            _outputs_event(lambda_urn, "aws:lambda/function:Function", timestamp=1002)
        )

        assert handler.components[sub_urn].status == "completed"

    def test_nested_function_failure_propagates_to_outer(self, handler):
        """When a nested Function's resource fails, the outer component shows failed."""
        sub_urn, func_urn = self._setup_subscription_with_function(handler)

        lambda_urn = _resource_urn("aws:lambda/function:Function", "on-notify-fn", "Function")
        handler.handle_event(
            _pre_event(lambda_urn, "aws:lambda/function:Function", parent_urn=func_urn)
        )
        handler.handle_event(_failed_event(lambda_urn, "aws:lambda/function:Function"))

        assert handler.components[sub_urn].status == "failed"

    def test_total_resources_counts_nested_resources(self, handler):
        """total_resources should count resources inside nested components."""
        sub_urn, func_urn = self._setup_subscription_with_function(handler)

        # Direct resource under subscription
        handler.handle_event(
            _pre_event(
                _resource_urn(
                    "aws:sns/topicSubscription:TopicSubscription", "sub", "TopicSubscription"
                ),
                "aws:sns/topicSubscription:TopicSubscription",
                parent_urn=sub_urn,
            )
        )

        # Resources under nested Function
        handler.handle_event(
            _pre_event(
                _resource_urn("aws:iam/role:Role", "role", "Function"),
                "aws:iam/role:Role",
                parent_urn=func_urn,
            )
        )
        handler.handle_event(
            _pre_event(
                _resource_urn("aws:lambda/function:Function", "fn", "Function"),
                "aws:lambda/function:Function",
                parent_urn=func_urn,
            )
        )

        assert handler.total_resources == 3


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


class TestNestedTreeRendering:
    def test_render_shows_nested_component_indentation(self, handler):
        sub_urn = _component_urn("TopicSubscription", "on-notify-sub")
        func_urn = _component_urn("Function", "on-notify")

        handler.handle_event(
            _pre_event(sub_urn, "stelvio:aws:TopicSubscription", parent_urn=STACK_URN)
        )
        handler.handle_event(_pre_event(func_urn, "stelvio:aws:Function", parent_urn=sub_urn))
        handler.handle_event(
            _pre_event(
                _resource_urn("aws:lambda/function:Function", "on-notify-fn", "Function"),
                "aws:lambda/function:Function",
                parent_urn=func_urn,
            )
        )

        content = _render_content_text(handler)
        assert "| TopicSubscription  on-notify-sub" in content
        assert "\n    | Function  on-notify" in content
        assert "\n        | Lambda Function" in content


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

    def test_empty_component_is_treated_as_unchanged_placeholder(self):
        empty = ComponentInfo(
            component_type="Queue",
            name="tasks",
            urn=_component_urn("Queue", "tasks"),
            children=[],
        )
        changing, unchanged, failed = group_components({"q": empty})
        assert not changing
        assert unchanged == [empty]
        assert not failed


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

    def test_noop_deploy_footer_omits_component_counter_when_only_unchanged_hidden(self, handler):
        parent_urn = _component_urn("Function", "api")
        lambda_urn = _resource_urn("aws:lambda/function:Function", "api-fn", "Function")

        handler.handle_event(
            _pre_event(
                lambda_urn,
                "aws:lambda/function:Function",
                op=OpType.SAME,
                parent_urn=parent_urn,
            )
        )
        handler.handle_event(
            _outputs_event(
                lambda_urn,
                "aws:lambda/function:Function",
                op=OpType.SAME,
            )
        )

        assert _render_content_text(handler) == ""
        assert _render_spinner_text(handler).startswith("Deploying  ")
        assert "complete" not in _render_spinner_text(handler)

    def test_deploy_footer_keeps_component_counter_for_visible_changes(self, handler):
        parent_urn = _component_urn("Function", "api")
        lambda_urn = _resource_urn("aws:lambda/function:Function", "api-fn", "Function")

        handler.handle_event(
            _pre_event(
                lambda_urn,
                "aws:lambda/function:Function",
                op=OpType.UPDATE,
                parent_urn=parent_urn,
            )
        )
        handler.handle_event(
            _outputs_event(
                lambda_urn,
                "aws:lambda/function:Function",
                op=OpType.UPDATE,
            )
        )

        assert "Function" in _render_content_text(handler)
        assert _render_spinner_text(handler).startswith("Deploying  1/1 complete")


# ===========================================================================
# Phase 2: Property diffs and replacement warnings
# ===========================================================================


def _pdiff(kind: DiffKind) -> PropertyDiff:
    return PropertyDiff(diff_kind=kind, input_diff=False)


# --- _get_nested_value ---


def test_nested_value_simple_key():
    assert _get_nested_value({"foo": 42}, "foo") == 42


def test_nested_value_dot_path():
    assert _get_nested_value({"tags": {"Name": "test"}}, "tags.Name") == "test"


def test_nested_value_missing_returns_none():
    assert _get_nested_value({"foo": 1}, "bar") is None


def test_nested_value_none_inputs():
    assert _get_nested_value(None, "foo") is None


def test_nested_value_deep():
    assert _get_nested_value({"a": {"b": {"c": "deep"}}}, "a.b.c") == "deep"


def test_nested_value_bracket_index_path():
    assert (
        _get_nested_value(
            {"Statement": [{"Resource": "arn:one"}, {"Resource": "arn:two"}]},
            "Statement[1].Resource",
        )
        == "arn:two"
    )


# --- ResourceInfo.has_replacement ---


def test_no_diff_means_no_replacement():
    r = ResourceInfo(
        logical_name="t",
        type="aws:dynamodb/table:Table",
        operation=OpType.UPDATE,
        status="completed",
        start_time=1000,
    )
    assert r.has_replacement is False


def test_update_diff_no_replacement():
    r = ResourceInfo(
        logical_name="t",
        type="aws:dynamodb/table:Table",
        operation=OpType.UPDATE,
        status="completed",
        start_time=1000,
        detailed_diff={"memorySize": _pdiff(DiffKind.UPDATE)},
    )
    assert r.has_replacement is False


def test_update_replace_triggers_replacement():
    r = ResourceInfo(
        logical_name="t",
        type="aws:dynamodb/table:Table",
        operation=OpType.UPDATE,
        status="completed",
        start_time=1000,
        detailed_diff={"name": _pdiff(DiffKind.UPDATE_REPLACE)},
    )
    assert r.has_replacement is True


def test_add_replace_triggers_replacement():
    r = ResourceInfo(
        logical_name="t",
        type="aws:dynamodb/table:Table",
        operation=OpType.UPDATE,
        status="completed",
        start_time=1000,
        detailed_diff={"newProp": _pdiff(DiffKind.ADD_REPLACE)},
    )
    assert r.has_replacement is True


def test_delete_replace_triggers_replacement():
    r = ResourceInfo(
        logical_name="t",
        type="aws:dynamodb/table:Table",
        operation=OpType.UPDATE,
        status="completed",
        start_time=1000,
        detailed_diff={"oldProp": _pdiff(DiffKind.DELETE_REPLACE)},
    )
    assert r.has_replacement is True


def test_replace_operation_triggers_replacement_without_detailed_diff():
    r = ResourceInfo(
        logical_name="t",
        type="aws:dynamodb/table:Table",
        operation=OpType.REPLACE,
        status="completed",
        start_time=1000,
    )
    assert r.has_replacement is True


# --- format_property_diff_lines ---


def test_property_diff_add():
    r = ResourceInfo(
        logical_name="fn",
        type="aws:lambda/function:Function",
        operation=OpType.CREATE,
        status="completed",
        start_time=1000,
        detailed_diff={"handler": _pdiff(DiffKind.ADD)},
        new_inputs={"handler": "src/handler.handler"},
    )
    lines = format_property_diff_lines(r)
    assert len(lines) == 1
    plain = lines[0].plain
    assert "+ handler" in plain
    assert "src/handler.handler" in plain


def test_property_diff_update_shows_old_and_new():
    r = ResourceInfo(
        logical_name="fn",
        type="aws:lambda/function:Function",
        operation=OpType.UPDATE,
        status="completed",
        start_time=1000,
        detailed_diff={"memorySize": _pdiff(DiffKind.UPDATE)},
        old_inputs={"memorySize": 128},
        new_inputs={"memorySize": 256},
    )
    lines = format_property_diff_lines(r)
    assert len(lines) == 1
    plain = lines[0].plain
    assert "* memorySize" in plain
    assert "128" in plain
    assert "256" in plain
    assert "->" in plain


def test_property_diff_delete():
    r = ResourceInfo(
        logical_name="fn",
        type="aws:lambda/function:Function",
        operation=OpType.UPDATE,
        status="completed",
        start_time=1000,
        detailed_diff={"environment": _pdiff(DiffKind.DELETE)},
    )
    lines = format_property_diff_lines(r)
    assert len(lines) == 1
    assert "- environment" in lines[0].plain


def test_property_diff_replace_annotation():
    r = ResourceInfo(
        logical_name="t",
        type="aws:dynamodb/table:Table",
        operation=OpType.UPDATE,
        status="completed",
        start_time=1000,
        detailed_diff={"name": _pdiff(DiffKind.UPDATE_REPLACE)},
        old_inputs={"name": "users-v1"},
        new_inputs={"name": "users-v2"},
    )
    lines = format_property_diff_lines(r)
    assert "(forces replacement)" in lines[0].plain


def test_property_diff_empty_when_no_diff():
    r = ResourceInfo(
        logical_name="fn",
        type="aws:lambda/function:Function",
        operation=OpType.SAME,
        status="completed",
        start_time=1000,
    )
    assert format_property_diff_lines(r) == []


def test_property_diff_multiple_sorted():
    r = ResourceInfo(
        logical_name="fn",
        type="aws:lambda/function:Function",
        operation=OpType.CREATE,
        status="completed",
        start_time=1000,
        detailed_diff={
            "runtime": _pdiff(DiffKind.ADD),
            "handler": _pdiff(DiffKind.ADD),
            "memorySize": _pdiff(DiffKind.ADD),
        },
        new_inputs={"handler": "h", "memorySize": 256, "runtime": "python3.12"},
    )
    lines = format_property_diff_lines(r)
    assert len(lines) == 3
    props = [line.plain.strip().split()[1] for line in lines]
    assert props == ["handler", "memorySize", "runtime"]


def test_property_diff_indentation():
    r = ResourceInfo(
        logical_name="fn",
        type="aws:lambda/function:Function",
        operation=OpType.CREATE,
        status="completed",
        start_time=1000,
        detailed_diff={"handler": _pdiff(DiffKind.ADD)},
        new_inputs={"handler": "h"},
    )
    lines_1 = format_property_diff_lines(r, indent=1)
    assert lines_1[0].plain.startswith("        ")  # 8 spaces
    lines_2 = format_property_diff_lines(r, indent=2)
    assert lines_2[0].plain.startswith("            ")  # 12 spaces


def test_property_diff_update_collapses_multiline_values():
    r = ResourceInfo(
        logical_name="policy",
        type="aws:iam/policy:Policy",
        operation=OpType.UPDATE,
        status="completed",
        start_time=1000,
        detailed_diff={"policy": _pdiff(DiffKind.UPDATE)},
        old_inputs={"policy": '{\n  "Version": "2012-10-17",\n  "Statement": []\n}'},
        new_inputs={"policy": '{\n  "Version": "2012-10-17",\n  "Statement": ["x"]\n}'},
    )
    lines = format_property_diff_lines(r)
    assert "JSON changed (1 paths)" in lines[0].plain
    assert "Statement[0]" not in lines[0].plain
    assert "Statement[0]" in lines[1].plain
    assert "old:" in lines[2].plain
    assert "new:" in lines[3].plain


def test_property_diff_update_json_path_missing_side_is_explicit():
    r = ResourceInfo(
        logical_name="policy",
        type="aws:iam/policy:Policy",
        operation=OpType.UPDATE,
        status="completed",
        start_time=1000,
        detailed_diff={"policy": _pdiff(DiffKind.UPDATE)},
        old_inputs={"policy": '{"Statement":[{"Resource":"arn:aws:sqs:us-east-1:123:q"}]}'},
        new_inputs={"policy": '{"Statement":[{}]}'},
    )

    lines = format_property_diff_lines(r)
    joined = "\n".join(line.plain for line in lines)
    assert "JSON changed (1 paths)" in joined
    assert "Statement[0].Resource" in joined
    assert "old:" in joined
    assert "old: arn:aws:" in joined
    assert "new: <missing>" in joined


def test_property_diff_update_json_shows_all_changed_paths():
    r = ResourceInfo(
        logical_name="policy",
        type="aws:iam/policy:Policy",
        operation=OpType.UPDATE,
        status="completed",
        start_time=1000,
        detailed_diff={"policy": _pdiff(DiffKind.UPDATE)},
        old_inputs={
            "policy": (
                '{"Statement":[{"Action":"sqs:SendMessage","Resource":"arn:aws:sqs:us-east-1:123:q"}]}'
            )
        },
        new_inputs={
            "policy": (
                '{"Statement":[{"Action":"sqs:ReceiveMessage","Resource":"arn:aws:sqs:us-east-1:123:q2"}]}'
            )
        },
    )

    lines = format_property_diff_lines(r)
    joined = "\n".join(line.plain for line in lines)
    assert "JSON changed (2 paths)" in joined
    assert "Statement[0].Action" in joined
    assert "Statement[0].Resource" in joined


def test_property_diff_update_dict_values_show_key_level_details():
    r = ResourceInfo(
        logical_name="fn",
        type="aws:lambda/function:Function",
        operation=OpType.UPDATE,
        status="completed",
        start_time=1000,
        detailed_diff={"environment.variables": _pdiff(DiffKind.UPDATE)},
        old_inputs={"environment": {"variables": {"BIG": "a" * 150}}},
        new_inputs={"environment": {"variables": {"BIG": "b" * 150}}},
    )
    lines = format_property_diff_lines(r)
    assert "keys: 1 changed" in lines[0].plain
    assert "~ BIG" in lines[1].plain
    assert "old:" in lines[2].plain
    assert "new:" in lines[3].plain


def test_property_diff_update_dict_values_show_all_changed_keys():
    r = ResourceInfo(
        logical_name="fn",
        type="aws:lambda/function:Function",
        operation=OpType.UPDATE,
        status="completed",
        start_time=1000,
        detailed_diff={"environment.variables": _pdiff(DiffKind.UPDATE)},
        old_inputs={"environment": {"variables": {"A": "1", "B": "2"}}},
        new_inputs={"environment": {"variables": {"A": "x", "B": "y"}}},
    )

    lines = format_property_diff_lines(r)
    joined = "\n".join(line.plain for line in lines)
    assert "keys: 2 changed" in lines[0].plain
    assert "~ A" in joined
    assert "~ B" in joined


def test_property_diff_update_dict_added_key_rendered_once():
    r = ResourceInfo(
        logical_name="fn",
        type="aws:lambda/function:Function",
        operation=OpType.UPDATE,
        status="completed",
        start_time=1000,
        detailed_diff={"environment.variables": _pdiff(DiffKind.UPDATE)},
        old_inputs={"environment": {"variables": {}}},
        new_inputs={"environment": {"variables": {"A": "1"}}},
    )

    lines = format_property_diff_lines(r)
    joined = "\n".join(line.plain for line in lines)
    assert joined.count("+ A = 1") == 1


def test_property_diff_update_dict_marks_fingerprint_as_computed():
    r = ResourceInfo(
        logical_name="fn",
        type="aws:lambda/function:Function",
        operation=OpType.UPDATE,
        status="completed",
        start_time=1000,
        detailed_diff={"environment.variables": _pdiff(DiffKind.UPDATE)},
        old_inputs={
            "environment": {
                "variables": {"STLV_USERS_TABLE_ARN": "arn:aws:dynamodb:...:table/users"}
            }
        },
        new_inputs={
            "environment": {
                "variables": {"STLV_USERS_TABLE_ARN": "04da6b54d4b94a67a7ac9f84ff0331ba9"}
            }
        },
    )

    lines = format_property_diff_lines(r)
    joined = "\n".join(line.plain for line in lines)
    assert "STLV_USERS_TABLE_ARN" in joined
    assert "old: arn:aws:dynamodb" in joined
    assert "new:" in joined
    assert "output<string>" in joined
    assert "<computed>" not in joined


def test_property_diff_update_dict_marks_uuid_fingerprint_as_computed():
    r = ResourceInfo(
        logical_name="fn",
        type="aws:lambda/function:Function",
        operation=OpType.UPDATE,
        status="completed",
        start_time=1000,
        detailed_diff={"environment.variables": _pdiff(DiffKind.UPDATE)},
        old_inputs={
            "environment": {
                "variables": {"STLV_USERS_TABLE_ARN": "arn:aws:dynamodb:...:table/users"}
            }
        },
        new_inputs={
            "environment": {
                "variables": {"STLV_USERS_TABLE_ARN": "04da6b54-80e4-46f7-96ec-b56ff0331ba9"}
            }
        },
    )

    lines = format_property_diff_lines(r)
    joined = "\n".join(line.plain for line in lines)
    assert "STLV_USERS_TABLE_ARN" in joined
    assert "old: arn:aws:dynamodb" in joined
    assert "output<string>" in joined
    assert "<computed>" not in joined


def test_property_diff_computed_marker_preserved_when_width_is_small():
    r = ResourceInfo(
        logical_name="fn",
        type="aws:lambda/function:Function",
        operation=OpType.UPDATE,
        status="completed",
        start_time=1000,
        detailed_diff={"environment.variables": _pdiff(DiffKind.UPDATE)},
        old_inputs={
            "environment": {
                "variables": {"STLV_USERS_TABLE_ARN": "arn:aws:dynamodb:...:table/users"}
            }
        },
        new_inputs={
            "environment": {
                "variables": {"STLV_USERS_TABLE_ARN": "04da6b54-80e4-46f7-96ec-b56ff0331ba9"}
            }
        },
    )

    lines = format_property_diff_lines(r, line_width=60)
    joined = "\n".join(line.plain for line in lines)
    assert "output<string>" in joined


# --- format_replacement_warning ---


def test_replacement_warning_text():
    line = format_replacement_warning(indent=1)
    assert "Replacement recreates resource" in line.plain
    assert "data may be lost" in line.plain


def test_replacement_warning_indentation():
    assert format_replacement_warning(indent=0).plain.startswith("    ")
    assert format_replacement_warning(indent=2).plain.startswith("    " * 3)


# --- ComponentInfo.has_replacement ---


def test_component_no_replacement():
    r = ResourceInfo(
        logical_name="fn",
        type="aws:lambda/function:Function",
        operation=OpType.UPDATE,
        status="completed",
        start_time=1000,
        detailed_diff={"memorySize": _pdiff(DiffKind.UPDATE)},
    )
    comp = ComponentInfo(
        component_type="Function",
        name="api",
        urn=_component_urn("Function", "api"),
        children=[r],
    )
    assert comp.has_replacement is False


def test_component_with_replacement():
    r = ResourceInfo(
        logical_name="t",
        type="aws:dynamodb/table:Table",
        operation=OpType.UPDATE,
        status="completed",
        start_time=1000,
        detailed_diff={"name": _pdiff(DiffKind.UPDATE_REPLACE)},
    )
    comp = ComponentInfo(
        component_type="DynamoTable",
        name="users",
        urn=_component_urn("DynamoTable", "users"),
        children=[r],
    )
    assert comp.has_replacement is True


def test_component_with_nested_replacement():
    replacement_resource = ResourceInfo(
        logical_name="users-table",
        type="aws:dynamodb/table:Table",
        operation=OpType.UPDATE,
        status="completed",
        start_time=1000,
        detailed_diff={"hashKey": _pdiff(DiffKind.UPDATE_REPLACE)},
    )
    child_comp = ComponentInfo(
        component_type="DynamoTable",
        name="users",
        urn=_component_urn("DynamoTable", "users"),
        children=[replacement_resource],
    )
    parent_comp = ComponentInfo(
        component_type="TopicSubscription",
        name="users-events",
        urn=_component_urn("TopicSubscription", "users-events"),
        children=[child_comp],
    )

    assert parent_comp.has_replacement is True


def test_component_data_loss_replacement_tracks_only_data_resources():
    data_resource = ResourceInfo(
        logical_name="t",
        type="aws:dynamodb/table:Table",
        operation=OpType.REPLACE,
        status="completed",
        start_time=1000,
    )
    stateless_resource = ResourceInfo(
        logical_name="f",
        type="aws:lambda/function:Function",
        operation=OpType.REPLACE,
        status="completed",
        start_time=1000,
    )

    data_comp = ComponentInfo(
        component_type="DynamoTable",
        name="users",
        urn=_component_urn("DynamoTable", "users"),
        children=[data_resource],
    )
    stateless_comp = ComponentInfo(
        component_type="Function",
        name="api",
        urn=_component_urn("Function", "api"),
        children=[stateless_resource],
    )

    assert data_comp.has_data_loss_replacement is True
    assert stateless_comp.has_data_loss_replacement is False


def test_component_data_loss_replacement_propagates_from_nested_component():
    data_resource = ResourceInfo(
        logical_name="users-table",
        type="aws:dynamodb/table:Table",
        operation=OpType.REPLACE,
        status="completed",
        start_time=1000,
    )
    child_comp = ComponentInfo(
        component_type="DynamoTable",
        name="users",
        urn=_component_urn("DynamoTable", "users"),
        children=[data_resource],
    )
    parent_comp = ComponentInfo(
        component_type="TopicSubscription",
        name="users-events",
        urn=_component_urn("TopicSubscription", "users-events"),
        children=[child_comp],
    )

    assert parent_comp.has_data_loss_replacement is True


# --- ComponentInfo.preview_summary ---


def test_preview_summary_create_only():
    children = [
        ResourceInfo(
            logical_name=f"r{i}",
            type="aws:lambda/function:Function",
            operation=OpType.CREATE,
            status="completed",
            start_time=1000,
        )
        for i in range(4)
    ]
    comp = ComponentInfo(
        component_type="Function",
        name="api",
        urn=_component_urn("Function", "api"),
        children=children,
    )
    assert comp.preview_summary() == "4 to create"


def test_preview_summary_mixed():
    children = [
        ResourceInfo(
            logical_name="r1",
            type="aws:lambda/function:Function",
            operation=OpType.CREATE,
            status="completed",
            start_time=1000,
        ),
        ResourceInfo(
            logical_name="r2",
            type="aws:iam/role:Role",
            operation=OpType.UPDATE,
            status="completed",
            start_time=1000,
        ),
    ]
    comp = ComponentInfo(
        component_type="Function",
        name="api",
        urn=_component_urn("Function", "api"),
        children=children,
    )
    summary = comp.preview_summary()
    assert "1 to create" in summary
    assert "1 to update" in summary


def test_preview_summary_replacement_counted():
    children = [
        ResourceInfo(
            logical_name="t",
            type="aws:dynamodb/table:Table",
            operation=OpType.UPDATE,
            status="completed",
            start_time=1000,
            detailed_diff={"name": _pdiff(DiffKind.UPDATE_REPLACE)},
        ),
    ]
    comp = ComponentInfo(
        component_type="DynamoTable",
        name="users",
        urn=_component_urn("DynamoTable", "users"),
        children=children,
    )
    assert comp.preview_summary() == "1 to replace"


def test_preview_summary_with_resource_words():
    children = [
        ResourceInfo(
            logical_name="r1",
            type="aws:lambda/function:Function",
            operation=OpType.CREATE,
            status="completed",
            start_time=1000,
        ),
        ResourceInfo(
            logical_name="r2",
            type="aws:iam/role:Role",
            operation=OpType.CREATE,
            status="completed",
            start_time=1000,
        ),
    ]
    comp = ComponentInfo(
        component_type="Function",
        name="api",
        urn=_component_urn("Function", "api"),
        children=children,
    )
    assert comp.preview_summary(include_resource_word=True) == "2 resources to create"


def test_preview_summary_with_resource_words_singular():
    children = [
        ResourceInfo(
            logical_name="r1",
            type="aws:lambda/function:Function",
            operation=OpType.CREATE,
            status="completed",
            start_time=1000,
        ),
    ]
    comp = ComponentInfo(
        component_type="Function",
        name="api",
        urn=_component_urn("Function", "api"),
        children=children,
    )
    assert comp.preview_summary(include_resource_word=True) == "1 resource to create"


def test_preview_summary_same_excluded():
    children = [
        ResourceInfo(
            logical_name="r1",
            type="aws:lambda/function:Function",
            operation=OpType.SAME,
            status="completed",
            start_time=1000,
        ),
    ]
    comp = ComponentInfo(
        component_type="Function",
        name="api",
        urn=_component_urn("Function", "api"),
        children=children,
    )
    assert comp.preview_summary() == ""


# --- build_preview_counts_text ---


def test_preview_counts_create_and_delete():
    resources = {
        "a": ResourceInfo(
            logical_name="a",
            type="aws:lambda/function:Function",
            operation=OpType.CREATE,
            status="completed",
            start_time=1000,
        ),
        "b": ResourceInfo(
            logical_name="b",
            type="aws:s3/bucketV2:BucketV2",
            operation=OpType.DELETE,
            status="completed",
            start_time=1000,
        ),
    }
    text = build_preview_counts_text(resources)
    assert text is not None
    assert "1 to create" in text.plain
    assert "1 to delete" in text.plain


def test_preview_counts_same_excluded():
    resources = {
        "a": ResourceInfo(
            logical_name="a",
            type="aws:lambda/function:Function",
            operation=OpType.SAME,
            status="completed",
            start_time=1000,
        ),
    }
    assert build_preview_counts_text(resources) is None


def test_preview_counts_replacement():
    resources = {
        "a": ResourceInfo(
            logical_name="a",
            type="aws:dynamodb/table:Table",
            operation=OpType.UPDATE,
            status="completed",
            start_time=1000,
            detailed_diff={"name": _pdiff(DiffKind.UPDATE_REPLACE)},
        ),
    }
    text = build_preview_counts_text(resources)
    assert "1 to replace" in text.plain


# --- format_component_header with preview ---


def test_preview_header_shows_summary():
    children = [
        ResourceInfo(
            logical_name=f"r{i}",
            type="aws:lambda/function:Function",
            operation=OpType.CREATE,
            status="completed",
            start_time=1000,
        )
        for i in range(4)
    ]
    comp = ComponentInfo(
        component_type="Function",
        name="api",
        urn=_component_urn("Function", "api"),
        children=children,
    )
    text = format_component_header(comp, is_preview=True)
    assert "(4 to create)" in text.plain


def test_live_header_shows_duration_not_summary():
    children = [
        ResourceInfo(
            logical_name="r",
            type="aws:lambda/function:Function",
            operation=OpType.CREATE,
            status="completed",
            start_time=1000,
        ),
    ]
    comp = ComponentInfo(
        component_type="Function",
        name="api",
        urn=_component_urn("Function", "api"),
        children=children,
    )
    text = format_component_header(comp, is_preview=False, duration_str="(2.1s)")
    assert "(2.1s)" in text.plain
    assert "to create" not in text.plain


# --- Handler: detailed_diff captured from events ---


def test_detailed_diff_stored_on_resource(preview_handler):
    parent_urn = _component_urn("Function", "api")
    res_urn = _resource_urn("aws:lambda/function:Function", "api-fn", "Function")

    preview_handler.handle_event(
        _pre_event(
            res_urn,
            "aws:lambda/function:Function",
            op=OpType.UPDATE,
            parent_urn=parent_urn,
            detailed_diff={"memorySize": _pdiff(DiffKind.UPDATE)},
            old_inputs={"memorySize": 128},
            new_inputs={"memorySize": 256},
        )
    )

    resource = preview_handler.resources[res_urn]
    assert resource.detailed_diff is not None
    assert "memorySize" in resource.detailed_diff
    assert resource.old_inputs == {"memorySize": 128}
    assert resource.new_inputs == {"memorySize": 256}


def test_property_diffs_shown_in_preview_render(preview_handler):
    parent_urn = _component_urn("Function", "api")
    res_urn = _resource_urn("aws:lambda/function:Function", "api-fn", "Function")

    preview_handler.handle_event(
        _pre_event(
            res_urn,
            "aws:lambda/function:Function",
            op=OpType.UPDATE,
            parent_urn=parent_urn,
            detailed_diff={"memorySize": _pdiff(DiffKind.UPDATE)},
            old_inputs={"memorySize": 128},
            new_inputs={"memorySize": 256},
        )
    )

    content = _render_content_text(preview_handler)
    assert "memorySize" in content
    assert "128" in content
    assert "256" in content


def test_replacement_warning_shown_in_render(preview_handler):
    parent_urn = _component_urn("DynamoTable", "users")
    res_urn = _resource_urn("aws:dynamodb/table:Table", "users-table", "DynamoTable")

    preview_handler.handle_event(
        _pre_event(
            res_urn,
            "aws:dynamodb/table:Table",
            op=OpType.UPDATE,
            parent_urn=parent_urn,
            detailed_diff={"name": _pdiff(DiffKind.UPDATE_REPLACE)},
            old_inputs={"name": "users-v1"},
            new_inputs={"name": "users-v2"},
        )
    )

    content = _render_content_text(preview_handler)
    assert "(forces replacement)" in content
    assert "Replacement recreates resource" in content


def test_replacement_warning_shown_for_replace_operation_without_detailed_diff(preview_handler):
    parent_urn = _component_urn("DynamoTable", "users")
    res_urn = _resource_urn("aws:dynamodb/table:Table", "users-table", "DynamoTable")

    preview_handler.handle_event(
        _pre_event(
            res_urn,
            "aws:dynamodb/table:Table",
            op=OpType.REPLACE,
            parent_urn=parent_urn,
        )
    )

    content = _render_content_text(preview_handler)
    assert "Replacement recreates resource" in content


def test_no_data_loss_warning_for_non_data_resource_replacement(preview_handler):
    parent_urn = _component_urn("Function", "api")
    res_urn = _resource_urn("aws:lambda/function:Function", "api-fn", "Function")

    preview_handler.handle_event(
        _pre_event(
            res_urn,
            "aws:lambda/function:Function",
            op=OpType.REPLACE,
            parent_urn=parent_urn,
        )
    )

    content = _render_content_text(preview_handler)
    assert "to replace" in content
    assert "Replacement recreates resource" not in content


def test_preview_render_keeps_children_visible_after_completion(preview_handler):
    parent_urn = _component_urn("Function", "api")
    res_urn = _resource_urn("aws:lambda/function:Function", "api-fn", "Function")

    preview_handler.handle_event(
        _pre_event(
            res_urn,
            "aws:lambda/function:Function",
            op=OpType.UPDATE,
            parent_urn=parent_urn,
            detailed_diff={"memorySize": _pdiff(DiffKind.UPDATE)},
            old_inputs={"memorySize": 128},
            new_inputs={"memorySize": 256},
        )
    )
    preview_handler.handle_event(
        _outputs_event(
            res_urn,
            "aws:lambda/function:Function",
            op=OpType.UPDATE,
            parent_urn=parent_urn,
        )
    )

    content = _render_content_text(preview_handler)
    assert "Lambda Function" in content
    assert "memorySize" in content
    assert "128 -> 256" in content


def test_preview_render_hides_empty_component_placeholders(preview_handler):
    queue_urn = _component_urn("Queue", "tasks")
    preview_handler.handle_event(_pre_event(queue_urn, "stelvio:aws:Queue", parent_urn=STACK_URN))

    content = _render_content_text(preview_handler)
    assert content == ""
    assert "Queue  tasks" not in content


def test_no_property_diffs_in_deploy_render(handler):
    """Deploy (non-preview) should NOT show property diffs."""
    parent_urn = _component_urn("Function", "api")
    res_urn = _resource_urn("aws:lambda/function:Function", "api-fn", "Function")

    handler.handle_event(
        _pre_event(
            res_urn,
            "aws:lambda/function:Function",
            op=OpType.UPDATE,
            parent_urn=parent_urn,
            detailed_diff={"memorySize": _pdiff(DiffKind.UPDATE)},
            old_inputs={"memorySize": 128},
            new_inputs={"memorySize": 256},
        )
    )

    content = _render_content_text(handler)
    assert "memorySize" not in content
    assert "(forces replacement)" not in content


# --- Compact mode ---


def test_compact_preview_header_only(compact_preview_handler):
    parent_urn = _component_urn("Function", "api")
    res_urn = _resource_urn("aws:lambda/function:Function", "api-fn", "Function")

    compact_preview_handler.handle_event(
        _pre_event(
            res_urn,
            "aws:lambda/function:Function",
            op=OpType.CREATE,
            parent_urn=parent_urn,
            detailed_diff={"handler": _pdiff(DiffKind.ADD)},
            new_inputs={"handler": "src/handler.handler"},
        )
    )

    content = _render_content_text(compact_preview_handler)
    assert "Function" in content
    assert "api" in content
    # No child resources or property diffs
    assert "Lambda Function" not in content
    assert "handler" not in content


def test_compact_shows_preview_summary(compact_preview_handler):
    parent_urn = _component_urn("Function", "api")
    for i, rtype in enumerate(["aws:iam/role:Role", "aws:lambda/function:Function"]):
        res_urn = _resource_urn(rtype, f"r{i}", "Function")
        compact_preview_handler.handle_event(
            _pre_event(
                res_urn,
                rtype,
                op=OpType.CREATE,
                parent_urn=parent_urn,
            )
        )

    content = _render_content_text(compact_preview_handler)
    assert "(2 resources to create)" in content


def test_compact_shows_replacement_warning(compact_preview_handler):
    parent_urn = _component_urn("DynamoTable", "users")
    res_urn = _resource_urn("aws:dynamodb/table:Table", "users-table", "DynamoTable")

    compact_preview_handler.handle_event(
        _pre_event(
            res_urn,
            "aws:dynamodb/table:Table",
            op=OpType.UPDATE,
            parent_urn=parent_urn,
            detailed_diff={"hashKey": _pdiff(DiffKind.UPDATE_REPLACE)},
            old_inputs={"hashKey": "pk"},
            new_inputs={"hashKey": "user_id"},
        )
    )

    content = _render_content_text(compact_preview_handler)
    assert "DynamoTable  users" in content
    assert "Replacement recreates resource" in content
    assert "DynamoDB Table" not in content


def test_compact_hides_data_loss_warning_for_non_data_replacement(compact_preview_handler):
    parent_urn = _component_urn("Function", "api")
    res_urn = _resource_urn("aws:lambda/function:Function", "api-fn", "Function")

    compact_preview_handler.handle_event(
        _pre_event(
            res_urn,
            "aws:lambda/function:Function",
            op=OpType.REPLACE,
            parent_urn=parent_urn,
        )
    )

    content = _render_content_text(compact_preview_handler)
    assert "to replace" in content
    assert "Replacement recreates resource" not in content


def test_compact_summary_shows_replacement_warning(compact_preview_handler):
    compact_preview_handler.console = Console(record=True, width=160)
    parent_urn = _component_urn("DynamoTable", "users")
    res_urn = _resource_urn("aws:dynamodb/table:Table", "users-table", "DynamoTable")

    compact_preview_handler.handle_event(
        _pre_event(
            res_urn,
            "aws:dynamodb/table:Table",
            op=OpType.UPDATE,
            parent_urn=parent_urn,
            detailed_diff={"hashKey": _pdiff(DiffKind.UPDATE_REPLACE)},
            old_inputs={"hashKey": "pk"},
            new_inputs={"hashKey": "user_id"},
        )
    )

    compact_preview_handler._print_resources_summary()
    output = compact_preview_handler.console.export_text()
    assert "Replacement recreates resource" in output


class _FakeOutput:
    def __init__(self, value: object, secret: bool = False) -> None:
        self.value = value
        self.secret = secret


def test_preview_completion_hides_outputs(preview_handler):
    preview_handler.console = Console(record=True, width=120)
    preview_handler.show_completion({"api_url": _FakeOutput("https://example.com")})

    output = preview_handler.console.export_text()
    assert "Outputs:" not in output
    assert "Analyzed in" in output


def test_deploy_completion_shows_outputs(handler):
    handler.console = Console(record=True, width=120)
    handler.show_completion({"api_url": _FakeOutput("https://example.com")})

    output = handler.console.export_text()
    assert "Outputs:" in output
    assert "api_url" in output
    assert "https://example.com" in output


def test_deploy_completion_prefers_preformatted_output_lines(handler):
    handler.console = Console(record=True, width=120)
    handler.show_completion(output_lines=["", "[bold]Outputs:", "  custom line"])

    output = handler.console.export_text()
    assert "custom line" in output
    assert "api_url" not in output


def test_build_json_summary_for_deploy(handler):
    comp_urn = _component_urn("Function", "api")
    res_urn = _resource_urn("aws:lambda/function:Function", "myapp-dev-api", "Function")

    handler.handle_event(
        _pre_event(
            comp_urn,
            "stelvio:aws:Function",
            parent_urn=STACK_URN,
        )
    )
    handler.handle_event(
        _pre_event(
            res_urn,
            "aws:lambda/function:Function",
            parent_urn=comp_urn,
            new_inputs={"memory_size": 256},
        )
    )
    handler.handle_event(
        _outputs_event(
            res_urn,
            "aws:lambda/function:Function",
            parent_urn=comp_urn,
        )
    )

    payload = handler.build_json_summary(
        outputs={"function_api_arn": "arn:aws:lambda:demo"},
        exit_code=0,
    )

    assert payload["operation"] == "deploy"
    assert payload["status"] == "success"
    assert payload["exit_code"] == 0
    assert payload["summary"] == {
        "created": 1,
        "updated": 0,
        "deleted": 0,
        "replaced": 0,
        "failed": 0,
        "unchanged": 0,
    }
    assert payload["outputs"] == {"function_api_arn": "arn:aws:lambda:demo"}
    assert payload["components"] == [
        {
            "type": "Function",
            "name": "api",
            "operation": "create",
            "resources": [
                {
                    "name": "myapp-dev-api",
                    "type": "aws:lambda/function:Function",
                    "operation": "create",
                }
            ],
        }
    ]


def test_stream_emits_component_resource_warning_and_completion_events(monkeypatch):
    from rich.live import Live

    monkeypatch.setattr(Live, "start", lambda self: None)
    monkeypatch.setattr(Live, "stop", lambda self: None)

    events: list[dict] = []
    stream_handler = RichDeploymentHandler(
        "myapp",
        "dev",
        "deploy",
        live_enabled=False,
        stream_writer=events.append,
    )
    comp_urn = _component_urn("Function", "api")
    res_urn = _resource_urn("aws:lambda/function:Function", "myapp-dev-api", "Function")

    stream_handler.handle_event(
        _pre_event(comp_urn, "stelvio:aws:Function", parent_urn=STACK_URN, timestamp=1000)
    )
    stream_handler.handle_event(
        _pre_event(
            res_urn,
            "aws:lambda/function:Function",
            parent_urn=comp_urn,
            timestamp=1001,
        )
    )
    stream_handler.handle_event(
        _outputs_event(
            res_urn,
            "aws:lambda/function:Function",
            parent_urn=comp_urn,
            timestamp=1002,
        )
    )
    stream_handler.handle_event(
        _outputs_event(
            comp_urn,
            "stelvio:aws:Function",
            parent_urn=STACK_URN,
            timestamp=1002,
        )
    )
    stream_handler.handle_event(
        EngineEvent(
            sequence=_next_seq(),
            timestamp=1003,
            diagnostic_event=DiagnosticEvent(
                message="Node.js 18.x runtime is deprecated",
                color="yellow",
                severity="warning",
                urn=res_urn,
            ),
        )
    )

    event_types = [event["event"] for event in events]
    assert event_types == [
        "resource",
        "warning",
    ]
    assert events[0]["resource"]["type"] == "aws:lambda/function:Function"
    assert events[0]["component"] == {"type": "Function", "name": "api"}
    assert events[1]["message"] == "Node.js 18.x runtime is deprecated"


def test_stream_does_not_emit_component_lifecycle_for_unchanged_component(monkeypatch):
    from rich.live import Live

    monkeypatch.setattr(Live, "start", lambda self: None)
    monkeypatch.setattr(Live, "stop", lambda self: None)

    events: list[dict] = []
    stream_handler = RichDeploymentHandler(
        "myapp",
        "dev",
        "deploy",
        live_enabled=False,
        stream_writer=events.append,
    )
    comp_urn = _component_urn("Queue", "tasks")
    res_urn = _resource_urn("aws:sqs/queue:Queue", "myapp-dev-tasks", "Queue")

    stream_handler.handle_event(
        _pre_event(comp_urn, "stelvio:aws:Queue", parent_urn=STACK_URN, timestamp=1000)
    )
    stream_handler.handle_event(
        _pre_event(
            res_urn,
            "aws:sqs/queue:Queue",
            op=OpType.SAME,
            parent_urn=comp_urn,
            timestamp=1001,
        )
    )
    stream_handler.handle_event(
        _outputs_event(
            res_urn,
            "aws:sqs/queue:Queue",
            op=OpType.SAME,
            parent_urn=comp_urn,
            timestamp=1002,
        )
    )

    assert events == []


def test_build_json_summary_for_diff_includes_changes(preview_handler):
    comp_urn = _component_urn("Function", "api")
    res_urn = _resource_urn("aws:lambda/function:Function", "myapp-dev-api", "Function")

    preview_handler.handle_event(
        _pre_event(
            comp_urn,
            "stelvio:aws:Function",
            op=OpType.UPDATE,
            parent_urn=STACK_URN,
        )
    )
    preview_handler.handle_event(
        _pre_event(
            res_urn,
            "aws:lambda/function:Function",
            op=OpType.UPDATE,
            parent_urn=comp_urn,
            detailed_diff={
                "memory_size": _pdiff(DiffKind.UPDATE),
                "runtime": _pdiff(DiffKind.UPDATE_REPLACE),
            },
            old_inputs={"memory_size": 128, "runtime": "python3.11"},
            new_inputs={"memory_size": 256, "runtime": "python3.12"},
        )
    )
    preview_handler.handle_event(
        _outputs_event(
            res_urn,
            "aws:lambda/function:Function",
            op=OpType.UPDATE,
            parent_urn=comp_urn,
            detailed_diff={
                "memory_size": _pdiff(DiffKind.UPDATE),
                "runtime": _pdiff(DiffKind.UPDATE_REPLACE),
            },
            old_inputs={"memory_size": 128, "runtime": "python3.11"},
            new_inputs={"memory_size": 256, "runtime": "python3.12"},
        )
    )

    payload = preview_handler.build_json_summary(exit_code=0)

    assert payload["operation"] == "diff"
    assert payload["summary"] == {
        "to_create": 0,
        "to_update": 0,
        "to_delete": 0,
        "to_replace": 1,
    }
    assert payload["components"] == [
        {
            "type": "Function",
            "name": "api",
            "operation": "replace",
            "resources": [
                {
                    "name": "myapp-dev-api",
                    "type": "aws:lambda/function:Function",
                    "operation": "replace",
                    "changes": [
                        {
                            "path": "memory_size",
                            "kind": "update",
                            "old": 128,
                            "new": 256,
                        },
                        {
                            "path": "runtime",
                            "kind": "update_replace",
                            "old": "python3.11",
                            "new": "python3.12",
                            "forces_replacement": True,
                        },
                    ],
                }
            ],
        }
    ]


def test_build_json_summary_for_diff_resolves_indexed_change_values(preview_handler):
    comp_urn = _component_urn("Function", "api")
    res_urn = _resource_urn("aws:iam/policy:Policy", "myapp-dev-api-p", "Function")

    preview_handler.handle_event(
        _pre_event(
            comp_urn,
            "stelvio:aws:Function",
            op=OpType.UPDATE,
            parent_urn=STACK_URN,
        )
    )
    preview_handler.handle_event(
        _pre_event(
            res_urn,
            "aws:iam/policy:Policy",
            op=OpType.UPDATE,
            parent_urn=comp_urn,
            detailed_diff={
                "policy.Statement[0].Resource": _pdiff(DiffKind.UPDATE),
            },
            old_inputs={"policy": {"Statement": [{"Resource": "arn:old"}]}},
            new_inputs={"policy": {"Statement": [{"Resource": "arn:new"}]}},
        )
    )
    preview_handler.handle_event(
        _outputs_event(
            res_urn,
            "aws:iam/policy:Policy",
            op=OpType.UPDATE,
            parent_urn=comp_urn,
            detailed_diff={
                "policy.Statement[0].Resource": _pdiff(DiffKind.UPDATE),
            },
            old_inputs={"policy": {"Statement": [{"Resource": "arn:old"}]}},
            new_inputs={"policy": {"Statement": [{"Resource": "arn:new"}]}},
        )
    )

    payload = preview_handler.build_json_summary(exit_code=0)
    assert payload["components"] == [
        {
            "type": "Function",
            "name": "api",
            "operation": "update",
            "resources": [
                {
                    "name": "myapp-dev-api-p",
                    "type": "aws:iam/policy:Policy",
                    "operation": "update",
                    "changes": [
                        {
                            "path": "policy.Statement[0].Resource",
                            "kind": "update",
                            "old": "arn:old",
                            "new": "arn:new",
                        }
                    ],
                }
            ],
        }
    ]


def test_build_json_summary_for_refresh_uses_unchanged_for_no_drift(handler):
    handler.operation = "refresh"

    comp_urn = _component_urn("Function", "api")
    res_urn = _resource_urn("aws:lambda/function:Function", "myapp-dev-api", "Function")

    handler.handle_event(
        _pre_event(
            comp_urn,
            "stelvio:aws:Function",
            op=OpType.REFRESH,
            parent_urn=STACK_URN,
        )
    )
    handler.handle_event(
        _pre_event(
            res_urn,
            "aws:lambda/function:Function",
            op=OpType.REFRESH,
            parent_urn=comp_urn,
        )
    )
    handler.handle_event(
        _outputs_event(
            res_urn,
            "aws:lambda/function:Function",
            op=OpType.REFRESH,
            parent_urn=comp_urn,
        )
    )

    payload = handler.build_json_summary(exit_code=0)

    assert payload["operation"] == "refresh"
    assert payload["components"] == [
        {
            "type": "Function",
            "name": "api",
            "operation": "unchanged",
            "resources": [
                {
                    "name": "myapp-dev-api",
                    "type": "aws:lambda/function:Function",
                    "operation": "unchanged",
                }
            ],
        }
    ]


def test_build_json_summary_for_refresh_reports_drift_updates(handler):
    handler.operation = "refresh"

    comp_urn = _component_urn("Function", "api")
    res_urn = _resource_urn("aws:lambda/function:Function", "myapp-dev-api", "Function")

    handler.handle_event(
        _pre_event(
            comp_urn,
            "stelvio:aws:Function",
            op=OpType.REFRESH,
            parent_urn=STACK_URN,
        )
    )
    handler.handle_event(
        _pre_event(
            res_urn,
            "aws:lambda/function:Function",
            op=OpType.REFRESH,
            parent_urn=comp_urn,
        )
    )
    handler.handle_event(
        _outputs_event(
            res_urn,
            "aws:lambda/function:Function",
            op=OpType.UPDATE,
            parent_urn=comp_urn,
            diffs=["memorySize"],
        )
    )

    payload = handler.build_json_summary(exit_code=0)

    assert payload["components"] == [
        {
            "type": "Function",
            "name": "api",
            "operation": "update",
            "resources": [
                {
                    "name": "myapp-dev-api",
                    "type": "aws:lambda/function:Function",
                    "operation": "update",
                }
            ],
        }
    ]


def test_build_json_summary_for_diff_includes_delete_operations(preview_handler):
    comp_urn = _component_urn("Queue", "tasks")
    res_urn = _resource_urn("aws:sqs/queue:Queue", "myapp-dev-tasks", "Queue")

    preview_handler.handle_event(
        _pre_event(
            comp_urn,
            "stelvio:aws:Queue",
            op=OpType.DELETE,
            parent_urn=STACK_URN,
        )
    )
    preview_handler.handle_event(
        _pre_event(
            res_urn,
            "aws:sqs/queue:Queue",
            op=OpType.DELETE,
            parent_urn=comp_urn,
        )
    )
    preview_handler.handle_event(
        _outputs_event(
            res_urn,
            "aws:sqs/queue:Queue",
            op=OpType.DELETE,
            parent_urn=comp_urn,
        )
    )

    payload = preview_handler.build_json_summary(exit_code=0)

    assert payload["summary"] == {
        "to_create": 0,
        "to_update": 0,
        "to_delete": 1,
        "to_replace": 0,
    }
    assert payload["components"] == [
        {
            "type": "Queue",
            "name": "tasks",
            "operation": "delete",
            "resources": [
                {
                    "name": "myapp-dev-tasks",
                    "type": "aws:sqs/queue:Queue",
                    "operation": "delete",
                }
            ],
        }
    ]


def test_build_json_summary_for_failed_deploy_includes_warnings_errors_and_orphans(handler):
    handler.warning_diagnostics.append(WarningInfo(message="Provider warning"))

    orphan_urn = _resource_urn("aws:sqs/queue:Queue", "orphan-queue")
    handler.handle_event(
        EngineEvent(
            sequence=_next_seq(),
            timestamp=1000,
            diagnostic_event=DiagnosticEvent(
                message="queue failed",
                color="red",
                severity="error",
                urn=orphan_urn,
            ),
        )
    )

    payload = handler.build_json_summary(
        status="failed",
        outputs={},
        exit_code=1,
        message="Deploy failed",
    )

    assert payload["status"] == "failed"
    assert payload["warnings"] == [{"message": "Provider warning"}]
    assert payload["errors"] == [
        {
            "resource": "aws:sqs/queue:Queue",
            "message": "queue failed",
        }
    ]
    assert payload["other_resources"] == [
        {
            "name": "orphan-queue",
            "type": "aws:sqs/queue:Queue",
            "operation": "create",
            "error": "queue failed",
        }
    ]
    assert payload["message"] == "Deploy failed"


def test_build_json_summary_uses_fallback_error_when_no_resource_errors(handler):
    payload = handler.build_json_summary(status="failed", exit_code=1, fallback_error="boom")

    assert payload["status"] == "failed"
    assert payload["errors"] == [{"message": "boom"}]


def test_summary_event_is_silent_when_live_disabled(monkeypatch):
    fake_console = Mock()
    monkeypatch.setattr("stelvio.rich_deployment_handler.Console", lambda: fake_console)

    handler = RichDeploymentHandler("myapp", "dev", "preview", live_enabled=False)
    handler.total_resources = 2
    handler.handle_event(EngineEvent(sequence=_next_seq(), timestamp=1002, summary_event={}))

    fake_console.print.assert_not_called()
    assert handler.cleanup_status is None


def test_describe_urn_for_component(handler):
    comp_urn = _component_urn("DynamoTable", "users")
    handler.handle_event(_pre_event(comp_urn, "stelvio:aws:DynamoTable", parent_urn=STACK_URN))

    assert handler.describe_urn(comp_urn) == "DynamoTable users"


def test_describe_urn_for_tracked_resource_with_component_context(handler):
    parent_urn = _component_urn("Function", "api")
    res_urn = _resource_urn("aws:lambda/function:Function", "api-fn", "Function")

    handler.handle_event(_pre_event(parent_urn, "stelvio:aws:Function", parent_urn=STACK_URN))
    handler.handle_event(
        _pre_event(
            res_urn,
            "aws:lambda/function:Function",
            op=OpType.UPDATE,
            parent_urn=parent_urn,
        )
    )

    assert handler.describe_urn(res_urn) == "Function api → api-fn (Lambda Function)"


def test_describe_urn_fallback_for_untracked_resource(handler):
    urn = _resource_urn("aws:dynamodb/table:Table", "users-table")
    assert handler.describe_urn(urn) == "users-table (DynamoDB Table)"


def test_diagnostic_untracked_resource_uses_leaf_resource_type(handler):
    urn = (
        f"urn:pulumi:{STACK}::{PROJECT}::"
        "stelvio:aws:DynamoTable$aws:dynamodb/table:Table::myapp-dev-users"
    )
    event = EngineEvent(
        sequence=_next_seq(),
        timestamp=1000,
        diagnostic_event=DiagnosticEvent(
            message='all attributes must be indexed. Unused attributes: ["email"]',
            color="red",
            severity="error",
            urn=urn,
        ),
    )

    handler.handle_event(event)

    assert handler.resources[urn].type == "aws:dynamodb/table:Table"
    assert handler.describe_urn(urn) == "users (DynamoDB Table)"


def test_diagnostic_untracked_resource_attaches_to_matching_component(handler):
    comp_urn = _component_urn("DynamoTable", "users")
    handler.handle_event(_pre_event(comp_urn, "stelvio:aws:DynamoTable", parent_urn=STACK_URN))

    resource_urn = (
        f"urn:pulumi:{STACK}::{PROJECT}::"
        "stelvio:aws:DynamoTable$aws:dynamodb/table:Table::myapp-dev-users"
    )
    handler.handle_event(
        EngineEvent(
            sequence=_next_seq(),
            timestamp=1000,
            diagnostic_event=DiagnosticEvent(
                message='all attributes must be indexed. Unused attributes: ["email"]',
                color="red",
                severity="error",
                urn=resource_urn,
            ),
        )
    )

    assert handler.resource_to_component[resource_urn] == comp_urn
    assert handler.describe_urn(resource_urn) == "DynamoTable users → users (DynamoDB Table)"


def test_diagnostic_untracked_child_resource_attaches_to_component_by_prefix(handler):
    comp_urn = _component_urn("Function", "api")
    handler.handle_event(_pre_event(comp_urn, "stelvio:aws:Function", parent_urn=STACK_URN))

    resource_urn = (
        f"urn:pulumi:{STACK}::{PROJECT}::stelvio:aws:Function$aws:iam/role:Role::myapp-dev-api-r"
    )
    handler.handle_event(
        EngineEvent(
            sequence=_next_seq(),
            timestamp=1000,
            diagnostic_event=DiagnosticEvent(
                message="role creation failed",
                color="red",
                severity="error",
                urn=resource_urn,
            ),
        )
    )

    assert handler.resource_to_component[resource_urn] == comp_urn
    assert handler.describe_urn(resource_urn) == "Function api → api-r (IAM Role)"


def test_diagnostic_untracked_resource_without_component_shows_as_orphan(handler):
    resource_urn = _resource_urn("aws:dynamodb/table:Table", "standalone-users")
    handler.handle_event(
        EngineEvent(
            sequence=_next_seq(),
            timestamp=1000,
            diagnostic_event=DiagnosticEvent(
                message=(
                    "sdk-v2/provider2.go:572: sdk.helper_schema: "
                    'all attributes must be indexed. Unused attributes: ["email"]'
                ),
                color="red",
                severity="error",
                urn=resource_urn,
            ),
        )
    )

    assert len(handler.orphan_resources) == 1
    orphan = handler.orphan_resources[0]
    assert orphan.logical_name == "standalone-users"
    assert orphan.type == "aws:dynamodb/table:Table"
    assert orphan.error == 'all attributes must be indexed. Unused attributes: ["email"]'

    content = _render_content_text(handler)
    assert "Other resources" in content
    assert "DynamoDB Table" in content
    assert 'Unused attributes: ["email"]' in content


def test_clean_diagnostic_message_extracts_actionable_bullet():
    message = (
        "diffing urn:pulumi:dev::myapp::aws:dynamodb/table:Table::users: "
        '1 error occurred:\n\t* all attributes must be indexed. Unused attributes: ["email"]'
    )
    assert _clean_diagnostic_message(message) == (
        'all attributes must be indexed. Unused attributes: ["email"]'
    )


def test_clean_diagnostic_message_removes_provider_prefix():
    message = (
        "sdk-v2/provider2.go:572: sdk.helper_schema: "
        'all attributes must be indexed. Unused attributes: ["email"]'
    )
    assert _clean_diagnostic_message(message) == (
        'all attributes must be indexed. Unused attributes: ["email"]'
    )


def test_preview_render_shows_resource_error_inline(preview_handler):
    parent_urn = _component_urn("Function", "api")
    res_urn = _resource_urn("aws:lambda/function:Function", "api-fn", "Function")
    preview_handler.handle_event(
        _pre_event(
            res_urn,
            "aws:lambda/function:Function",
            op=OpType.UPDATE,
            parent_urn=parent_urn,
        )
    )
    preview_handler.handle_event(
        EngineEvent(
            sequence=_next_seq(),
            timestamp=1001,
            diagnostic_event=DiagnosticEvent(
                message="Invalid runtime",
                color="red",
                severity="error",
                urn=res_urn,
            ),
        )
    )

    content = _render_content_text(preview_handler)
    assert "Lambda Function" in content
    assert "Invalid runtime" in content


def test_failed_component_summary_shows_all_children_for_context(handler):
    handler.console = Console(record=True, width=160)
    parent_urn = _component_urn("Function", "api")
    role_urn = _resource_urn("aws:iam/role:Role", "api-role", "Function")
    lambda_urn = _resource_urn("aws:lambda/function:Function", "api-fn", "Function")

    handler.handle_event(_pre_event(role_urn, "aws:iam/role:Role", parent_urn=parent_urn))
    handler.handle_event(
        _pre_event(lambda_urn, "aws:lambda/function:Function", parent_urn=parent_urn)
    )
    handler.handle_event(_outputs_event(role_urn, "aws:iam/role:Role"))
    handler.handle_event(
        EngineEvent(
            sequence=_next_seq(),
            timestamp=1002,
            diagnostic_event=DiagnosticEvent(
                message="Invalid runtime",
                color="red",
                severity="error",
                urn=lambda_urn,
            ),
        )
    )

    handler._print_resources_summary()
    output = handler.console.export_text()
    assert "Function  api" in output
    assert "IAM Role" in output
    assert "Lambda Function" in output
    assert "Invalid runtime" in output


def test_warning_diagnostic_displayed_in_completion_with_context(handler):
    handler.console = Console(record=True, width=160)
    parent_urn = _component_urn("Function", "api")
    lambda_urn = _resource_urn("aws:lambda/function:Function", "api-fn", "Function")

    handler.handle_event(
        _pre_event(lambda_urn, "aws:lambda/function:Function", parent_urn=parent_urn)
    )
    handler.handle_event(_outputs_event(lambda_urn, "aws:lambda/function:Function"))
    handler.handle_event(
        EngineEvent(
            sequence=_next_seq(),
            timestamp=1002,
            diagnostic_event=DiagnosticEvent(
                message="Node.js 18.x runtime is deprecated",
                color="yellow",
                severity="warning",
                urn=lambda_urn,
            ),
        )
    )

    handler.show_completion()
    output = handler.console.export_text()
    assert "⚠ 1 warning" in output
    assert "Function api → api-fn (Lambda Function):" in output
    assert "Node.js 18.x runtime is deprecated" in output
    assert handler.failed_count == 0


def test_duplicate_warning_diagnostics_are_deduplicated(handler):
    handler.console = Console(record=True, width=160)
    parent_urn = _component_urn("Function", "api")
    lambda_urn = _resource_urn("aws:lambda/function:Function", "api-fn", "Function")

    handler.handle_event(
        _pre_event(lambda_urn, "aws:lambda/function:Function", parent_urn=parent_urn)
    )
    handler.handle_event(_outputs_event(lambda_urn, "aws:lambda/function:Function"))

    warning_event = EngineEvent(
        sequence=_next_seq(),
        timestamp=1002,
        diagnostic_event=DiagnosticEvent(
            message="Node.js 18.x runtime is deprecated",
            color="yellow",
            severity="warning",
            urn=lambda_urn,
        ),
    )
    handler.handle_event(warning_event)
    handler.handle_event(
        EngineEvent(
            sequence=_next_seq(),
            timestamp=1003,
            diagnostic_event=warning_event.diagnostic_event,
        )
    )

    handler.show_completion()
    output = handler.console.export_text()
    assert output.count("⚠ 1 warning") == 1
    assert output.count("Node.js 18.x runtime is deprecated") == 1


def test_interrupted_create_warning_is_user_friendly_and_actionable(handler):
    handler.console = Console(record=True, width=160)
    handler.handle_event(
        EngineEvent(
            sequence=_next_seq(),
            timestamp=1000,
            diagnostic_event=DiagnosticEvent(
                message=(
                    "urn:pulumi:dev::myapp::aws:iam/role:Role::myapp-dev-test-fn-d-r, "
                    "interrupted while creating"
                ),
                color="yellow",
                severity="warning",
                urn="",
            ),
        )
    )

    handler.show_completion()
    output = handler.console.export_text()
    assert "⚠ 1 warning" in output
    assert "test-fn-d-r (IAM Role):" in output
    assert (
        "A previous deploy appears to have been interrupted while creating this resource."
        in output
    )
    assert "Hint: Run `stlv state repair` to clear stale pending operations." in output
    assert "urn:pulumi:dev::myapp::aws:iam/role:Role::myapp-dev-test-fn-d-r" not in output
