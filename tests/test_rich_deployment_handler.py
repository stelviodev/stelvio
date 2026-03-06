"""Tests for RichDeploymentHandler component grouping (Phase 1 CLI redesign)."""

import itertools
import sys

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


def _render_content_text(handler: RichDeploymentHandler) -> str:
    """Return the textual content portion of the live render output."""
    renderable = handler._render()
    return renderable.renderables[0].plain


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
