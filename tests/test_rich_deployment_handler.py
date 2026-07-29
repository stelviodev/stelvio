"""Behavioral tests for RichDeploymentHandler: events in, the four public outputs out.

Every test feeds Pulumi engine events through ``handle_event`` and asserts on what the
user sees — the rendered terminal frame, the ``--json`` payload, the ``--stream`` events,
or the completion frame. See notes/rich-deployment-behavioral-tests.md for the rules.
"""

import itertools
import sys
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from textwrap import dedent
from unittest.mock import Mock, patch

from pulumi.automation import DiffKind, OpType, PropertyDiff
from pulumi.automation.events import (
    DiagnosticEvent,
    EngineEvent,
    ResOpFailedEvent,
    ResourcePreEvent,
    ResOutputsEvent,
    StepEventMetadata,
    StepEventStateMetadata,
    SummaryEvent,
)
from pytest import fixture, mark, param
from rich.console import Console
from rich.live import Live

from stelvio.rich_deployment_handler import RichDeploymentHandler
from stelvio.rich_deployment_model import _parse_stelvio_parent

# ---------------------------------------------------------------------------
# URN helpers
# ---------------------------------------------------------------------------
STACK = "dev"
PROJECT = "myapp"
STACK_URN = f"urn:pulumi:{STACK}::{PROJECT}::pulumi:pulumi:Stack::{STACK}"

APIGW_ACCOUNT_REF_URN = (
    f"urn:pulumi:{STACK}::{PROJECT}::aws:apigateway/account:Account::api-gateway-account-ref"
)
APIGW_ACCOUNT_URN = (
    f"urn:pulumi:{STACK}::{PROJECT}::aws:apigateway/account:Account::api-gateway-account"
)
APIGW_ROLE_URN = (
    f"urn:pulumi:{STACK}::{PROJECT}::aws:iam/role:Role::StelvioAPIGatewayPushToCloudWatchLogsRole"
)


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


@fixture(autouse=True)
def reset_sequence_counter(monkeypatch):
    """Reset event sequence counter to avoid inter-test coupling."""
    monkeypatch.setattr(sys.modules[__name__], "_seq_counter", itertools.count(1))


def _make_state(
    urn: str, resource_type: str, parent_urn: str = "", inputs: dict | None = None
) -> StepEventStateMetadata:
    return StepEventStateMetadata(
        type=resource_type,
        urn=urn,
        id="some-id",
        parent=parent_urn,
        provider="urn:pulumi:dev::myapp::pulumi:providers:aws::default",
        inputs=inputs,
    )


def _step_metadata(  # noqa: PLR0913
    urn: str,
    resource_type: str,
    op: OpType,
    parent_urn: str,
    diffs: list[str] | None,
    detailed_diff: dict[str, PropertyDiff] | None,
    old_inputs: dict | None,
    new_inputs: dict | None,
) -> StepEventMetadata:
    return StepEventMetadata(
        op=op,
        urn=urn,
        type=resource_type,
        provider="urn:pulumi:dev::myapp::pulumi:providers:aws::default",
        new=_make_state(urn, resource_type, parent_urn, inputs=new_inputs),
        old=_make_state(urn, resource_type, parent_urn, inputs=old_inputs) if old_inputs else None,
        diffs=diffs,
        detailed_diff=detailed_diff,
    )


def _pre_event(  # noqa: PLR0913
    urn: str,
    resource_type: str,
    op: OpType = OpType.CREATE,
    parent_urn: str = "",
    timestamp: int = 1000,
    diffs: list[str] | None = None,
    detailed_diff: dict[str, PropertyDiff] | None = None,
    old_inputs: dict | None = None,
    new_inputs: dict | None = None,
) -> EngineEvent:
    metadata = _step_metadata(
        urn, resource_type, op, parent_urn, diffs, detailed_diff, old_inputs, new_inputs
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
    detailed_diff: dict[str, PropertyDiff] | None = None,
    old_inputs: dict | None = None,
    new_inputs: dict | None = None,
) -> EngineEvent:
    metadata = _step_metadata(
        urn, resource_type, op, parent_urn, diffs, detailed_diff, old_inputs, new_inputs
    )
    return EngineEvent(
        sequence=_next_seq(),
        timestamp=timestamp,
        res_outputs_event=ResOutputsEvent(metadata=metadata),
    )


def _failed_event(urn: str, resource_type: str, timestamp: int = 1001) -> EngineEvent:
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


def _diagnostic_event(
    message: str, urn: str = "", *, severity: str = "error", timestamp: int = 1000
) -> EngineEvent:
    return EngineEvent(
        sequence=_next_seq(),
        timestamp=timestamp,
        diagnostic_event=DiagnosticEvent(
            message=message,
            color="red" if severity == "error" else "yellow",
            severity=severity,
            urn=urn,
        ),
    )


def _summary_event(timestamp: int = 2000) -> EngineEvent:
    """The end-of-operation event that flips the render to its final (spinner-free) frame."""
    return EngineEvent(
        sequence=_next_seq(),
        timestamp=timestamp,
        summary_event=SummaryEvent(
            maybe_corrupt=False,
            duration_seconds=1,
            resource_changes={},
            policy_packs={},
        ),
    )


# ---------------------------------------------------------------------------
# Output helpers: feed events, read one of the handler's four public outputs.
# Tests assert on these (terminal / --json / --stream) instead of handler
# internals, so the render + model code underneath stays free to change.
# See notes/rich-deployment-behavioral-tests.md.
#
# Multi-line expected frames are dedented triple-quoted strings — one source line per CLI
# line, indented one level past the assert. Use ``dedent("""\`` when the frame has no
# leading blank line; bare ``dedent("""`` when it does (the newline after the quotes IS
# the blank line). A blank line before the closing quotes is a trailing blank line.
# ---------------------------------------------------------------------------
DEFAULT_WIDTH = 100

# Patch target for the wall-clock duration the terminal surfaces print (kills flake).
_DURATION_TARGET = "stelvio.rich_deployment_handler.get_total_duration"
# Patch target for the wall clock the format module reads for *active* (unfinished)
# durations. Pinned via ``rendered(now=...)`` so an in-progress frame is deterministic.
_NOW_TARGET = "stelvio.rich_deployment_format.datetime"


def build_handler(  # noqa: PLR0913
    events: list[EngineEvent],
    *,
    app_name: str = "myapp",
    environment: str = "dev",
    operation: str = "deploy",
    show_unchanged: bool = False,
    compact: bool = False,
    stream_writer=None,
) -> RichDeploymentHandler:
    """Build a handler and replay ``events`` through it, with Rich's Live display stubbed out.

    ``Live.start``/``stop``/``refresh`` are stubbed so nothing hits the terminal, but
    ``live_enabled`` stays ``True`` — that keeps the real ``_handle_summary`` path, so a
    ``SummaryEvent`` faithfully flips the render to its final (spinner-free) frame.
    """
    with (
        patch.object(Live, "start"),
        patch.object(Live, "stop"),
        patch.object(Live, "refresh"),
    ):
        handler = RichDeploymentHandler(
            app_name,
            environment,
            operation,
            show_unchanged=show_unchanged,
            compact=compact,
            live_enabled=True,
            stream_writer=stream_writer,
        )
        for event in events:
            handler.handle_event(event)
    return handler


@contextmanager
def _frozen_clock(now: float | None):
    """Freeze the wall clocks the render reads: footer total always → 0; active-elapsed → ``now``
    when given. Renders nothing — the caller renders inside the ``with``."""
    with ExitStack() as stack:
        stack.enter_context(patch(_DURATION_TARGET, return_value=(0, 0)))
        if now is not None:
            dt = stack.enter_context(patch(_NOW_TARGET))
            dt.now.return_value.timestamp.return_value = now
        yield


def rendered(
    events: list[EngineEvent],
    *,
    width: int = DEFAULT_WIDTH,
    now: float | None = None,
    **handler_kwargs,
) -> str:
    """Return the terminal frame as plain text (colour stripped), rendered through ``__rich__``."""
    handler = build_handler(events, **handler_kwargs)
    console = Console(record=True, width=width, no_color=True)
    handler.console = console  # prod renders to its own console; keep width honest for diffs
    with _frozen_clock(now):
        console.print(handler)
    return console.export_text()


def styled(
    events: list[EngineEvent],
    *,
    width: int = DEFAULT_WIDTH,
    now: float | None = None,
    **handler_kwargs,
) -> str:
    """The colour twin of ``rendered``: the frame with Rich style markup woven inline
    (``[red]✗ [/red]``). Reads like the plain golden, colour made visible — no ANSI codes."""
    handler = build_handler(events, **handler_kwargs)
    console = Console(record=True, width=width)
    handler.console = console  # prod renders to its own console; keep width honest for diffs
    with _frozen_clock(now):
        segments = list(console.render(handler))
    return "".join(f"[{s.style}]{s.text}[/{s.style}]" if s.style else s.text for s in segments)


def completion(
    events: list[EngineEvent],
    *,
    output_lines: list[str] | None = None,
    width: int = DEFAULT_WIDTH,
    **handler_kwargs,
) -> str:
    """Return the ``show_completion`` frame (final ``✓ …`` line + counts) as plain text."""
    handler = build_handler(events, **handler_kwargs)
    handler.console = Console(record=True, width=width, no_color=True)
    with _frozen_clock(None):
        handler.show_completion(output_lines=output_lines)
    return handler.console.export_text()


def summary_json(  # noqa: PLR0913
    events: list[EngineEvent],
    *,
    status: str = "success",
    outputs: dict | None = None,
    exit_code: int = 0,
    fallback_error: str | None = None,
    message: str | None = None,
    **handler_kwargs,
) -> dict:
    """Return the ``--json`` payload with the nondeterministic ``duration`` dropped."""
    handler = build_handler(events, **handler_kwargs)
    payload = handler.build_json_summary(
        status=status,
        outputs=outputs,
        exit_code=exit_code,
        fallback_error=fallback_error,
        message=message,
    )
    payload.pop("duration", None)
    return payload


def stream_events(
    events: list[EngineEvent], *, keep_timestamps: bool = False, **handler_kwargs
) -> list[dict]:
    """Return the events pushed to the ``--stream`` writer, timestamps dropped by default."""
    captured: list[dict] = []
    build_handler(events, stream_writer=captured.append, **handler_kwargs)
    if not keep_timestamps:
        for event in captured:
            event.pop("timestamp", None)
    return captured


# ===========================================================================
# Output-helper characterization
# ---------------------------------------------------------------------------
# Lock the exact current output of each of the four public surfaces for a
# representative event sequence. These prove the helpers are deterministic and
# give a golden net before the welded tests migrate onto them. When the output
# intentionally changes, update the expected value here.
# ===========================================================================
_API_FUNC = _component_urn("Function", "api")
_API_ROLE = _resource_urn("aws:iam/role:Role", "api-role", "Function")
_API_LAMBDA = _resource_urn("aws:lambda/function:Function", "api-fn", "Function")


def _create_function_events() -> list[EngineEvent]:
    """A Function component creating its role + lambda child resources."""
    return [
        _pre_event(_API_ROLE, "aws:iam/role:Role", parent_urn=_API_FUNC),
        _pre_event(_API_LAMBDA, "aws:lambda/function:Function", parent_urn=_API_FUNC),
        _outputs_event(_API_ROLE, "aws:iam/role:Role"),
        _outputs_event(_API_LAMBDA, "aws:lambda/function:Function"),
    ]


def test_rendered_final_frame_shows_completed_component():
    events = [*_create_function_events(), _summary_event()]
    assert rendered(events) == "✓ Function api  (1.0s)\n\n"


def test_rendered_preview_frame_shows_changed_child():
    events = [
        _pre_event(
            _API_LAMBDA, "aws:lambda/function:Function", op=OpType.UPDATE, parent_urn=_API_FUNC
        ),
        _outputs_event(
            _API_LAMBDA, "aws:lambda/function:Function", op=OpType.UPDATE, parent_urn=_API_FUNC
        ),
        _summary_event(),
    ]
    assert rendered(events, operation="preview") == dedent("""\
        ~ Function api  (1 to update)
            ~ Lambda Function

        """)


def test_completion_frame_reports_component_and_resource_counts():
    assert completion(_create_function_events()) == dedent("""\
        ✓ Deployed in 0s
          1 component (2 resources) deployed
        """)


def test_summary_json_payload_for_create():
    assert summary_json(_create_function_events()) == {
        "operation": "deploy",
        "app": "myapp",
        "env": "dev",
        "status": "success",
        "exit_code": 0,
        "components": [
            {
                "type": "Function",
                "name": "api",
                "operation": "create",
                "resources": [
                    {"name": "api-role", "type": "aws:iam/role:Role", "operation": "create"},
                    {
                        "name": "api-fn",
                        "type": "aws:lambda/function:Function",
                        "operation": "create",
                    },
                ],
            }
        ],
        "summary": {
            "created": 2,
            "updated": 0,
            "deleted": 0,
            "replaced": 0,
            "failed": 0,
            "unchanged": 0,
        },
        "warnings": [],
        "errors": [],
    }


def test_stream_events_emitted_per_visible_resource():
    assert stream_events(_create_function_events()) == [
        {
            "event": "resource",
            "operation": "deploy",
            "app": "myapp",
            "env": "dev",
            "resource": {"name": "api-role", "type": "aws:iam/role:Role", "operation": "create"},
            "component": {"type": "Function", "name": "api"},
        },
        {
            "event": "resource",
            "operation": "deploy",
            "app": "myapp",
            "env": "dev",
            "resource": {
                "name": "api-fn",
                "type": "aws:lambda/function:Function",
                "operation": "create",
            },
            "component": {"type": "Function", "name": "api"},
        },
    ]


# ===========================================================================
# Colour dimension (dedicated)
# ---------------------------------------------------------------------------
# Colour is its own dimension (Beck: composable): the bulk tests strip colour
# and assert on the glyph + text, while these few pin the *semantic colour* of
# the glyph so it can never become the sole signal. Kept minimal on purpose.
# ``styled()`` projects the rendered segments back to Rich markup ([red]✗ [/red])
# — reads like the plain golden with colour woven in, no ANSI escape codes.
# ===========================================================================
def test_failed_frame_styling():
    comp = _component_urn("Function", "api")
    role = _resource_urn("aws:iam/role:Role", "api-role", "Function")
    events = [
        _pre_event(role, "aws:iam/role:Role", parent_urn=comp),
        _failed_event(role, "aws:iam/role:Role"),
        _summary_event(),
    ]
    assert styled(events) == dedent("""\
        [red]✗ [/red][bold]Function[/bold] api
            [red]✗ [/red]IAM Role[dim] (1.0s)[/dim]

        """)


def test_created_frame_styling():
    assert styled([*_create_function_events(), _summary_event()]) == (
        "[green]✓ [/green][bold]Function[/bold] api[dim]  (1.0s)[/dim]\n\n"
    )


def test_update_preview_styling():
    events = [
        _pre_event(
            _API_LAMBDA, "aws:lambda/function:Function", op=OpType.UPDATE, parent_urn=_API_FUNC
        ),
        _outputs_event(
            _API_LAMBDA, "aws:lambda/function:Function", op=OpType.UPDATE, parent_urn=_API_FUNC
        ),
        _summary_event(),
    ]
    assert styled(events, operation="preview") == dedent("""\
        [yellow]~ [/yellow][bold]Function[/bold] api[dim]  (1 to update)[/dim]
            [yellow]~ [/yellow]Lambda Function

        """)


# ===========================================================================
# _parse_stelvio_parent
# ===========================================================================
def test_parse_stelvio_parent_returns_none_for_a_too_short_urn():
    # Defensive: a URN with fewer than 4 `::` segments can't carry a component type/name.
    # Kept as a unit because it is unreachable via real Pulumi events (every emitted URN is
    # well-formed), so there is no seam path to exercise it. The observable grouping behaviors
    # this function drives ARE seam-pinned: leaf-type extraction from a `$`-composed parent by
    # test_resource_under_a_composed_function_urn_groups_by_leaf_type; component labels
    # (prefix strip) and non-Stelvio/pulumi URNs -> orphan by the grouping/JSON tests.
    assert _parse_stelvio_parent("urn:pulumi:dev") is None


# ===========================================================================
# Component duration (derived from children)
# ===========================================================================
def test_completed_component_duration_runs_to_its_last_child_to_finish():
    # the component's completed duration ends at its LAST child's end (max), not the first
    fn = _component_urn("Function", "api")
    role = _resource_urn("aws:iam/role:Role", "api-role", "Function")
    lam = _resource_urn("aws:lambda/function:Function", "api-fn", "Function")
    events = [
        _pre_event(role, "aws:iam/role:Role", parent_urn=fn),
        _pre_event(lam, "aws:lambda/function:Function", parent_urn=fn),
        _outputs_event(role, "aws:iam/role:Role", timestamp=1001),
        _outputs_event(lam, "aws:lambda/function:Function", timestamp=1005),
        _summary_event(),
    ]
    # start 1000, last child ends 1005 -> 5.0s (a min-of-children bug would show 1.0s)
    assert rendered(events) == "✓ Function api  (5.0s)\n\n"


def test_active_component_shows_live_elapsed_not_a_finished_childs_end():
    # An in-progress component's collapsed header must show LIVE elapsed (now - start), never a
    # "finished" duration taken from a child that already completed. This pins the
    # `ComponentInfo.end_time` active->None guard behaviorally: it surfaces ONLY here, where an
    # unchanged (all-SAME) component is still active AND rendered collapsed (show_unchanged=True).
    # Drop the guard and end_time falls back to the finished child's end -> a wrong "(5.0s)" on a
    # component that hasn't finished. (This is why the old plan's "delete both end_time units" was
    # wrong: the behavior is real and seam-observable, just via an obscure path.)
    fn = _component_urn("Function", "api")
    done = _resource_urn("aws:iam/role:Role", "api-role", "Function")
    running = _resource_urn("aws:lambda/function:Function", "api-fn", "Function")
    events = [
        _pre_event(done, "aws:iam/role:Role", op=OpType.SAME, parent_urn=fn),
        _pre_event(running, "aws:lambda/function:Function", op=OpType.SAME, parent_urn=fn),
        _outputs_event(done, "aws:iam/role:Role", op=OpType.SAME, timestamp=1005),
    ]
    assert rendered(events, show_unchanged=True, now=1000) == dedent("""
        ~ Function api  (0.0s)

        ⠋ Deploying  0/1 complete  0s
        """)


# ===========================================================================
# Event handling: component grouping
# ===========================================================================
def test_multiple_components_each_group_their_resources():
    """Each resource nests under its own Stelvio parent component, keyed by type."""
    func = _component_urn("Function", "api")
    table = _component_urn("DynamoTable", "users")
    components = summary_json(
        [
            _pre_event(
                _resource_urn("aws:iam/role:Role", "api-role", "Function"),
                "aws:iam/role:Role",
                parent_urn=func,
            ),
            _pre_event(
                _resource_urn("aws:dynamodb/table:Table", "users-table", "DynamoTable"),
                "aws:dynamodb/table:Table",
                parent_urn=table,
            ),
        ]
    )["components"]
    assert [(c["type"], c["name"]) for c in components] == [
        ("Function", "api"),
        ("DynamoTable", "users"),
    ]
    assert [r["type"] for r in components[0]["resources"]] == ["aws:iam/role:Role"]
    assert [r["type"] for r in components[1]["resources"]] == ["aws:dynamodb/table:Table"]


def test_orphan_resource_appears_in_other_resources():
    """A raw resource with no Stelvio parent surfaces under other_resources, not components."""
    urn = f"urn:pulumi:{STACK}::{PROJECT}::aws:s3/bucketV2:BucketV2::manual-bucket"
    payload = summary_json([_pre_event(urn, "aws:s3/bucketV2:BucketV2")])
    assert payload["components"] == []
    assert [r["type"] for r in payload["other_resources"]] == ["aws:s3/bucketV2:BucketV2"]


def test_pulumi_internal_resources_are_skipped():
    """Stack/provider events produce no components, resources, or counts."""
    payload = summary_json(
        [
            _pre_event(
                f"urn:pulumi:{STACK}::{PROJECT}::pulumi:pulumi:Stack::dev", "pulumi:pulumi:Stack"
            ),
            _pre_event(
                f"urn:pulumi:{STACK}::{PROJECT}::pulumi:providers:aws::default",
                "pulumi:providers:aws",
            ),
        ]
    )
    assert payload["components"] == []
    assert "other_resources" not in payload
    assert not any(payload["summary"].values())


def test_component_event_itself_is_not_counted_as_a_resource():
    """A bare ComponentResource event registers the component but adds no resource."""
    payload = summary_json([_pre_event(_component_urn("Function", "api"), "stelvio:aws:Function")])
    assert payload["components"] == [
        {"type": "Function", "name": "api", "operation": "create", "resources": []}
    ]
    assert payload["summary"]["created"] == 0


def test_duplicate_component_events_ignored():
    """A repeated top-level component event does not create a duplicate component."""
    event = _pre_event(
        _component_urn("Function", "api"), "stelvio:aws:Function", parent_urn=STACK_URN
    )
    components = summary_json([event, event])["components"]
    assert [(c["type"], c["name"]) for c in components] == [("Function", "api")]


def test_component_operation_is_highest_priority_of_its_children():
    # the component's JSON operation is the highest-priority child op (create > update)
    fn = _component_urn("Function", "api")
    role = _resource_urn("aws:iam/role:Role", "api-role", "Function")
    lam = _resource_urn("aws:lambda/function:Function", "api-fn", "Function")
    events = [
        _pre_event(role, "aws:iam/role:Role", op=OpType.CREATE, parent_urn=fn),
        _pre_event(lam, "aws:lambda/function:Function", op=OpType.UPDATE, parent_urn=fn),
        _outputs_event(role, "aws:iam/role:Role", op=OpType.CREATE),
        _outputs_event(lam, "aws:lambda/function:Function", op=OpType.UPDATE),
    ]
    assert summary_json(events)["components"] == [
        {
            "type": "Function",
            "name": "api",
            "operation": "create",
            "resources": [
                {"name": "api-role", "type": "aws:iam/role:Role", "operation": "create"},
                {"name": "api-fn", "type": "aws:lambda/function:Function", "operation": "update"},
            ],
        }
    ]


def test_component_error_is_propagated_from_failed_child():
    # a failed child's error string bubbles up to the component's JSON error
    fn = _component_urn("Function", "api")
    lam = _resource_urn("aws:lambda/function:Function", "api-fn", "Function")
    events = [
        _pre_event(lam, "aws:lambda/function:Function", op=OpType.CREATE, parent_urn=fn),
        _diagnostic_event("Invalid runtime", lam, timestamp=1001),
    ]
    assert summary_json(events)["components"] == [
        {
            "type": "Function",
            "name": "api",
            "operation": "create",
            "resources": [
                {
                    "name": "api-fn",
                    "type": "aws:lambda/function:Function",
                    "operation": "create",
                    "error": "Invalid runtime",
                }
            ],
            "error": "Invalid runtime",
        }
    ]


def test_component_shows_active_until_all_children_complete():
    """The component header stays on the active glyph until every child finishes."""
    comp = _component_urn("Function", "api")
    role = _resource_urn("aws:iam/role:Role", "api-role", "Function")
    func = _resource_urn("aws:lambda/function:Function", "api-func", "Function")
    started = [
        _pre_event(role, "aws:iam/role:Role", parent_urn=comp),
        _pre_event(func, "aws:lambda/function:Function", parent_urn=comp),
    ]
    assert rendered(started, now=1000) == dedent("""
        | Function api
            | IAM Role (0.0s)
            | Lambda Function (0.0s)

        ⠋ Deploying  0/1 complete  0s
        """)
    # one child done, the other still running -> component header stays active
    assert rendered([*started, _outputs_event(role, "aws:iam/role:Role")], now=1000) == dedent("""
        | Function api
            ✓ IAM Role (1.0s)
            | Lambda Function (0.0s)

        ⠋ Deploying  0/1 complete  0s
        """)
    # both done + summary -> completed final frame
    done = [
        *started,
        _outputs_event(role, "aws:iam/role:Role"),
        _outputs_event(func, "aws:lambda/function:Function"),
        _summary_event(),
    ]
    assert rendered(done) == "✓ Function api  (1.0s)\n\n"


def test_component_fails_when_a_child_fails():
    comp = _component_urn("Function", "api")
    role = _resource_urn("aws:iam/role:Role", "api-role", "Function")
    events = [
        _pre_event(role, "aws:iam/role:Role", parent_urn=comp),
        _failed_event(role, "aws:iam/role:Role"),
        _summary_event(),
    ]
    assert rendered(events) == dedent("""\
        ✗ Function api
            ✗ IAM Role (1.0s)

        """)


def test_duplicate_resource_events_ignored():
    """A repeated resource pre-event is not double-counted."""
    comp = _component_urn("Function", "api")
    event = _pre_event(
        _resource_urn("aws:iam/role:Role", "api-role", "Function"),
        "aws:iam/role:Role",
        parent_urn=comp,
    )
    payload = summary_json([event, event])
    assert [r["type"] for r in payload["components"][0]["resources"]] == ["aws:iam/role:Role"]
    assert payload["summary"]["created"] == 1


# --- Nested component parent resolution ---
# When a component creates a Function internally with parent=self,
# the Function appears as a sub-component in the tree.
# E.g. TopicSubscription has a Function child, which has Lambda/IAM children.


def _component_event(comp_type: str, name: str, parent_urn: str = STACK_URN) -> EngineEvent:
    """A Stelvio ComponentResource pre-event (URN is ``_component_urn(comp_type, name)``)."""
    return _pre_event(
        _component_urn(comp_type, name), f"stelvio:aws:{comp_type}", parent_urn=parent_urn
    )


@mark.parametrize("parent_type", ["TopicSubscription", "Api", "Cron"])
def test_parent_component_nests_its_function_child(parent_type):
    """A Function a parent component creates for itself nests under it, not at the top level."""
    parent = _component_urn(parent_type, "outer")
    func = _component_urn("Function", "inner")
    components = summary_json(
        [
            _component_event(parent_type, "outer"),
            _component_event("Function", "inner", parent),
            _pre_event(
                _resource_urn("aws:iam/role:Role", "inner-role", "Function"),
                "aws:iam/role:Role",
                parent_urn=func,
            ),
            _pre_event(
                _resource_urn("aws:lambda/function:Function", "inner-fn", "Function"),
                "aws:lambda/function:Function",
                parent_urn=func,
            ),
        ]
    )["components"]
    # Exactly one top-level component (the parent); the Function nests under it with its resources.
    assert [(c["type"], c["name"]) for c in components] == [(parent_type, "outer")]
    nested = components[0]["components"]
    assert [(c["type"], c["name"]) for c in nested] == [("Function", "inner")]
    assert {r["type"] for r in nested[0]["resources"]} == {
        "aws:iam/role:Role",
        "aws:lambda/function:Function",
    }


def test_resource_under_a_composed_function_urn_groups_by_leaf_type():
    """A Function nested inside another component: Pulumi joins the two Stelvio types with `$`,
    so the child resource's parent URN is
    ``stelvio:aws:TopicSubscription$stelvio:aws:Function::on-notify``. The resource must group
    under the LEAF component (Function on-notify), not the outer TopicSubscription. Real shape
    verified via code audit + offline Pulumi URN probe. This is the seam pin for the `$`
    leaf-type extraction in ``_parse_stelvio_parent``."""
    nested_func = (
        f"urn:pulumi:{STACK}::{PROJECT}"
        "::stelvio:aws:TopicSubscription$stelvio:aws:Function::on-notify"
    )
    components = summary_json(
        [
            _pre_event(
                _resource_urn("aws:lambda/function:Function", "on-notify-fn"),
                "aws:lambda/function:Function",
                parent_urn=nested_func,
            ),
        ]
    )["components"]
    assert [(c["type"], c["name"]) for c in components] == [("Function", "on-notify")]
    assert [r["type"] for r in components[0]["resources"]] == ["aws:lambda/function:Function"]


def test_out_of_order_child_component_before_parent_still_nests():
    """Child component events may arrive before the parent (e.g. destroy ordering)."""
    parent = _component_urn("TopicSubscription", "outer")
    func = _component_urn("Function", "inner")
    components = summary_json(
        [
            _component_event("Function", "inner", parent),  # child first
            _pre_event(
                _resource_urn("aws:lambda/function:Function", "inner-fn", "Function"),
                "aws:lambda/function:Function",
                parent_urn=func,
            ),
            _component_event("TopicSubscription", "outer"),  # parent arrives later
        ]
    )["components"]
    assert [(c["type"], c["name"]) for c in components] == [("TopicSubscription", "outer")]
    assert [(c["type"], c["name"]) for c in components[0]["components"]] == [("Function", "inner")]


def test_duplicate_nested_component_events_ignored():
    """A repeated nested component event does not duplicate the sub-component."""
    parent = _component_urn("TopicSubscription", "outer")
    func = _component_urn("Function", "inner")
    nested = _component_event("Function", "inner", parent)
    components = summary_json(
        [
            _component_event("TopicSubscription", "outer"),
            nested,
            nested,  # duplicate nested component event
            _pre_event(
                _resource_urn("aws:lambda/function:Function", "inner-fn", "Function"),
                "aws:lambda/function:Function",
                parent_urn=func,
            ),
        ]
    )["components"]
    assert [(c["type"], c["name"]) for c in components[0]["components"]] == [("Function", "inner")]


def test_component_holds_direct_resource_and_nested_component():
    """A parent can hold both a direct resource and a nested component; nested resources count."""
    parent = _component_urn("TopicSubscription", "outer")
    func = _component_urn("Function", "inner")
    payload = summary_json(
        [
            _component_event("TopicSubscription", "outer"),
            _component_event("Function", "inner", parent),
            _pre_event(
                _resource_urn(
                    "aws:sns/topicSubscription:TopicSubscription", "outer", "TopicSubscription"
                ),
                "aws:sns/topicSubscription:TopicSubscription",
                parent_urn=parent,
            ),
            _pre_event(
                _resource_urn("aws:lambda/function:Function", "inner-fn", "Function"),
                "aws:lambda/function:Function",
                parent_urn=func,
            ),
        ]
    )
    top = payload["components"][0]
    assert [r["type"] for r in top["resources"]] == ["aws:sns/topicSubscription:TopicSubscription"]
    assert [c["type"] for c in top["components"]] == ["Function"]
    # Both the direct resource and the resource inside the nested component are counted.
    assert payload["summary"]["created"] == 2


def test_nested_function_completion_bubbles_to_outer():
    """The outer component reflects completion of a resource inside its nested Function."""
    parent = _component_urn("TopicSubscription", "outer")
    func = _component_urn("Function", "inner")
    lam = _resource_urn("aws:lambda/function:Function", "inner-fn", "Function")
    base = [
        _component_event("TopicSubscription", "outer"),
        _component_event("Function", "inner", parent),
        _pre_event(lam, "aws:lambda/function:Function", parent_urn=func),
    ]
    # nested resource still running -> outer stays active
    assert rendered(base, now=1000) == dedent("""
        | TopicSubscription outer
            | Function inner
                | Lambda Function (0.0s)

        ⠋ Deploying  0/1 complete  0s
        """)
    # nested resource completes -> outer shows completed
    done = [
        *base,
        _outputs_event(lam, "aws:lambda/function:Function", timestamp=1002),
        _summary_event(),
    ]
    assert rendered(done) == "✓ TopicSubscription outer  (2.0s)\n\n"


def test_nested_function_failure_bubbles_to_outer():
    """A failure inside a nested Function marks the whole tree failed, indented top to bottom."""
    parent = _component_urn("TopicSubscription", "outer")
    func = _component_urn("Function", "inner")
    lam = _resource_urn("aws:lambda/function:Function", "inner-fn", "Function")
    events = [
        _component_event("TopicSubscription", "outer"),
        _component_event("Function", "inner", parent),
        _pre_event(lam, "aws:lambda/function:Function", parent_urn=func),
        _failed_event(lam, "aws:lambda/function:Function"),
        _summary_event(),
    ]
    assert rendered(events) == dedent("""\
        ✗ TopicSubscription outer
            ✗ Function inner
                ✗ Lambda Function (1.0s)

        """)


# ===========================================================================
# Nested tree rendering
# ===========================================================================
def test_render_shows_nested_component_indentation():
    sub_urn = _component_urn("TopicSubscription", "on-notify-sub")
    func_urn = _component_urn("Function", "on-notify")
    events = [
        _pre_event(sub_urn, "stelvio:aws:TopicSubscription", parent_urn=STACK_URN),
        _pre_event(func_urn, "stelvio:aws:Function", parent_urn=sub_urn),
        _pre_event(
            _resource_urn("aws:lambda/function:Function", "on-notify-fn", "Function"),
            "aws:lambda/function:Function",
            parent_urn=func_urn,
        ),
    ]

    assert rendered(events, now=1000) == dedent("""
        | TopicSubscription on-notify-sub
            | Function on-notify
                | Lambda Function (0.0s)

        ⠋ Deploying  0/1 complete  0s
        """)


# ===========================================================================
# Progress counter (component-level)
# ===========================================================================
def test_progress_counts_components():
    func_urn = _component_urn("Function", "api")
    table_urn = _component_urn("DynamoTable", "users")
    role_urn = _resource_urn("aws:iam/role:Role", "api-role", "Function")
    lambda_urn = _resource_urn("aws:lambda/function:Function", "api-func", "Function")
    table_res_urn = _resource_urn("aws:dynamodb/table:Table", "users-table", "DynamoTable")
    events = [
        _pre_event(role_urn, "aws:iam/role:Role", parent_urn=func_urn),
        _pre_event(lambda_urn, "aws:lambda/function:Function", parent_urn=func_urn),
        _pre_event(table_res_urn, "aws:dynamodb/table:Table", parent_urn=table_urn),
        # table done, function still active
        _outputs_event(table_res_urn, "aws:dynamodb/table:Table"),
    ]

    # Footer counts completed/total components: table done, function still active → 1/2.
    assert rendered(events, now=1000) == dedent("""
        | Function api
            | IAM Role (0.0s)
            | Lambda Function (0.0s)
        ✓ DynamoTable users  (1.0s)

        ⠋ Deploying  1/2 complete  0s
        """)


def test_noop_deploy_footer_omits_component_counter_when_only_unchanged_hidden():
    parent_urn = _component_urn("Function", "api")
    lambda_urn = _resource_urn("aws:lambda/function:Function", "api-fn", "Function")
    events = [
        _pre_event(
            lambda_urn, "aws:lambda/function:Function", op=OpType.SAME, parent_urn=parent_urn
        ),
        _outputs_event(lambda_urn, "aws:lambda/function:Function", op=OpType.SAME),
    ]

    # Unchanged component is hidden -> empty content, bare footer with no N/M counter.
    assert rendered(events) == "\n⠋ Deploying  0s\n"


def test_deploy_footer_keeps_component_counter_for_visible_changes():
    parent_urn = _component_urn("Function", "api")
    lambda_urn = _resource_urn("aws:lambda/function:Function", "api-fn", "Function")
    events = [
        _pre_event(
            lambda_urn, "aws:lambda/function:Function", op=OpType.UPDATE, parent_urn=parent_urn
        ),
        _outputs_event(lambda_urn, "aws:lambda/function:Function", op=OpType.UPDATE),
    ]

    # Component completed (outputs received) but no summary yet -> active footer keeps 1/1.
    assert rendered(events) == dedent("""
        ✓ Function api  (1.0s)

        ⠋ Deploying  1/1 complete  0s
        """)


# ===========================================================================
# Property diffs and replacement warnings
# ===========================================================================


def _pdiff(kind: DiffKind) -> PropertyDiff:
    return PropertyDiff(diff_kind=kind, input_diff=False)


def test_property_diffs_shown_in_preview_render():
    parent_urn = _component_urn("Function", "api")
    res_urn = _resource_urn("aws:lambda/function:Function", "api-fn", "Function")
    events = [
        _pre_event(
            res_urn,
            "aws:lambda/function:Function",
            op=OpType.UPDATE,
            parent_urn=parent_urn,
            detailed_diff={"memorySize": _pdiff(DiffKind.UPDATE)},
            old_inputs={"memorySize": 128},
            new_inputs={"memorySize": 256},
        ),
    ]

    assert rendered(events, operation="preview") == dedent("""
        ~ Function api  (1 to update)
            ~ Lambda Function
                * memorySize = 128 -> 256

        ⠋ Analyzing differences  0/1 complete  0s
        """)


def test_preview_lists_added_and_removed_properties_sorted():
    """Added props render `+ name = value`, removed `- name`, alphabetical, with the
    forces-replacement marker on replace-kind adds/deletes."""
    parent_urn = _component_urn("Function", "api")
    res_urn = _resource_urn("aws:lambda/function:Function", "api-fn", "Function")
    events = [
        _pre_event(
            res_urn,
            "aws:lambda/function:Function",
            op=OpType.UPDATE,
            parent_urn=parent_urn,
            detailed_diff={
                "runtime": _pdiff(DiffKind.ADD),
                "handler": _pdiff(DiffKind.DELETE),
                "architectures": _pdiff(DiffKind.ADD_REPLACE),
            },
            new_inputs={"runtime": "python3.12", "architectures": "arm64"},
        ),
    ]

    assert rendered(events, operation="preview") == dedent("""
        ~ Function api  (1 to replace)
            ~ Lambda Function
                + architectures = arm64 (forces replacement)
                - handler
                + runtime = python3.12

        ⠋ Analyzing differences  0/1 complete  0s
        """)


def test_preview_indents_property_diffs_under_nested_component():
    """Diff lines sit one level deeper when the resource lives in a nested component."""
    sub_urn = _component_urn("TopicSubscription", "on-notify-sub")
    func_urn = _component_urn("Function", "on-notify")
    res_urn = _resource_urn("aws:lambda/function:Function", "on-notify-fn", "Function")
    events = [
        _pre_event(
            sub_urn, "stelvio:aws:TopicSubscription", op=OpType.UPDATE, parent_urn=STACK_URN
        ),
        _pre_event(func_urn, "stelvio:aws:Function", op=OpType.UPDATE, parent_urn=sub_urn),
        _pre_event(
            res_urn,
            "aws:lambda/function:Function",
            op=OpType.UPDATE,
            parent_urn=func_urn,
            detailed_diff={"memorySize": _pdiff(DiffKind.UPDATE)},
            old_inputs={"memorySize": 128},
            new_inputs={"memorySize": 256},
        ),
    ]

    assert rendered(events, operation="preview") == dedent("""
        ~ TopicSubscription on-notify-sub  (1 to update)
            ~ Function on-notify  (1 to update)
                ~ Lambda Function
                    * memorySize = 128 -> 256

        ⠋ Analyzing differences  0/1 complete  0s
        """)


def test_preview_collapses_json_property_change_to_changed_paths():
    """A JSON-string property diff collapses to a path count, then lists each changed
    path with old/new; a value missing on one side is explicit."""
    parent_urn = _component_urn("Function", "api")
    res_urn = _resource_urn("aws:iam/policy:Policy", "api-p", "Function")
    events = [
        _pre_event(
            res_urn,
            "aws:iam/policy:Policy",
            op=OpType.UPDATE,
            parent_urn=parent_urn,
            detailed_diff={"policy": _pdiff(DiffKind.UPDATE)},
            old_inputs={"policy": '{"Statement":[{"Sid":"A","Resource":"arn:aws:sqs:q"}]}'},
            new_inputs={"policy": '{"Statement":[{"Resource":"arn:aws:sqs:q2"}]}'},
        ),
    ]

    assert rendered(events, operation="preview") == dedent("""
        ~ Function api  (1 to update)
            ~ IAM Policy
                * policy (JSON changed (2 paths))
                    ~ Statement[0].Resource
                      old: arn:aws:sqs:q
                      new: arn:aws:sqs:q2
                    ~ Statement[0].Sid
                      old: A
                      new: <missing>

        ⠋ Analyzing differences  0/1 complete  0s
        """)


def test_preview_summarizes_dict_property_change_by_key():
    """A dict property diff summarizes key counts, details each key once, and masks
    provider fingerprints (hex and uuid) as `output<string>`."""
    parent_urn = _component_urn("Function", "api")
    res_urn = _resource_urn("aws:lambda/function:Function", "api-fn", "Function")
    events = [
        _pre_event(
            res_urn,
            "aws:lambda/function:Function",
            op=OpType.UPDATE,
            parent_urn=parent_urn,
            detailed_diff={"environment.variables": _pdiff(DiffKind.UPDATE)},
            old_inputs={
                "environment": {
                    "variables": {
                        "STLV_QUEUE_ARN": "arn:aws:sqs:q",
                        "STLV_TABLE_ARN": "arn:aws:dynamodb:t",
                    }
                }
            },
            new_inputs={
                "environment": {
                    "variables": {
                        "STLV_QUEUE_ARN": "0123456789abcdef0123456789abcdef",
                        "STLV_TABLE_ARN": "04da6b54-80e4-46f7-96ec-b56ff0331ba9",
                        "LOG_LEVEL": "debug",
                    }
                }
            },
        ),
    ]

    assert rendered(events, operation="preview") == dedent("""
        ~ Function api  (1 to update)
            ~ Lambda Function
                * environment.variables (keys: 2 changed, 1 added)
                    ~ STLV_QUEUE_ARN
                      old: arn:aws:sqs:q
                      new: output<string>
                    ~ STLV_TABLE_ARN
                      old: arn:aws:dynamodb:t
                      new: output<string>
                    + LOG_LEVEL = debug

        ⠋ Analyzing differences  0/1 complete  0s
        """)


def test_preview_widens_diff_values_on_wide_terminal():
    """Value truncation follows terminal width: a wide console shows values a narrow
    default would ellipsize."""
    parent_urn = _component_urn("Function", "api")
    res_urn = _resource_urn("aws:lambda/function:Function", "api-fn", "Function")
    long_old = "x" * 50
    long_new = "y" * 50
    events = [
        _pre_event(
            res_urn,
            "aws:lambda/function:Function",
            op=OpType.UPDATE,
            parent_urn=parent_urn,
            detailed_diff={"environment.variables": _pdiff(DiffKind.UPDATE)},
            old_inputs={"environment": {"variables": {"BIG": long_old}}},
            new_inputs={"environment": {"variables": {"BIG": long_new}}},
        ),
    ]

    assert rendered(events, operation="preview", width=200) == dedent(f"""
        ~ Function api  (1 to update)
            ~ Lambda Function
                * environment.variables (keys: 1 changed)
                    ~ BIG
                      old: {long_old}
                      new: {long_new}

        ⠋ Analyzing differences  0/1 complete  0s
        """)


def test_replacement_warning_shown_in_render():
    parent_urn = _component_urn("DynamoTable", "users")
    res_urn = _resource_urn("aws:dynamodb/table:Table", "users-table", "DynamoTable")
    events = [
        _pre_event(
            res_urn,
            "aws:dynamodb/table:Table",
            op=OpType.UPDATE,
            parent_urn=parent_urn,
            detailed_diff={"name": _pdiff(DiffKind.UPDATE_REPLACE)},
            old_inputs={"name": "users-v1"},
            new_inputs={"name": "users-v2"},
        ),
    ]

    assert rendered(events, operation="preview") == dedent("""
        ~ DynamoTable users  (1 to replace)
            ~ DynamoDB Table
                * name = users-v1 -> users-v2 (forces replacement)
                !! Replacement recreates resource; data may be lost.

        ⠋ Analyzing differences  0/1 complete  0s
        """)


def test_replacement_warning_shown_for_replace_operation_without_detailed_diff():
    parent_urn = _component_urn("DynamoTable", "users")
    res_urn = _resource_urn("aws:dynamodb/table:Table", "users-table", "DynamoTable")
    events = [
        _pre_event(res_urn, "aws:dynamodb/table:Table", op=OpType.REPLACE, parent_urn=parent_urn),
    ]

    assert rendered(events, operation="preview") == dedent("""
        ± DynamoTable users  (1 to replace)
            ± DynamoDB Table
                !! Replacement recreates resource; data may be lost.

        ⠋ Analyzing differences  0/1 complete  0s
        """)


def test_no_data_loss_warning_for_non_data_resource_replacement():
    parent_urn = _component_urn("Function", "api")
    res_urn = _resource_urn("aws:lambda/function:Function", "api-fn", "Function")
    events = [
        _pre_event(
            res_urn, "aws:lambda/function:Function", op=OpType.REPLACE, parent_urn=parent_urn
        ),
    ]

    assert rendered(events, operation="preview") == dedent("""
        ± Function api  (1 to replace)
            ± Lambda Function

        ⠋ Analyzing differences  0/1 complete  0s
        """)


@mark.parametrize(
    "kind",
    [DiffKind.ADD_REPLACE, DiffKind.UPDATE_REPLACE, DiffKind.DELETE_REPLACE],
    ids=["add-replace", "update-replace", "delete-replace"],
)
def test_property_diff_that_forces_replacement_counts_as_replaced(kind):
    # Any _REPLACE_KINDS diff on a property forces replacement, so the resource counts as
    # "replaced". Exercising all three kinds pins _REPLACE_KINDS membership at the seam:
    # dropping ADD_REPLACE/DELETE_REPLACE from the set would silently stop flagging a real
    # class of destructive replacements (and drop their data-loss warning) with nothing red.
    parent = _component_urn("DynamoTable", "users")
    res = _resource_urn("aws:dynamodb/table:Table", "users-table", "DynamoTable")
    events = [
        _pre_event(
            res,
            "aws:dynamodb/table:Table",
            op=OpType.UPDATE,
            parent_urn=parent,
            detailed_diff={"hashKey": _pdiff(kind)},
        ),
        _outputs_event(res, "aws:dynamodb/table:Table", op=OpType.UPDATE, parent_urn=parent),
    ]
    assert summary_json(events)["summary"] == {
        "created": 0,
        "updated": 0,
        "deleted": 0,
        "replaced": 1,
        "failed": 0,
        "unchanged": 0,
    }


def test_preview_render_keeps_children_visible_after_completion():
    parent_urn = _component_urn("Function", "api")
    res_urn = _resource_urn("aws:lambda/function:Function", "api-fn", "Function")
    events = [
        _pre_event(
            res_urn,
            "aws:lambda/function:Function",
            op=OpType.UPDATE,
            parent_urn=parent_urn,
            detailed_diff={"memorySize": _pdiff(DiffKind.UPDATE)},
            old_inputs={"memorySize": 128},
            new_inputs={"memorySize": 256},
        ),
        _outputs_event(
            res_urn, "aws:lambda/function:Function", op=OpType.UPDATE, parent_urn=parent_urn
        ),
    ]

    assert rendered(events, operation="preview") == dedent("""
        ~ Function api  (1 to update)
            ~ Lambda Function
                * memorySize = 128 -> 256

        ⠋ Analyzing differences  1/1 complete  0s
        """)


def test_preview_header_summarizes_mixed_operations():
    # A component whose children span more than one operation lists each in the header,
    # comma-joined. Pins the multi-entry preview_summary join (single-op headers never
    # exercise the ", " separator).
    parent_urn = _component_urn("Function", "api")
    role = _resource_urn("aws:iam/role:Role", "api-role", "Function")
    fn = _resource_urn("aws:lambda/function:Function", "api-fn", "Function")
    events = [
        _pre_event(role, "aws:iam/role:Role", op=OpType.CREATE, parent_urn=parent_urn),
        _outputs_event(role, "aws:iam/role:Role", op=OpType.CREATE, parent_urn=parent_urn),
        _pre_event(fn, "aws:lambda/function:Function", op=OpType.UPDATE, parent_urn=parent_urn),
        _outputs_event(
            fn, "aws:lambda/function:Function", op=OpType.UPDATE, parent_urn=parent_urn
        ),
    ]

    assert rendered(events, operation="preview") == dedent("""
        + Function api  (1 to create, 1 to update)
            + IAM Role
            ~ Lambda Function

        ⠋ Analyzing differences  1/1 complete  0s
        """)


def test_preview_render_hides_empty_component_placeholders():
    queue_urn = _component_urn("Queue", "tasks")
    events = [_pre_event(queue_urn, "stelvio:aws:Queue", parent_urn=STACK_URN)]

    # empty component omitted — only the footer remains
    assert rendered(events, operation="preview") == "\n⠋ Analyzing differences  0s\n"


def test_no_property_diffs_in_deploy_render():
    """Deploy (non-preview) should NOT show property diffs."""
    parent_urn = _component_urn("Function", "api")
    res_urn = _resource_urn("aws:lambda/function:Function", "api-fn", "Function")
    events = [
        _pre_event(
            res_urn,
            "aws:lambda/function:Function",
            op=OpType.UPDATE,
            parent_urn=parent_urn,
            detailed_diff={"memorySize": _pdiff(DiffKind.UPDATE)},
            old_inputs={"memorySize": 128},
            new_inputs={"memorySize": 256},
        ),
    ]

    # deploy (non-preview) shows no property diffs — just the active tree + footer
    assert rendered(events, now=1000) == dedent("""
        | Function api
            | Lambda Function (0.0s)

        ⠋ Deploying  0/1 complete  0s
        """)


# --- Compact mode ---


def test_compact_preview_header_only():
    parent_urn = _component_urn("Function", "api")
    res_urn = _resource_urn("aws:lambda/function:Function", "api-fn", "Function")
    events = [
        _pre_event(
            res_urn,
            "aws:lambda/function:Function",
            op=OpType.CREATE,
            parent_urn=parent_urn,
            detailed_diff={"handler": _pdiff(DiffKind.ADD)},
            new_inputs={"handler": "src/handler.handler"},
        ),
    ]

    # compact: header + count only — no child lines or property diffs
    assert rendered(events, operation="preview", compact=True) == dedent("""
        + Function api  (1 resource to create)

        ⠋ Analyzing differences  0/1 complete  0s
        """)


def test_compact_shows_preview_summary():
    parent_urn = _component_urn("Function", "api")
    events = [
        _pre_event(
            _resource_urn(rtype, f"r{i}", "Function"),
            rtype,
            op=OpType.CREATE,
            parent_urn=parent_urn,
        )
        for i, rtype in enumerate(["aws:iam/role:Role", "aws:lambda/function:Function"])
    ]

    assert rendered(events, operation="preview", compact=True) == dedent("""
        + Function api  (2 resources to create)

        ⠋ Analyzing differences  0/1 complete  0s
        """)


def test_compact_shows_replacement_warning():
    parent_urn = _component_urn("DynamoTable", "users")
    res_urn = _resource_urn("aws:dynamodb/table:Table", "users-table", "DynamoTable")
    events = [
        _pre_event(
            res_urn,
            "aws:dynamodb/table:Table",
            op=OpType.UPDATE,
            parent_urn=parent_urn,
            detailed_diff={"hashKey": _pdiff(DiffKind.UPDATE_REPLACE)},
            old_inputs={"hashKey": "pk"},
            new_inputs={"hashKey": "user_id"},
        ),
    ]

    # compact: warning bubbles to the component; the child line (DynamoDB Table) is hidden
    assert rendered(events, operation="preview", compact=True, width=160) == dedent("""
        ~ DynamoTable users  (1 resource to replace)
            !! Replacement recreates resource; data may be lost.

        ⠋ Analyzing differences  0/1 complete  0s
        """)


def test_compact_hides_data_loss_warning_for_non_data_replacement():
    parent_urn = _component_urn("Function", "api")
    res_urn = _resource_urn("aws:lambda/function:Function", "api-fn", "Function")
    events = [
        _pre_event(
            res_urn, "aws:lambda/function:Function", op=OpType.REPLACE, parent_urn=parent_urn
        ),
    ]

    assert rendered(events, operation="preview", compact=True) == dedent("""
        ± Function api  (1 resource to replace)

        ⠋ Analyzing differences  0/1 complete  0s
        """)


def test_compact_bubbles_nested_replacement_and_data_loss_to_outer_component():
    # A replacement in a NESTED sub-component must surface on the outer component: its count
    # includes the nested resource and its data-loss warning bubbles up. Pins all_resources
    # recursion (outer.preview_summary / outer.has_data_loss_replacement); single-level tests
    # never exercise it.
    outer = _component_urn("TopicSubscription", "users-events")
    inner = _component_urn("DynamoTable", "users")
    table = _resource_urn("aws:dynamodb/table:Table", "users-table", "DynamoTable")
    events = [
        _component_event("TopicSubscription", "users-events"),
        _component_event("DynamoTable", "users", outer),
        _pre_event(table, "aws:dynamodb/table:Table", op=OpType.REPLACE, parent_urn=inner),
    ]

    assert rendered(events, operation="preview", compact=True, width=160) == dedent("""
        ± TopicSubscription users-events  (1 resource to replace)
            !! Replacement recreates resource; data may be lost.

        ⠋ Analyzing differences  0/1 complete  0s
        """)


def test_preview_completion_hides_outputs():
    # preview completion never prints Outputs, even when given lines
    assert (
        completion([], operation="preview", output_lines=["", "[bold]Outputs:", "  a: 1"])
        == "✓ Analyzed in 0s\n"
    )


def test_preview_completion_reports_change_counts():
    # The preview completion frame prints per-operation counts (build_preview_counts_text),
    # ordered create/update/replace/delete and comma-joined, prefixed by the component count.
    # No other seam test drives a preview completion with resources, so this is its only pin.
    create = _resource_urn("aws:lambda/function:Function", "api-fn", "Function")
    update = _resource_urn("aws:lambda/function:Function", "cfg-fn", "Function")
    replace = _resource_urn("aws:dynamodb/table:Table", "users-table", "DynamoTable")
    delete = _resource_urn("aws:lambda/function:Function", "old-fn", "Function")
    events = [
        _pre_event(
            create,
            "aws:lambda/function:Function",
            op=OpType.CREATE,
            parent_urn=_component_urn("Function", "api"),
        ),
        _pre_event(
            update,
            "aws:lambda/function:Function",
            op=OpType.UPDATE,
            parent_urn=_component_urn("Function", "cfg"),
        ),
        _pre_event(
            replace,
            "aws:dynamodb/table:Table",
            op=OpType.REPLACE,
            parent_urn=_component_urn("DynamoTable", "users"),
        ),
        _pre_event(
            delete,
            "aws:lambda/function:Function",
            op=OpType.DELETE,
            parent_urn=_component_urn("Function", "old"),
        ),
    ]

    assert completion(events, operation="preview") == dedent("""\
        ✓ Analyzed in 0s
          4 components: 1 to create, 1 to update, 1 to replace, 1 to delete
        """)


def test_deploy_completion_omits_counts_for_noop():
    parent_urn = _component_urn("Function", "api")
    lambda_urn = _resource_urn("aws:lambda/function:Function", "api-fn", "Function")
    events = [
        _pre_event(
            lambda_urn, "aws:lambda/function:Function", op=OpType.SAME, parent_urn=parent_urn
        ),
        _outputs_event(lambda_urn, "aws:lambda/function:Function", op=OpType.SAME),
    ]

    # a no-op deploy (only SAME) shows the header alone — no component/resource counts
    assert completion(events) == "✓ Deployed in 0s\n"


def test_deploy_completion_counts_only_changed_components_and_resources():
    function_urn = _component_urn("Function", "api")
    queue_urn = _component_urn("Queue", "tasks")
    lambda_urn = _resource_urn("aws:lambda/function:Function", "api-fn", "Function")
    queue_res_urn = _resource_urn("aws:sqs/queue:Queue", "tasks-q", "Queue")
    unchanged_urn = _component_urn("Topic", "notifications")
    unchanged_res_urn = _resource_urn("aws:sns/topic:Topic", "notifications-topic", "Topic")
    events = [
        _pre_event(
            lambda_urn, "aws:lambda/function:Function", op=OpType.UPDATE, parent_urn=function_urn
        ),
        _outputs_event(lambda_urn, "aws:lambda/function:Function", op=OpType.UPDATE),
        _pre_event(queue_res_urn, "aws:sqs/queue:Queue", op=OpType.CREATE, parent_urn=queue_urn),
        _outputs_event(queue_res_urn, "aws:sqs/queue:Queue", op=OpType.CREATE),
        _pre_event(
            unchanged_res_urn, "aws:sns/topic:Topic", op=OpType.SAME, parent_urn=unchanged_urn
        ),
        _outputs_event(unchanged_res_urn, "aws:sns/topic:Topic", op=OpType.SAME),
    ]

    # the unchanged (SAME) component + resource are excluded from the counts
    assert completion(events) == dedent("""\
        ✓ Deployed in 0s
          2 components (2 resources) deployed
        """)


def test_deploy_completion_failure_appends_with_errors():
    comp = _component_urn("Function", "api")
    role = _resource_urn("aws:iam/role:Role", "api-role", "Function")
    events = [
        _pre_event(role, "aws:iam/role:Role", parent_urn=comp),
        _failed_event(role, "aws:iam/role:Role"),
        _summary_event(),
    ]

    # the suffix is space-separated from the time (regression: "in 0swith errors")
    assert completion(events) == dedent("""\
        ✗ Deployed in 0s with errors
          1 component (1 resource) deployed
        """)


def test_deploy_completion_prefers_preformatted_output_lines():
    # preformatted output_lines are used verbatim (markup rendered), outputs dict ignored
    assert completion([], output_lines=["", "[bold]Outputs:", "  custom line"]) == dedent("""\
        ✓ Deployed in 0s

        Outputs:
          custom line
        """)


def test_build_json_summary_for_deploy():
    comp_urn = _component_urn("Function", "api")
    res_urn = _resource_urn("aws:lambda/function:Function", "myapp-dev-api", "Function")
    events = [
        _pre_event(comp_urn, "stelvio:aws:Function", parent_urn=STACK_URN),
        _pre_event(
            res_urn,
            "aws:lambda/function:Function",
            parent_urn=comp_urn,
            new_inputs={"memory_size": 256},
        ),
        _outputs_event(res_urn, "aws:lambda/function:Function", parent_urn=comp_urn),
    ]

    payload = summary_json(events, outputs={"function_api_arn": "arn:aws:lambda:demo"})

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


def test_stream_emits_resource_and_warning_events_with_component_context():
    comp_urn = _component_urn("Function", "api")
    res_urn = _resource_urn("aws:lambda/function:Function", "myapp-dev-api", "Function")
    events = [
        _pre_event(comp_urn, "stelvio:aws:Function", parent_urn=STACK_URN, timestamp=1000),
        _pre_event(res_urn, "aws:lambda/function:Function", parent_urn=comp_urn, timestamp=1001),
        _outputs_event(
            res_urn, "aws:lambda/function:Function", parent_urn=comp_urn, timestamp=1002
        ),
        _outputs_event(comp_urn, "stelvio:aws:Function", parent_urn=STACK_URN, timestamp=1002),
        _diagnostic_event(
            "Node.js 18.x runtime is deprecated", res_urn, severity="warning", timestamp=1003
        ),
    ]

    # the component lifecycle itself emits nothing; a warning on a tracked resource
    # carries the resource type plus its owning component as context
    assert stream_events(events) == [
        {
            "event": "resource",
            "operation": "deploy",
            "app": "myapp",
            "env": "dev",
            "resource": {
                "name": "myapp-dev-api",
                "type": "aws:lambda/function:Function",
                "operation": "create",
            },
            "component": {"type": "Function", "name": "api"},
        },
        {
            "event": "warning",
            "operation": "deploy",
            "app": "myapp",
            "env": "dev",
            "message": "Node.js 18.x runtime is deprecated",
            "resource": "aws:lambda/function:Function",
            "component": "Function",
            "name": "api",
        },
    ]


def test_stream_does_not_emit_component_lifecycle_for_unchanged_component():
    comp_urn = _component_urn("Queue", "tasks")
    res_urn = _resource_urn("aws:sqs/queue:Queue", "myapp-dev-tasks", "Queue")
    events = [
        _pre_event(comp_urn, "stelvio:aws:Queue", parent_urn=STACK_URN, timestamp=1000),
        _pre_event(
            res_urn, "aws:sqs/queue:Queue", op=OpType.SAME, parent_urn=comp_urn, timestamp=1001
        ),
        _outputs_event(
            res_urn, "aws:sqs/queue:Queue", op=OpType.SAME, parent_urn=comp_urn, timestamp=1002
        ),
    ]

    assert stream_events(events) == []


def test_stream_deduplicates_repeated_resource_output_events():
    comp_urn = _component_urn("DynamoTable", "users")
    res_urn = _resource_urn("aws:dynamodb/table:Table", "myapp-dev-users", "Table")
    events = [
        _pre_event(comp_urn, "stelvio:aws:DynamoTable", parent_urn=STACK_URN, timestamp=1000),
        _pre_event(
            res_urn,
            "aws:dynamodb/table:Table",
            op=OpType.REPLACE,
            parent_urn=comp_urn,
            timestamp=1001,
        ),
        _outputs_event(
            res_urn,
            "aws:dynamodb/table:Table",
            op=OpType.REPLACE,
            parent_urn=comp_urn,
            timestamp=1002,
        ),
        _outputs_event(
            res_urn,
            "aws:dynamodb/table:Table",
            op=OpType.REPLACE,
            parent_urn=comp_urn,
            timestamp=1003,
        ),
    ]

    # only the first outputs event is emitted; its timestamp (1002, not 1003) proves it
    first_outputs_ts = datetime.fromtimestamp(1002, tz=UTC).astimezone().isoformat()
    assert stream_events(events, keep_timestamps=True) == [
        {
            "event": "resource",
            "operation": "deploy",
            "app": "myapp",
            "env": "dev",
            "timestamp": first_outputs_ts,
            "resource": {
                "name": "myapp-dev-users",
                "type": "aws:dynamodb/table:Table",
                "operation": "replace",
            },
            "component": {"type": "DynamoTable", "name": "users"},
        }
    ]


def test_build_json_summary_for_diff_includes_changes():
    comp_urn = _component_urn("Function", "api")
    res_urn = _resource_urn("aws:lambda/function:Function", "myapp-dev-api", "Function")
    diff_kwargs = {
        "detailed_diff": {
            "memory_size": _pdiff(DiffKind.UPDATE),
            "runtime": _pdiff(DiffKind.UPDATE_REPLACE),
        },
        "old_inputs": {"memory_size": 128, "runtime": "python3.11"},
        "new_inputs": {"memory_size": 256, "runtime": "python3.12"},
    }
    events = [
        _pre_event(comp_urn, "stelvio:aws:Function", op=OpType.UPDATE, parent_urn=STACK_URN),
        _pre_event(
            res_urn,
            "aws:lambda/function:Function",
            op=OpType.UPDATE,
            parent_urn=comp_urn,
            **diff_kwargs,
        ),
        _outputs_event(
            res_urn,
            "aws:lambda/function:Function",
            op=OpType.UPDATE,
            parent_urn=comp_urn,
            **diff_kwargs,
        ),
    ]

    payload = summary_json(events, operation="preview")

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


def test_build_json_summary_for_diff_resolves_indexed_change_values():
    comp_urn = _component_urn("Function", "api")
    res_urn = _resource_urn("aws:iam/policy:Policy", "myapp-dev-api-p", "Function")
    diff_kwargs = {
        "detailed_diff": {"policy.Statement[0].Resource": _pdiff(DiffKind.UPDATE)},
        "old_inputs": {"policy": {"Statement": [{"Resource": "arn:old"}]}},
        "new_inputs": {"policy": {"Statement": [{"Resource": "arn:new"}]}},
    }
    events = [
        _pre_event(comp_urn, "stelvio:aws:Function", op=OpType.UPDATE, parent_urn=STACK_URN),
        _pre_event(
            res_urn, "aws:iam/policy:Policy", op=OpType.UPDATE, parent_urn=comp_urn, **diff_kwargs
        ),
        _outputs_event(
            res_urn, "aws:iam/policy:Policy", op=OpType.UPDATE, parent_urn=comp_urn, **diff_kwargs
        ),
    ]

    assert summary_json(events, operation="preview")["components"] == [
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


def test_build_json_summary_for_refresh_uses_unchanged_for_no_drift():
    comp_urn = _component_urn("Function", "api")
    res_urn = _resource_urn("aws:lambda/function:Function", "myapp-dev-api", "Function")
    events = [
        _pre_event(comp_urn, "stelvio:aws:Function", op=OpType.REFRESH, parent_urn=STACK_URN),
        _pre_event(
            res_urn, "aws:lambda/function:Function", op=OpType.REFRESH, parent_urn=comp_urn
        ),
        _outputs_event(
            res_urn, "aws:lambda/function:Function", op=OpType.REFRESH, parent_urn=comp_urn
        ),
    ]

    payload = summary_json(events, operation="refresh")

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


def test_build_json_summary_for_refresh_reports_drift_updates():
    comp_urn = _component_urn("Function", "api")
    res_urn = _resource_urn("aws:lambda/function:Function", "myapp-dev-api", "Function")
    events = [
        _pre_event(comp_urn, "stelvio:aws:Function", op=OpType.REFRESH, parent_urn=STACK_URN),
        _pre_event(
            res_urn, "aws:lambda/function:Function", op=OpType.REFRESH, parent_urn=comp_urn
        ),
        _outputs_event(
            res_urn,
            "aws:lambda/function:Function",
            op=OpType.UPDATE,
            parent_urn=comp_urn,
            diffs=["memorySize"],
        ),
    ]

    assert summary_json(events, operation="refresh")["components"] == [
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


def test_build_json_summary_for_diff_includes_delete_operations():
    comp_urn = _component_urn("Queue", "tasks")
    res_urn = _resource_urn("aws:sqs/queue:Queue", "myapp-dev-tasks", "Queue")
    events = [
        _pre_event(comp_urn, "stelvio:aws:Queue", op=OpType.DELETE, parent_urn=STACK_URN),
        _pre_event(res_urn, "aws:sqs/queue:Queue", op=OpType.DELETE, parent_urn=comp_urn),
        _outputs_event(res_urn, "aws:sqs/queue:Queue", op=OpType.DELETE, parent_urn=comp_urn),
    ]

    payload = summary_json(events, operation="preview")

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


def test_build_json_summary_for_failed_deploy_includes_warnings_errors_and_orphans():
    orphan_urn = _resource_urn("aws:sqs/queue:Queue", "orphan-queue")
    events = [
        _diagnostic_event("Provider warning", severity="warning", timestamp=999),
        _diagnostic_event("queue failed", orphan_urn),
    ]

    payload = summary_json(
        events, status="failed", outputs={}, exit_code=1, message="Deploy failed"
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


def test_build_json_summary_uses_fallback_error_when_no_resource_errors():
    payload = summary_json([], status="failed", exit_code=1, fallback_error="boom")

    assert payload["status"] == "failed"
    assert payload["errors"] == [{"message": "boom"}]


def test_failed_preview_summary_omits_empty_discovered_components():
    users_comp_urn = _component_urn("DynamoTable", "users")
    users_res_urn = _resource_urn("aws:dynamodb/table:Table", "myapp-dev-users", "Table")
    queue_comp_urn = _component_urn("Queue", "tasks")
    events = [
        _pre_event(
            users_comp_urn, "stelvio:aws:DynamoTable", op=OpType.CREATE, parent_urn=STACK_URN
        ),
        _pre_event(
            users_res_urn, "aws:dynamodb/table:Table", op=OpType.CREATE, parent_urn=users_comp_urn
        ),
        _diagnostic_event(
            'all attributes must be indexed. Unused attributes: ["email"]', users_res_urn
        ),
        _pre_event(queue_comp_urn, "stelvio:aws:Queue", op=OpType.CREATE, parent_urn=STACK_URN),
    ]

    payload = summary_json(events, operation="preview", status="failed", exit_code=1)

    assert payload["components"] == [
        {
            "type": "DynamoTable",
            "name": "users",
            "operation": "create",
            "resources": [
                {
                    "name": "myapp-dev-users",
                    "type": "aws:dynamodb/table:Table",
                    "operation": "create",
                    "error": 'all attributes must be indexed. Unused attributes: ["email"]',
                }
            ],
            "error": 'all attributes must be indexed. Unused attributes: ["email"]',
        }
    ]


def test_summary_event_is_silent_when_live_disabled(monkeypatch):
    # live_enabled=False is the --json/--stream mode: printing NOTHING on the summary event
    # IS the behavior. Silence can't be asserted through the four output helpers (they'd
    # show an empty frame for many reasons), so this pins it below the seam: a mocked
    # console proves no print, and cleanup_status None proves no "Finalizing..." spinner.
    # An empty deploy is the scenario where live mode WOULD print ("Nothing to deploy")
    # and start the spinner — so the guard's removal is observable here.
    fake_console = Mock()
    monkeypatch.setattr("stelvio.rich_deployment_handler.Console", lambda: fake_console)

    handler = RichDeploymentHandler("myapp", "dev", "deploy", live_enabled=False)
    handler.handle_event(_summary_event())

    fake_console.print.assert_not_called()
    assert handler.cleanup_status is None


def test_warning_on_component_urn_shows_component_context():
    comp_urn = _component_urn("DynamoTable", "users")
    events = [
        _pre_event(comp_urn, "stelvio:aws:DynamoTable", parent_urn=STACK_URN),
        _outputs_event(comp_urn, "stelvio:aws:DynamoTable"),
        _diagnostic_event("Table billing mode changed", comp_urn, severity="warning"),
    ]

    # a warning aimed at the component itself gets "<Type> <name>:" context
    assert completion(events, width=160) == dedent("""\
        ✓ Deployed in 0s

        ⚠ 1 warning
          DynamoTable users:
            Table billing mode changed
        """)

    # the JSON surface carries the same context as structured fields
    assert summary_json(events)["warnings"] == [
        {"message": "Table billing mode changed", "component": "DynamoTable", "name": "users"}
    ]


def test_error_on_untracked_nested_urn_reports_leaf_resource_type_as_orphan():
    nested_urn = (
        f"urn:pulumi:{STACK}::{PROJECT}::"
        "stelvio:aws:DynamoTable$aws:dynamodb/table:Table::myapp-dev-users"
    )
    events = [_diagnostic_event("boom", nested_urn)]

    # the leaf type is extracted from the $-composed URN (not the stelvio parent segment)
    assert summary_json(events)["other_resources"] == [
        {
            "name": "myapp-dev-users",
            "type": "aws:dynamodb/table:Table",
            "operation": "create",
            "error": "boom",
        }
    ]


def test_untracked_failed_resource_attaches_to_component_matching_its_name():
    comp_urn = _component_urn("DynamoTable", "users")
    nested_urn = (
        f"urn:pulumi:{STACK}::{PROJECT}::"
        "stelvio:aws:DynamoTable$aws:dynamodb/table:Table::myapp-dev-users"
    )
    events = [
        _pre_event(comp_urn, "stelvio:aws:DynamoTable", parent_urn=STACK_URN),
        _diagnostic_event(
            'all attributes must be indexed. Unused attributes: ["email"]', nested_urn
        ),
    ]

    payload = summary_json(events)

    # the failed resource lands under the same-named component, not in other_resources
    assert payload["components"] == [
        {
            "type": "DynamoTable",
            "name": "users",
            "operation": "create",
            "resources": [
                {
                    "name": "myapp-dev-users",
                    "type": "aws:dynamodb/table:Table",
                    "operation": "create",
                    "error": 'all attributes must be indexed. Unused attributes: ["email"]',
                }
            ],
            "error": 'all attributes must be indexed. Unused attributes: ["email"]',
        }
    ]
    assert payload["errors"] == [
        {
            "resource": "aws:dynamodb/table:Table",
            "message": 'all attributes must be indexed. Unused attributes: ["email"]',
            "component": "DynamoTable",
            "name": "users",
        }
    ]
    assert "other_resources" not in payload


def test_untracked_failed_resource_attaches_to_component_by_name_prefix():
    comp_urn = _component_urn("Function", "api")
    role_urn = (
        f"urn:pulumi:{STACK}::{PROJECT}::stelvio:aws:Function$aws:iam/role:Role::myapp-dev-api-r"
    )
    events = [
        _pre_event(comp_urn, "stelvio:aws:Function", parent_urn=STACK_URN),
        _diagnostic_event("role creation failed", role_urn),
    ]

    payload = summary_json(events)

    # "api-r" attaches to component "api" via the "api-" name prefix
    assert payload["components"] == [
        {
            "type": "Function",
            "name": "api",
            "operation": "create",
            "resources": [
                {
                    "name": "myapp-dev-api-r",
                    "type": "aws:iam/role:Role",
                    "operation": "create",
                    "error": "role creation failed",
                }
            ],
            "error": "role creation failed",
        }
    ]
    assert payload["errors"] == [
        {
            "resource": "aws:iam/role:Role",
            "message": "role creation failed",
            "component": "Function",
            "name": "api",
        }
    ]
    assert "other_resources" not in payload


def test_diagnostic_untracked_resource_without_component_shows_as_orphan():
    resource_urn = _resource_urn("aws:dynamodb/table:Table", "standalone-users")
    events = [
        _diagnostic_event(
            "sdk-v2/provider2.go:572: sdk.helper_schema: "
            'all attributes must be indexed. Unused attributes: ["email"]',
            resource_urn,
        )
    ]

    assert summary_json(events)["other_resources"] == [
        {
            "name": "standalone-users",
            "type": "aws:dynamodb/table:Table",
            "operation": "create",
            "error": 'all attributes must be indexed. Unused attributes: ["email"]',
        }
    ]

    assert rendered(events) == dedent("""
        Other resources
          ✗ DynamoDB Table (0.0s)
                all attributes must be indexed. Unused attributes: ["email"]

        ⠋ Deploying  1/1 complete  0s
        """)


def test_error_diagnostic_reduces_to_its_actionable_bullet():
    resource_urn = _resource_urn("aws:dynamodb/table:Table", "standalone-users")
    events = [
        _diagnostic_event(
            "diffing urn:pulumi:dev::myapp::aws:dynamodb/table:Table::users: "
            "1 error occurred:\n\t* all attributes must be indexed. "
            'Unused attributes: ["email"]',
            resource_urn,
        )
    ]

    # only the "* ..." bullet survives — the diffing-urn preamble is noise
    assert summary_json(events)["other_resources"] == [
        {
            "name": "standalone-users",
            "type": "aws:dynamodb/table:Table",
            "operation": "create",
            "error": 'all attributes must be indexed. Unused attributes: ["email"]',
        }
    ]


def test_preview_render_shows_resource_error_inline():
    parent_urn = _component_urn("Function", "api")
    res_urn = _resource_urn("aws:lambda/function:Function", "api-fn", "Function")
    events = [
        _pre_event(
            res_urn, "aws:lambda/function:Function", op=OpType.UPDATE, parent_urn=parent_urn
        ),
        _diagnostic_event("Invalid runtime", res_urn, timestamp=1001),
    ]

    assert rendered(events, operation="preview") == dedent("""
        ✗ Function api  (1 to update)
            ✗ Lambda Function
                Invalid runtime

        ⠋ Analyzing differences  1/1 complete  0s
        """)


def test_failed_component_summary_shows_all_children_for_context():
    parent_urn = _component_urn("Function", "api")
    role_urn = _resource_urn("aws:iam/role:Role", "api-role", "Function")
    lambda_urn = _resource_urn("aws:lambda/function:Function", "api-fn", "Function")
    events = [
        _pre_event(role_urn, "aws:iam/role:Role", parent_urn=parent_urn),
        _pre_event(lambda_urn, "aws:lambda/function:Function", parent_urn=parent_urn),
        _outputs_event(role_urn, "aws:iam/role:Role"),
        _diagnostic_event("Invalid runtime", lambda_urn, timestamp=1002),
    ]

    # a failed child expands the whole component tree (all children shown for context)
    assert rendered(events, width=160, now=1000) == dedent("""
        ✗ Function api
            ✓ IAM Role (1.0s)
            ✗ Lambda Function (2.0s)
                Invalid runtime

        ⠋ Deploying  1/1 complete  0s
        """)


def test_warning_diagnostic_displayed_in_completion_with_context():
    parent_urn = _component_urn("Function", "api")
    lambda_urn = _resource_urn("aws:lambda/function:Function", "api-fn", "Function")
    events = [
        _pre_event(lambda_urn, "aws:lambda/function:Function", parent_urn=parent_urn),
        _outputs_event(lambda_urn, "aws:lambda/function:Function"),
        _diagnostic_event(
            "Node.js 18.x runtime is deprecated", lambda_urn, severity="warning", timestamp=1002
        ),
    ]

    # a warning doesn't fail the deploy; it's surfaced with component→resource context
    assert completion(events, width=160) == dedent("""\
        ✓ Deployed in 0s
          1 component (1 resource) deployed

        ⚠ 1 warning
          Function api → api-fn (Lambda Function):
            Node.js 18.x runtime is deprecated
        """)


def test_duplicate_warning_diagnostics_are_deduplicated():
    parent_urn = _component_urn("Function", "api")
    lambda_urn = _resource_urn("aws:lambda/function:Function", "api-fn", "Function")
    warning = "Node.js 18.x runtime is deprecated"
    events = [
        _pre_event(lambda_urn, "aws:lambda/function:Function", parent_urn=parent_urn),
        _outputs_event(lambda_urn, "aws:lambda/function:Function"),
        _diagnostic_event(warning, lambda_urn, severity="warning", timestamp=1002),
        _diagnostic_event(warning, lambda_urn, severity="warning", timestamp=1003),
    ]

    # the identical warning, emitted twice, is shown once
    assert completion(events, width=160) == dedent("""\
        ✓ Deployed in 0s
          1 component (1 resource) deployed

        ⚠ 1 warning
          Function api → api-fn (Lambda Function):
            Node.js 18.x runtime is deprecated
        """)


def test_interrupted_create_warning_is_user_friendly_and_actionable():
    events = [
        _diagnostic_event(
            "urn:pulumi:dev::myapp::aws:iam/role:Role::myapp-dev-test-fn-d-r, "
            "interrupted while creating",
            severity="warning",
        )
    ]

    # the raw pending-create urn becomes a friendly, actionable hint (no raw urn shown)
    assert completion(events, width=160) == dedent("""\
        ✓ Deployed in 0s

        ⚠ 1 warning
          test-fn-d-r (IAM Role):
            A previous deploy appears to have been interrupted while creating this resource.
            Hint: Run `stlv state repair` to clear stale pending operations.
        """)


# ---------------------------------------------------------------------------
# API Gateway internal resource filtering — JSON and stream
# ---------------------------------------------------------------------------


def _apigw_internal_pre_events(op: OpType = OpType.CREATE) -> list[EngineEvent]:
    """Account.get() ref (always-hidden READ), managed Account, and managed Role."""
    return [
        _pre_event(APIGW_ACCOUNT_REF_URN, "aws:apigateway/account:Account", op=OpType.READ),
        _pre_event(APIGW_ACCOUNT_URN, "aws:apigateway/account:Account", op=op),
        _pre_event(APIGW_ROLE_URN, "aws:iam/role:Role", op=op),
    ]


def _apigw_internal_outputs_events(op: OpType = OpType.CREATE) -> list[EngineEvent]:
    """Completion (outputs) events matching ``_apigw_internal_pre_events``."""
    return [
        _outputs_event(APIGW_ACCOUNT_REF_URN, "aws:apigateway/account:Account", op=OpType.READ),
        _outputs_event(APIGW_ACCOUNT_URN, "aws:apigateway/account:Account", op=op),
        _outputs_event(APIGW_ROLE_URN, "aws:iam/role:Role", op=op),
    ]


# ===========================================================================
# JSON summary: internal resource filtering
# ===========================================================================
# JSON summary counts and other_resources must filter internal resources.


_ZERO_DEPLOY_SUMMARY = {
    "created": 0,
    "updated": 0,
    "deleted": 0,
    "replaced": 0,
    "failed": 0,
    "unchanged": 0,
}
_ZERO_PREVIEW_SUMMARY = {"to_create": 0, "to_update": 0, "to_delete": 0, "to_replace": 0}


@mark.parametrize(
    ("command", "op", "expected_summary"),
    [
        # 2nd deploy: Account/Role DELETE hidden — must not leak into ANY count
        param("deploy", OpType.DELETE, _ZERO_DEPLOY_SUMMARY, id="deploy-hides-delete"),
        # 1st deploy: CREATE counted (Account ref READ is always hidden)
        param(
            "deploy",
            OpType.CREATE,
            {**_ZERO_DEPLOY_SUMMARY, "created": 2},
            id="deploy-counts-create",
        ),
        # destroy: DELETE visible and counted
        param(
            "destroy",
            OpType.DELETE,
            {**_ZERO_DEPLOY_SUMMARY, "deleted": 2},
            id="destroy-counts-delete",
        ),
        # diff (preview): DELETE hidden — must not leak into ANY count
        param("preview", OpType.DELETE, _ZERO_PREVIEW_SUMMARY, id="diff-hides-delete"),
        # diff (preview): CREATE counted
        param(
            "preview",
            OpType.CREATE,
            {**_ZERO_PREVIEW_SUMMARY, "to_create": 2},
            id="diff-counts-create",
        ),
    ],
)
def test_json_summary_counts_for_apigw_internals(command, op, expected_summary):
    events = _apigw_internal_pre_events(op)
    if command != "preview":
        events += _apigw_internal_outputs_events(op)

    assert summary_json(events, operation=command)["summary"] == expected_summary


def test_json_other_resources_excludes_hidden_delete():
    """2nd deploy: Account/Role DELETE should not appear in other_resources."""
    events = [
        *_apigw_internal_pre_events(OpType.DELETE),
        *_apigw_internal_outputs_events(OpType.DELETE),
    ]

    assert "other_resources" not in summary_json(events)


def test_json_other_resources_includes_created():
    """1st deploy: managed Account/Role CREATE appear; the READ ref never does."""
    events = [*_apigw_internal_pre_events(), *_apigw_internal_outputs_events()]

    assert summary_json(events)["other_resources"] == [
        {
            "name": "api-gateway-account",
            "type": "aws:apigateway/account:Account",
            "operation": "create",
        },
        {
            "name": "StelvioAPIGatewayPushToCloudWatchLogsRole",
            "type": "aws:iam/role:Role",
            "operation": "create",
        },
    ]


def test_json_other_resources_account_ref_always_excluded():
    """Account.get() read reference never appears in JSON other_resources."""
    events = [_pre_event(APIGW_ACCOUNT_REF_URN, "aws:apigateway/account:Account", op=OpType.READ)]

    assert "other_resources" not in summary_json(events)


# ===========================================================================
# Stream events: internal resource filtering
# ===========================================================================
# Stream events must not emit hidden internal resources.


def test_stream_excludes_account_ref_read():
    """Account.get() read reference should never produce a stream resource event."""
    events = [
        _pre_event(APIGW_ACCOUNT_REF_URN, "aws:apigateway/account:Account", op=OpType.READ),
        _outputs_event(APIGW_ACCOUNT_REF_URN, "aws:apigateway/account:Account", op=OpType.READ),
    ]

    assert stream_events(events) == []


@mark.parametrize(
    ("command", "op", "emitted_op"),
    [
        # 2nd deploy: Account/Role DELETE should not emit stream resource events
        param("deploy", OpType.DELETE, None, id="deploy-delete-hidden"),
        # 1st deploy: Account/Role CREATE should emit stream resource events
        param("deploy", OpType.CREATE, "create", id="deploy-create-emitted"),
        # destroy: Account/Role DELETE should emit stream resource events
        param("destroy", OpType.DELETE, "delete", id="destroy-delete-emitted"),
    ],
)
def test_stream_apigw_managed_resource_events(command, op, emitted_op):
    events = [*_apigw_internal_pre_events(op), *_apigw_internal_outputs_events(op)]

    streamed = stream_events(events, operation=command)

    if emitted_op is None:
        assert streamed == []
    else:
        base = {"event": "resource", "operation": command, "app": "myapp", "env": "dev"}
        assert streamed == [
            {
                **base,
                "resource": {
                    "name": "api-gateway-account",
                    "type": "aws:apigateway/account:Account",
                    "operation": emitted_op,
                },
            },
            {
                **base,
                "resource": {
                    "name": "StelvioAPIGatewayPushToCloudWatchLogsRole",
                    "type": "aws:iam/role:Role",
                    "operation": emitted_op,
                },
            },
        ]
