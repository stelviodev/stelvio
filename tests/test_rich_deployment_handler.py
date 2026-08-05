"""Behavioral tests for RichDeploymentHandler: events in, the four public outputs out.

Every test feeds Pulumi engine events through ``handle_event`` and asserts on what the
user sees — the rendered terminal frame, the ``--json`` payload, the ``--stream`` events,
or the completion frame. A test earns its keep only if breaking the behavior it pins
turns it red — exact output equality, no substring checks.
"""

import itertools
import sys
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from textwrap import dedent
from unittest.mock import Mock, call, patch

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
from stelvio.rich_deployment_model import (
    UNKNOWN_OUTPUT_SENTINEL,
    _parse_stelvio_parent,
    get_total_duration,
)

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
    urn: str,
    resource_type: str,
    parent_urn: str = "",
    inputs: dict | None = None,
    outputs: dict | None = None,
) -> StepEventStateMetadata:
    return StepEventStateMetadata(
        type=resource_type,
        urn=urn,
        id="some-id",
        parent=parent_urn,
        provider="urn:pulumi:dev::myapp::pulumi:providers:aws::default",
        inputs=inputs,
        outputs=outputs,
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
    old_outputs: dict | None = None,
    new_outputs: dict | None = None,
) -> StepEventMetadata:
    return StepEventMetadata(
        op=op,
        urn=urn,
        type=resource_type,
        provider="urn:pulumi:dev::myapp::pulumi:providers:aws::default",
        new=_make_state(urn, resource_type, parent_urn, inputs=new_inputs, outputs=new_outputs),
        old=_make_state(urn, resource_type, parent_urn, inputs=old_inputs, outputs=old_outputs)
        if old_inputs or old_outputs
        else None,
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
    old_outputs: dict | None = None,
    new_outputs: dict | None = None,
) -> EngineEvent:
    metadata = _step_metadata(
        urn,
        resource_type,
        op,
        parent_urn,
        diffs,
        detailed_diff,
        old_inputs,
        new_inputs,
        old_outputs,
        new_outputs,
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
    old_outputs: dict | None = None,
    new_outputs: dict | None = None,
) -> EngineEvent:
    metadata = _step_metadata(
        urn,
        resource_type,
        op,
        parent_urn,
        diffs,
        detailed_diff,
        old_inputs,
        new_inputs,
        old_outputs,
        new_outputs,
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


def _destroy_step_metadata(urn: str, resource_type: str, parent_urn: str) -> StepEventMetadata:
    """Real destroy shape: ``new`` is None — the parent rides on ``old`` (input fidelity)."""
    return StepEventMetadata(
        op=OpType.DELETE,
        urn=urn,
        type=resource_type,
        provider="urn:pulumi:dev::myapp::pulumi:providers:aws::default",
        new=None,
        old=_make_state(urn, resource_type, parent_urn),
    )


def _destroy_pre_event(
    urn: str, resource_type: str, parent_urn: str = "", timestamp: int = 1000
) -> EngineEvent:
    return EngineEvent(
        sequence=_next_seq(),
        timestamp=timestamp,
        resource_pre_event=ResourcePreEvent(
            metadata=_destroy_step_metadata(urn, resource_type, parent_urn)
        ),
    )


def _destroy_outputs_event(
    urn: str, resource_type: str, parent_urn: str = "", timestamp: int = 1001
) -> EngineEvent:
    return EngineEvent(
        sequence=_next_seq(),
        timestamp=timestamp,
        res_outputs_event=ResOutputsEvent(
            metadata=_destroy_step_metadata(urn, resource_type, parent_urn)
        ),
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


def _markup(segments) -> str:
    """Project rendered segments back to Rich markup: ``[red]✗ [/red]``."""
    return "".join(f"[{s.style}]{s.text}[/{s.style}]" if s.style else s.text for s in segments)


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
    return _markup(segments)


def completion(
    events: list[EngineEvent],
    *,
    output_lines: list[str] | None = None,
    width: int = DEFAULT_WIDTH,
    duration: tuple[int, int] = (0, 0),
    **handler_kwargs,
) -> str:
    """Return the ``show_completion`` frame (final ``✓ …`` line + counts) as plain text.

    ``duration`` is the frozen (minutes, seconds) total the header prints.
    """
    handler = build_handler(events, **handler_kwargs)
    handler.console = Console(record=True, width=width, no_color=True)
    with patch(_DURATION_TARGET, return_value=duration):
        handler.show_completion(output_lines=output_lines)
    return handler.console.export_text()


def styled_completion(
    events: list[EngineEvent],
    *,
    width: int = DEFAULT_WIDTH,
    duration: tuple[int, int] = (0, 0),
    **handler_kwargs,
) -> str:
    """The colour twin of ``completion``: the completion frame with Rich markup woven inline.

    ``show_completion`` prints (it is not a renderable), so the styled twin reads the console's
    recorded segments — the only representation that keeps *style names*. Rich's public
    ``export_text(styles=True)`` flattens them to ANSI, where ``green`` comes back as
    ``color(2)`` and the assert stops being readable.
    """
    handler = build_handler(events, **handler_kwargs)
    console = Console(record=True, width=width)
    handler.console = console
    with patch(_DURATION_TARGET, return_value=duration):
        handler.show_completion(output_lines=None)
    return _markup(console._record_buffer)


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
    assert rendered(events) == "\n✓ Function api  (1.0s)\n\n"


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
    assert rendered(events, operation="preview") == dedent("""
        ~ Function api  (1 to update)
            ~ Lambda Function

        """)


def test_completion_frame_reports_component_and_resource_counts():
    assert completion(_create_function_events()) == dedent("""\
        ✓ Deployed in 0s
          1 component (2 resources) deployed
        """)


def test_completion_frame_emphasises_the_component_count():
    # Only the component count is bold — the number the eye should land on first.
    assert styled_completion(_create_function_events()) == (
        "✓ Deployed in 0s\n  [bold]1[/bold] component (2 resources) deployed\n"
    )


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
        _diagnostic_event("EntityAlreadyExists: role api-role already exists", urn=role),
        _failed_event(role, "aws:iam/role:Role"),
        _summary_event(),
    ]
    # The failure reason carries the alarm colour too — it is not dimmed detail text.
    assert styled(events) == dedent("""
        [red]✗ [/red][bold]Function[/bold] api
            [red]✗ [/red]IAM Role[dim] (1.0s)[/dim]
                [red]EntityAlreadyExists: role api-role already exists[/red]

        """)


def test_replacement_warning_styling():
    # The data-loss warning must carry its own alarm colour, not inherit the line style.
    parent_urn = _component_urn("DynamoTable", "users")
    res_urn = _resource_urn("aws:dynamodb/table:Table", "users-table", "DynamoTable")
    events = [
        _pre_event(res_urn, "aws:dynamodb/table:Table", op=OpType.REPLACE, parent_urn=parent_urn),
        _summary_event(),
    ]
    assert styled(events, operation="preview") == dedent("""
        [blue]± [/blue][bold]DynamoTable[/bold] users[dim]  (1 to replace)[/dim]
            [blue]± [/blue]DynamoDB Table
                [bold red]!! Replacement recreates resource; data may be lost.[/bold red]

        """)


def test_refresh_drift_frame_styling():
    # What drifted is dim detail hanging off the resource line, not a signal of its own.
    events = [
        _pre_event(
            _API_LAMBDA,
            "aws:lambda/function:Function",
            op=OpType.UPDATE,
            parent_urn=_API_FUNC,
            diffs=["memorySize"],
        ),
        _outputs_event(
            _API_LAMBDA,
            "aws:lambda/function:Function",
            op=OpType.UPDATE,
            parent_urn=_API_FUNC,
            diffs=["memorySize"],
        ),
        _summary_event(),
    ]
    assert styled(events, operation="refresh") == dedent("""
        [yellow]✓ [/yellow][bold]Function[/bold] api
            [yellow]✓ [/yellow]Lambda Function[dim] (memorySize changed)[/dim]

        """)


# ===========================================================================
# The operation display table
# ---------------------------------------------------------------------------
# Glyph + colour is the CLI's whole visual vocabulary for "what is happening to
# this resource", and it is a table: three modes (preview / in-flight / finished)
# by one row per Pulumi operation. One test per mode, one row per operation, so a
# cell can never change — or lose its colour — unnoticed.
#
# ``show_unchanged=True`` throughout: read/refresh/unchanged components collapse
# into the unchanged bucket, and without it those rows would render nothing.
# The last row of each table is an operation NO map covers (DELETE_REPLACED, which
# real replacement deploys emit) — it must land on the neutral fallback.
# ===========================================================================
def _single_child_events(op: OpType, *, completed: bool) -> list[EngineEvent]:
    """One Function component with one Lambda child being `op`-ed."""
    lambda_type = "aws:lambda/function:Function"
    events = [_pre_event(_API_LAMBDA, lambda_type, op=op, parent_urn=_API_FUNC)]
    if completed:
        events += [
            _outputs_event(_API_LAMBDA, lambda_type, op=op, parent_urn=_API_FUNC),
            _summary_event(),
        ]
    return events


_DISPLAY_TABLE_IDS = [
    "create",
    "update",
    "delete",
    "discard",
    "replace",
    "swap",
    "refresh",
    "read",
    "unchanged",
    "unmapped",
]


@mark.parametrize(
    ("op", "frame"),
    [
        (
            OpType.CREATE,
            dedent("""
            [green]+ [/green][bold]Function[/bold] api[dim]  (1 to create)[/dim]
                [green]+ [/green]Lambda Function

            """),
        ),
        (
            OpType.UPDATE,
            dedent("""
            [yellow]~ [/yellow][bold]Function[/bold] api[dim]  (1 to update)[/dim]
                [yellow]~ [/yellow]Lambda Function

            """),
        ),
        (
            OpType.DELETE,
            dedent("""
            [red]- [/red][bold]Function[/bold] api[dim]  (1 to delete)[/dim]
                [red]- [/red]Lambda Function

            """),
        ),
        (
            OpType.DISCARD,
            dedent("""
            [red]- [/red][bold]Function[/bold] api[dim]  (1 to change)[/dim]
                [red]- [/red]Lambda Function

            """),
        ),
        (
            OpType.REPLACE,
            dedent("""
            [blue]± [/blue][bold]Function[/bold] api[dim]  (1 to replace)[/dim]
                [blue]± [/blue]Lambda Function

            """),
        ),
        (
            OpType.CREATE_REPLACEMENT,
            dedent("""
            [blue]± [/blue][bold]Function[/bold] api[dim]  (1 to replace)[/dim]
                [blue]± [/blue]Lambda Function

            """),
        ),
        # Refresh/read/unchanged components collapse to a header — no child line.
        (
            OpType.REFRESH,
            "\n[sea_green3]~ [/sea_green3][bold]Function[/bold] api[dim]  (1 to change)[/dim]\n\n",
        ),
        (
            OpType.READ,
            "\n[sea_green3]~ [/sea_green3][bold]Function[/bold] api[dim]  (1 to change)[/dim]\n\n",
        ),
        # Nothing to preview: no count suffix at all (the empty-summary guard).
        (OpType.SAME, "\n[dim]~ [/dim][bold]Function[/bold] api\n\n"),
        (
            OpType.DELETE_REPLACED,
            dedent("""
            [yellow]| [/yellow][bold]Function[/bold] api[dim]  (1 to change)[/dim]
                [yellow]| [/yellow]Lambda Function

            """),
        ),
    ],
    ids=_DISPLAY_TABLE_IDS,
)
def test_preview_frame_glyph_and_colour_per_operation(op, frame):
    events = _single_child_events(op, completed=True)

    assert styled(events, operation="preview", show_unchanged=True) == frame


@mark.parametrize(
    ("op", "frame"),
    [
        (
            OpType.CREATE,
            dedent("""
            [green]| [/green][bold]Function[/bold] api
                [green]| [/green]Lambda Function[dim] (2.0s)[/dim]

            [cyan]⠋[/cyan] Deploying  0/1 complete  0s
            """),
        ),
        (
            OpType.UPDATE,
            dedent("""
            [yellow]| [/yellow][bold]Function[/bold] api
                [yellow]| [/yellow]Lambda Function[dim] (2.0s)[/dim]

            [cyan]⠋[/cyan] Deploying  0/1 complete  0s
            """),
        ),
        (
            OpType.DELETE,
            dedent("""
            [red]| [/red][bold]Function[/bold] api
                [red]| [/red]Lambda Function[dim] (2.0s)[/dim]

            [cyan]⠋[/cyan] Deploying  0/1 complete  0s
            """),
        ),
        (
            OpType.DISCARD,
            dedent("""
            [red]| [/red][bold]Function[/bold] api
                [red]| [/red]Lambda Function[dim] (2.0s)[/dim]

            [cyan]⠋[/cyan] Deploying  0/1 complete  0s
            """),
        ),
        (
            OpType.REPLACE,
            dedent("""
            [blue]| [/blue][bold]Function[/bold] api
                [blue]| [/blue]Lambda Function[dim] (2.0s)[/dim]

            [cyan]⠋[/cyan] Deploying  0/1 complete  0s
            """),
        ),
        (
            OpType.CREATE_REPLACEMENT,
            dedent("""
            [blue]| [/blue][bold]Function[/bold] api
                [blue]| [/blue]Lambda Function[dim] (2.0s)[/dim]

            [cyan]⠋[/cyan] Deploying  0/1 complete  0s
            """),
        ),
        (
            OpType.REFRESH,
            dedent("""
            [sea_green3]| [/sea_green3][bold]Function[/bold] api[dim]  (2.0s)[/dim]

            [cyan]⠋[/cyan] Deploying  0/1 complete  0s
            """),
        ),
        (
            OpType.READ,
            dedent("""
            [sea_green3]| [/sea_green3][bold]Function[/bold] api[dim]  (2.0s)[/dim]

            [cyan]⠋[/cyan] Deploying  0/1 complete  0s
            """),
        ),
        (
            OpType.SAME,
            dedent("""
            [dim]~ [/dim][bold]Function[/bold] api[dim]  (2.0s)[/dim]

            [cyan]⠋[/cyan] Deploying  0/1 complete  0s
            """),
        ),
        (
            OpType.DELETE_REPLACED,
            dedent("""
            [yellow]| [/yellow][bold]Function[/bold] api
                [yellow]| [/yellow]Lambda Function[dim] (2.0s)[/dim]

            [cyan]⠋[/cyan] Deploying  0/1 complete  0s
            """),
        ),
    ],
    ids=_DISPLAY_TABLE_IDS,
)
def test_in_flight_frame_glyph_and_colour_per_operation(op, frame):
    # In flight: the resource started at 1000 and the frozen clock reads 1002 — hence (2.0s).
    events = _single_child_events(op, completed=False)

    assert styled(events, now=1002.0, show_unchanged=True) == frame


@mark.parametrize(
    ("op", "frame"),
    [
        (OpType.CREATE, "\n[green]✓ [/green][bold]Function[/bold] api[dim]  (1.0s)[/dim]\n\n"),
        (OpType.UPDATE, "\n[yellow]✓ [/yellow][bold]Function[/bold] api[dim]  (1.0s)[/dim]\n\n"),
        (OpType.DELETE, "\n[red]✓ [/red][bold]Function[/bold] api[dim]  (1.0s)[/dim]\n\n"),
        (OpType.DISCARD, "\n[red]✓ [/red][bold]Function[/bold] api[dim]  (1.0s)[/dim]\n\n"),
        (OpType.REPLACE, "\n[blue]✓ [/blue][bold]Function[/bold] api[dim]  (1.0s)[/dim]\n\n"),
        (
            OpType.CREATE_REPLACEMENT,
            "\n[blue]✓ [/blue][bold]Function[/bold] api[dim]  (1.0s)[/dim]\n\n",
        ),
        (
            OpType.REFRESH,
            "\n[sea_green3]✓ [/sea_green3][bold]Function[/bold] api[dim]  (1.0s)[/dim]\n\n",
        ),
        (
            OpType.READ,
            "\n[sea_green3]✓ [/sea_green3][bold]Function[/bold] api[dim]  (1.0s)[/dim]\n\n",
        ),
        (OpType.SAME, "\n[dim]~ [/dim][bold]Function[/bold] api[dim]  (1.0s)[/dim]\n\n"),
        (
            OpType.DELETE_REPLACED,
            "\n[yellow]| [/yellow][bold]Function[/bold] api[dim]  (1.0s)[/dim]\n\n",
        ),
    ],
    ids=_DISPLAY_TABLE_IDS,
)
def test_finished_frame_glyph_and_colour_per_operation(op, frame):
    # Finished components collapse to their header line, so the glyph is all the user gets.
    events = _single_child_events(op, completed=True)

    assert styled(events, show_unchanged=True) == frame


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


def test_get_total_duration_splits_elapsed_into_minutes_and_seconds():
    # Below-seam KEEP (earned exception, wall-clock class like the duration carve-outs):
    # every output helper freezes get_total_duration itself for determinism, so the
    # minutes/seconds split inside it has no seam path — pin the pure derivation here.
    start = datetime(2026, 1, 1, 12, 0, 0)
    with patch("stelvio.rich_deployment_model.datetime") as frozen:
        frozen.now.return_value = datetime(2026, 1, 1, 12, 2, 5)
        assert get_total_duration(start) == (2, 5)


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
    assert rendered(events) == "\n✓ Function api  (5.0s)\n\n"


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
    """A bare ComponentResource event registers the component but adds no resource — and
    with no children there is no operation to derive, so it reports skipped."""
    payload = summary_json([_pre_event(_component_urn("Function", "api"), "stelvio:aws:Function")])
    assert payload["components"] == [
        {"type": "Function", "name": "api", "operation": "skipped", "resources": []}
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


def test_component_operation_prefers_delete_over_update():
    # delete sits above create/update in the priority table: a component losing a
    # resource while another updates reports "delete", not the lower-priority op
    fn = _component_urn("Function", "api")
    role = _resource_urn("aws:iam/role:Role", "api-role", "Function")
    lam = _resource_urn("aws:lambda/function:Function", "api-fn", "Function")
    events = [
        _pre_event(role, "aws:iam/role:Role", op=OpType.DELETE, parent_urn=fn),
        _pre_event(lam, "aws:lambda/function:Function", op=OpType.UPDATE, parent_urn=fn),
        _outputs_event(role, "aws:iam/role:Role", op=OpType.DELETE),
        _outputs_event(lam, "aws:lambda/function:Function", op=OpType.UPDATE),
    ]
    assert summary_json(events)["components"] == [
        {
            "type": "Function",
            "name": "api",
            "operation": "delete",
            "resources": [
                {"name": "api-role", "type": "aws:iam/role:Role", "operation": "delete"},
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


def test_component_error_reports_first_failed_childs_error():
    # with several failed children the component surfaces the FIRST child's error —
    # the one that failed first is usually the root cause, later ones are fallout
    fn = _component_urn("Function", "api")
    role = _resource_urn("aws:iam/role:Role", "api-role", "Function")
    lam = _resource_urn("aws:lambda/function:Function", "api-fn", "Function")
    events = [
        _pre_event(role, "aws:iam/role:Role", parent_urn=fn),
        _pre_event(lam, "aws:lambda/function:Function", parent_urn=fn),
        _failed_event(role, "aws:iam/role:Role"),
        _failed_event(lam, "aws:lambda/function:Function"),
        _diagnostic_event("role exploded", role, timestamp=1001),
        _diagnostic_event("lambda exploded", lam, timestamp=1002),
    ]
    assert summary_json(events, status="failed", exit_code=1)["components"] == [
        {
            "type": "Function",
            "name": "api",
            "operation": "create",
            "resources": [
                {
                    "name": "api-role",
                    "type": "aws:iam/role:Role",
                    "operation": "create",
                    "error": "role exploded",
                },
                {
                    "name": "api-fn",
                    "type": "aws:lambda/function:Function",
                    "operation": "create",
                    "error": "lambda exploded",
                },
            ],
            "error": "role exploded",
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
    assert rendered(done) == "\n✓ Function api  (1.0s)\n\n"


def test_component_fails_when_a_child_fails():
    comp = _component_urn("Function", "api")
    role = _resource_urn("aws:iam/role:Role", "api-role", "Function")
    events = [
        _pre_event(role, "aws:iam/role:Role", parent_urn=comp),
        _failed_event(role, "aws:iam/role:Role"),
        _summary_event(),
    ]
    assert rendered(events) == dedent("""
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
    assert rendered(done) == "\n✓ TopicSubscription outer  (2.0s)\n\n"


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
    assert rendered(events) == dedent("""
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


def test_childless_component_placeholder_renders_active_with_show_unchanged():
    # component events can arrive before any child resource; with --show-unchanged the
    # placeholder shows as still ACTIVE (| glyph, not counted complete in the footer)
    events = [_component_event("Bucket", "assets")]

    assert rendered(events, show_unchanged=True, now=1000) == dedent("""
        | Bucket assets  (0.0s)

        ⠋ Deploying  0/1 complete  0s
        """)


def test_destroy_active_frame_shows_destroying_footer():
    # the only render pin for destroy mode: tree + the "Destroying" footer verb
    fn = _component_urn("Function", "api")
    role = _resource_urn("aws:iam/role:Role", "api-role", "Function")
    events = [
        _pre_event(fn, "stelvio:aws:Function", op=OpType.DELETE, parent_urn=STACK_URN),
        _pre_event(role, "aws:iam/role:Role", op=OpType.DELETE, parent_urn=fn),
    ]

    assert rendered(events, operation="destroy", now=1000) == dedent("""
        | Function api
            | IAM Role (0.0s)

        ⠋ Destroying  0/1 complete  0s
        """)


def test_refresh_active_frame_shows_refreshing_footer():
    # REFRESH-op components group as unchanged (hidden until drift), so an in-progress
    # refresh is a bare "Refreshing" footer
    fn = _component_urn("Function", "api")
    lam = _resource_urn("aws:lambda/function:Function", "api-fn", "Function")
    events = [
        _pre_event(fn, "stelvio:aws:Function", op=OpType.REFRESH, parent_urn=STACK_URN),
        _pre_event(lam, "aws:lambda/function:Function", op=OpType.REFRESH, parent_urn=fn),
    ]

    assert rendered(events, operation="refresh", now=1000) == "\n⠋ Refreshing  0s\n"


# ===========================================================================
# Property diffs and replacement warnings
#
# Known-unpinned formatter edges (probed 2026-07-29, accepted without seam tests —
# rare paths; census follow-up in todo.md): missing-value-with-arn-counterpart and
# fingerprint-with-arn-counterpart masking (_format_detail_value), whitespace
# collapse in displayed values (_format_value), None -> `null` detail marker.
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
    """Added props render `+ name = value`, alphabetical, with the forces-replacement marker
    on replace-kind adds/deletes. A removed prop the payload carries no old inputs for falls
    back to a bare `- name` rather than an empty `= `."""
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


def test_engine_internal_input_keys_are_never_shown():
    """Pulumi annotates inputs with `__defaults` (which properties it filled in). Those keys
    are engine bookkeeping — they must not reach the diff line or the JSON payload, at any
    nesting depth. Inputs are sanitized once on ingestion, so both surfaces pin the same fix.
    """
    parent_urn = _component_urn("Function", "api")
    res_urn = _resource_urn("aws:lambda/function:Function", "api-fn", "Function")
    events = [
        _pre_event(
            res_urn,
            "aws:lambda/function:Function",
            op=OpType.UPDATE,
            parent_urn=parent_urn,
            detailed_diff={"environment": _pdiff(DiffKind.ADD)},
            new_inputs={
                "__defaults": ["timeout"],
                "environment": {"__defaults": [], "variables": {"LOG_LEVEL": "debug"}},
            },
        ),
    ]

    assert rendered(events, operation="preview") == dedent("""
        ~ Function api  (1 to update)
            ~ Lambda Function
                + environment = {'variables': {'LOG_LEVEL': 'debug'}}

        ⠋ Analyzing differences  0/1 complete  0s
        """)
    assert summary_json(events, operation="preview")["components"][0]["resources"][0][
        "changes"
    ] == [
        {
            "path": "environment",
            "kind": "add",
            "new": {"variables": {"LOG_LEVEL": "debug"}},
        }
    ]


def test_unresolved_output_sentinel_is_never_shown():
    """Pulumi stands in for a value it can't know until apply with a fixed uuid sentinel.
    That uuid is engine bookkeeping — the diff line and the JSON payload both show
    `output<string>` instead, whatever the previous value looked like. Inputs are sanitized
    once on ingestion, so both surfaces pin the same fix.
    """
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
                        "STLV_TABLE_ARN": "arn:aws:dynamodb:t",
                        "STLV_TABLE_NAME": "users-e7cba56",
                    }
                }
            },
            new_inputs={
                "environment": {
                    "variables": {
                        "STLV_TABLE_ARN": UNKNOWN_OUTPUT_SENTINEL,
                        "STLV_TABLE_NAME": UNKNOWN_OUTPUT_SENTINEL,
                    }
                }
            },
        ),
    ]

    assert rendered(events, operation="preview") == dedent("""
        ~ Function api  (1 to update)
            ~ Lambda Function
                * environment.variables (keys: 2 changed)
                    ~ STLV_TABLE_ARN
                      old: arn:aws:dynamodb:t
                      new: output<string>
                    ~ STLV_TABLE_NAME
                      old: users-e7cba56
                      new: output<string>

        ⠋ Analyzing differences  0/1 complete  0s
        """)
    assert summary_json(events, operation="preview")["components"][0]["resources"][0][
        "changes"
    ] == [
        {
            "path": "environment.variables",
            "kind": "update",
            "old": {
                "STLV_TABLE_ARN": "arn:aws:dynamodb:t",
                "STLV_TABLE_NAME": "users-e7cba56",
            },
            "new": {
                "STLV_TABLE_ARN": "output<string>",
                "STLV_TABLE_NAME": "output<string>",
            },
        }
    ]


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


def test_preview_summarizes_list_valued_property_without_detail_lines():
    """List (and other non-dict, non-JSON) values fall back to a `value changed` summary —
    the diff machinery details dict and JSON-string values only."""
    parent_urn = _component_urn("Function", "api")
    res_urn = _resource_urn("aws:lambda/function:Function", "api-fn", "Function")
    events = [
        _pre_event(
            res_urn,
            "aws:lambda/function:Function",
            op=OpType.UPDATE,
            parent_urn=parent_urn,
            detailed_diff={"layers": _pdiff(DiffKind.UPDATE)},
            old_inputs={"layers": ["arn:aws:lambda:eu-west-1:1:layer:shared:1"]},
            new_inputs={
                "layers": [
                    "arn:aws:lambda:eu-west-1:1:layer:shared:1",
                    "arn:aws:lambda:eu-west-1:1:layer:extra:3",
                ]
            },
        ),
    ]

    assert rendered(events, operation="preview") == dedent("""
        ~ Function api  (1 to update)
            ~ Lambda Function
                * layers (value changed)

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


def test_preview_reports_json_reserialization_without_detail_lines():
    """A JSON property whose parsed content is identical (key order only) renders a bare
    `JSON updated` summary — no path details, because no path actually changed."""
    parent_urn = _component_urn("Function", "api")
    res_urn = _resource_urn("aws:iam/policy:Policy", "api-p", "Function")
    events = [
        _pre_event(
            res_urn,
            "aws:iam/policy:Policy",
            op=OpType.UPDATE,
            parent_urn=parent_urn,
            detailed_diff={"policy": _pdiff(DiffKind.UPDATE)},
            old_inputs={"policy": '{"Version":"2012-10-17","Statement":[]}'},
            new_inputs={"policy": '{"Statement":[],"Version":"2012-10-17"}'},
        ),
    ]

    assert rendered(events, operation="preview") == dedent("""
        ~ Function api  (1 to update)
            ~ IAM Policy
                * policy (JSON updated)

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


def test_preview_details_removed_dict_key_with_its_old_value():
    """A key dropped from a dict property renders `- KEY (was value)` under a
    `keys: N removed` header — the removal sibling of the changed/added details."""
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
                    "variables": {"STLV_TABLE_ARN": "arn:aws:dynamodb:t", "LOG_LEVEL": "debug"}
                }
            },
            new_inputs={"environment": {"variables": {"LOG_LEVEL": "debug"}}},
        ),
    ]

    assert rendered(events, operation="preview") == dedent("""
        ~ Function api  (1 to update)
            ~ Lambda Function
                * environment.variables (keys: 1 removed)
                    - STLV_TABLE_ARN (was arn:aws:dynamodb:t)

        ⠋ Analyzing differences  0/1 complete  0s
        """)


def test_preview_truncates_long_values_in_the_middle_on_narrow_terminal():
    """Long inline values are ellipsized in the MIDDLE, keeping both ends — for arns the
    head identifies, the tail distinguishes. The narrow twin of the wide test below."""
    parent_urn = _component_urn("Function", "api")
    res_urn = _resource_urn("aws:lambda/function:Function", "api-fn", "Function")
    events = [
        _pre_event(
            res_urn,
            "aws:lambda/function:Function",
            op=OpType.UPDATE,
            parent_urn=parent_urn,
            detailed_diff={"role": _pdiff(DiffKind.UPDATE)},
            old_inputs={"role": "arn:aws:iam::123456789012:role/myapp-dev-api-fn-role-old"},
            new_inputs={"role": "arn:aws:iam::123456789012:role/myapp-dev-api-fn-role-new"},
        ),
    ]

    assert rendered(events, operation="preview", width=80) == dedent("""
        ~ Function api  (1 to update)
            ~ Lambda Function
                * role = arn:aws:iam...-fn-role-old -> arn:aws:iam...-fn-role-new

        ⠋ Analyzing differences  0/1 complete  0s
        """)


def test_preview_fits_an_added_value_to_the_terminal_width():
    """An added property shows one value, so it gets the whole remaining row — but it must
    still FIT it. Budgeting per line (indent + glyph + path + ` = `) is what keeps a bulky
    value like a new index from wrapping onto an unindented second line."""
    res_urn = _resource_urn("aws:dynamodb/table:Table", "users-table", "DynamoTable")
    events = [
        _pre_event(
            res_urn,
            "aws:dynamodb/table:Table",
            op=OpType.UPDATE,
            parent_urn=_component_urn("DynamoTable", "users"),
            detailed_diff={"globalSecondaryIndexes": _pdiff(DiffKind.ADD)},
            new_inputs={
                "globalSecondaryIndexes": [
                    {"name": "by-email", "hashKey": "email", "projectionType": "KEYS_ONLY"}
                ]
            },
        ),
    ]

    assert rendered(events, operation="preview", width=80) == dedent("""
        ~ DynamoTable users  (1 to update)
            ~ DynamoDB Table
                + globalSecondaryIndexes = [{'name': 'by-email',...nType': 'KEYS_ONLY'}]

        ⠋ Analyzing differences  0/1 complete  0s
        """)


def test_preview_shows_what_a_removed_property_held():
    """A removed property names what is going away: `- path` alone says nothing about what
    is lost, so the old value rides along on the same row — budgeted to the terminal width
    exactly like an added value, since both show a single value."""
    res_urn = _resource_urn("aws:dynamodb/table:Table", "users-table", "DynamoTable")
    events = [
        _pre_event(
            res_urn,
            "aws:dynamodb/table:Table",
            op=OpType.UPDATE,
            parent_urn=_component_urn("DynamoTable", "users"),
            detailed_diff={"globalSecondaryIndexes": _pdiff(DiffKind.DELETE)},
            old_inputs={
                "globalSecondaryIndexes": [
                    {"name": "by-email", "hashKey": "email", "projectionType": "KEYS_ONLY"}
                ]
            },
        ),
    ]

    assert rendered(events, operation="preview", width=80) == dedent("""
        ~ DynamoTable users  (1 to update)
            ~ DynamoDB Table
                - globalSecondaryIndexes = [{'name': 'by-email',...nType': 'KEYS_ONLY'}]

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


@mark.parametrize(
    ("component_type", "comp_name", "res_type", "res_name", "frame"),
    [
        (
            "DynamoTable",
            "users",
            "aws:dynamodb/table:Table",
            "users-table",
            dedent("""
            ± DynamoTable users  (1 to replace)
                ± DynamoDB Table
                    !! Replacement recreates resource; data may be lost.

            ⠋ Analyzing differences  0/1 complete  0s
            """),
        ),
        (
            "Bucket",
            "media",
            "aws:s3/bucketV2:BucketV2",
            "media-bucket",
            dedent("""
            ± Bucket media  (1 to replace)
                ± S3 Bucket
                    !! Replacement recreates resource; data may be lost.

            ⠋ Analyzing differences  0/1 complete  0s
            """),
        ),
        (
            "Queue",
            "jobs",
            "aws:sqs/queue:Queue",
            "jobs-queue",
            dedent("""
            ± Queue jobs  (1 to replace)
                ± SQS Queue
                    !! Replacement recreates resource; data may be lost.

            ⠋ Analyzing differences  0/1 complete  0s
            """),
        ),
    ],
    ids=["dynamo-table", "s3-bucket", "sqs-queue"],
)
def test_replacement_warning_shown_for_replace_operation_without_detailed_diff(
    component_type, comp_name, res_type, res_name, frame
):
    # One row per data-backed type so _DATA_LOSS_REPLACEMENT_TYPES membership stays pinned:
    # dropping a type from the allowlist silently drops its data-loss warning (the Function
    # test below pins the negative side).
    parent_urn = _component_urn(component_type, comp_name)
    res_urn = _resource_urn(res_type, res_name, component_type)
    events = [_pre_event(res_urn, res_type, op=OpType.REPLACE, parent_urn=parent_urn)]

    assert rendered(events, operation="preview") == frame


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


def test_create_replacement_operation_counts_as_replaced_in_json_summary():
    # Deploys split a replacement into create-replacement/delete-replaced steps; the op's
    # membership in has_replacement's check keeps the resource counted "replaced" — without
    # it, CREATE_REPLACEMENT matches no operation elif and would be reported "unchanged".
    parent = _component_urn("DynamoTable", "users")
    res = _resource_urn("aws:dynamodb/table:Table", "users-table", "DynamoTable")
    events = [
        _pre_event(
            res, "aws:dynamodb/table:Table", op=OpType.CREATE_REPLACEMENT, parent_urn=parent
        ),
        _outputs_event(
            res, "aws:dynamodb/table:Table", op=OpType.CREATE_REPLACEMENT, parent_urn=parent
        ),
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


def test_preview_render_hides_unchanged_children():
    # an unchanged (SAME) child is hidden from the preview frame AND excluded from the
    # header count — only the changed sibling shows
    parent_urn = _component_urn("Function", "api")
    role = _resource_urn("aws:iam/role:Role", "api-role", "Function")
    fn = _resource_urn("aws:lambda/function:Function", "api-fn", "Function")
    events = [
        _pre_event(role, "aws:iam/role:Role", op=OpType.SAME, parent_urn=parent_urn),
        _pre_event(fn, "aws:lambda/function:Function", op=OpType.UPDATE, parent_urn=parent_urn),
    ]

    assert rendered(events, operation="preview") == dedent("""
        ~ Function api  (1 to update)
            ~ Lambda Function

        ⠋ Analyzing differences  0/1 complete  0s
        """)


def test_replace_component_stays_visible_when_unchanged_child_is_listed_first():
    # the component op is the highest-priority child op; if REPLACE lost its priority a
    # SAME child listed first would win the tie and route the whole component to the
    # hidden unchanged bucket — swallowing the data-loss warning with it
    parent_urn = _component_urn("DynamoTable", "users")
    role = _resource_urn("aws:iam/role:Role", "users-role", "DynamoTable")
    table = _resource_urn("aws:dynamodb/table:Table", "users-table", "DynamoTable")
    events = [
        _pre_event(role, "aws:iam/role:Role", op=OpType.SAME, parent_urn=parent_urn),
        _pre_event(table, "aws:dynamodb/table:Table", op=OpType.REPLACE, parent_urn=parent_urn),
        _summary_event(),
    ]

    assert rendered(events, operation="preview") == dedent("""
        ± DynamoTable users  (1 to replace)
            ± DynamoDB Table
                !! Replacement recreates resource; data may be lost.

        """)


def test_preview_renders_state_discard_as_generic_change():
    # DISCARD (state-only cleanup, e.g. after an interrupted replace) has no dedicated
    # preview_summary label — it falls through to the generic "to change" count
    parent_urn = _component_urn("Function", "api")
    dep = _resource_urn("aws:apigateway/deployment:Deployment", "api-deploy", "Function")
    events = [
        _pre_event(
            dep, "aws:apigateway/deployment:Deployment", op=OpType.DISCARD, parent_urn=parent_urn
        ),
        _summary_event(),
    ]

    assert rendered(events, operation="preview") == dedent("""
        - Function api  (1 to change)
            - API Deployment

        """)


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


def test_refresh_final_frame_expands_drifted_component_with_diffs():
    # refresh keeps a drifted component expanded in the final frame (has_drift) and shows
    # the drift's property diffs (show_diffs covers refresh, not only preview). Real refresh
    # payloads: inputs hold the program's declared values on BOTH sides, and the PRE event
    # holds the recorded state on both sides too — only the OUTPUTS event has seen the live
    # cloud read, so drift values must come from ITS outputs (seen live 2026-07-31 as
    # `512 -> 512` when the pre event's values were kept).
    comp_urn = _component_urn("Function", "api")
    res_urn = _resource_urn("aws:lambda/function:Function", "myapp-dev-api", "Function")
    events = [
        _pre_event(comp_urn, "stelvio:aws:Function", op=OpType.REFRESH, parent_urn=STACK_URN),
        _pre_event(
            res_urn,
            "aws:lambda/function:Function",
            op=OpType.REFRESH,
            parent_urn=comp_urn,
            old_inputs={"memorySize": 128},
            new_inputs={"memorySize": 128},
            old_outputs={"memorySize": 128},
            new_outputs={"memorySize": 128},
        ),
        _outputs_event(
            res_urn,
            "aws:lambda/function:Function",
            op=OpType.UPDATE,
            parent_urn=comp_urn,
            detailed_diff={"memorySize": _pdiff(DiffKind.UPDATE)},
            old_inputs={"memorySize": 128},
            new_inputs={"memorySize": 128},
            old_outputs={"memorySize": 128},
            new_outputs={"memorySize": 512},
        ),
        _summary_event(),
    ]

    assert rendered(events, operation="refresh") == dedent("""
        ✓ Function api
            ✓ Lambda Function
                * memorySize = 128 -> 512

        """)


@mark.parametrize(
    ("diffs", "summary"),
    [
        (["memorySize"], "memorySize changed"),
        (["memorySize", "timeout", "handler"], "memorySize, timeout, handler changed"),
        (["a", "b", "c", "d"], "4 properties changed"),
    ],
    ids=["one-property", "up-to-three-listed", "four-plus-counted"],
)
def test_refresh_summarizes_drift_without_detailed_diff(diffs, summary):
    # real refreshes often report drift as a bare property-name list (no detailed_diff);
    # up to three names are listed, more collapse to a count
    fn = _component_urn("Function", "api")
    lam = _resource_urn("aws:lambda/function:Function", "api-fn", "Function")
    events = [
        _pre_event(fn, "stelvio:aws:Function", op=OpType.REFRESH, parent_urn=STACK_URN),
        _pre_event(lam, "aws:lambda/function:Function", op=OpType.REFRESH, parent_urn=fn),
        _outputs_event(
            lam, "aws:lambda/function:Function", op=OpType.UPDATE, parent_urn=fn, diffs=diffs
        ),
        _summary_event(),
    ]

    assert rendered(events, operation="refresh") == dedent(f"""
        ✓ Function api
            ✓ Lambda Function ({summary})

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


def test_compact_preview_header_joins_mixed_operations():
    # compact twin of the mixed-operations header: the resource-word join is its own branch
    parent_urn = _component_urn("Function", "api")
    events = [
        _pre_event(
            _resource_urn("aws:iam/role:Role", "api-role", "Function"),
            "aws:iam/role:Role",
            op=OpType.CREATE,
            parent_urn=parent_urn,
        ),
        _pre_event(
            _resource_urn("aws:lambda/function:Function", "api-fn", "Function"),
            "aws:lambda/function:Function",
            op=OpType.UPDATE,
            parent_urn=parent_urn,
        ),
    ]

    assert rendered(events, operation="preview", compact=True) == dedent("""
        + Function api  (1 resource to create, 1 resource to update)

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
    # Two creates: the counts are RESOURCES, and only the first label carries the noun (plural
    # here, singular in test_preview_completion_uses_singular_component_word).
    create = _resource_urn("aws:lambda/function:Function", "api-fn", "Function")
    create2 = _resource_urn("aws:iam/role:Role", "api-role", "Function")
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
            create2,
            "aws:iam/role:Role",
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
          4 components: 2 resources to create, 1 to update, 1 to replace, 1 to delete
        """)


def test_preview_completion_counts_are_coloured_per_operation():
    # Each count carries its operation's colour — the same vocabulary as the tree glyphs.
    # The trailing DISCARD has no label of its own: it falls back to a neutral white "to change".
    def one_resource(component_type, comp_name, res_type, res_name, op):
        return _pre_event(
            _resource_urn(res_type, res_name, component_type),
            res_type,
            op=op,
            parent_urn=_component_urn(component_type, comp_name),
        )

    events = [
        one_resource("Function", "api", "aws:lambda/function:Function", "api-fn", OpType.CREATE),
        one_resource("Function", "cfg", "aws:lambda/function:Function", "cfg-fn", OpType.UPDATE),
        one_resource("DynamoTable", "users", "aws:dynamodb/table:Table", "tbl", OpType.REPLACE),
        one_resource("Function", "old", "aws:lambda/function:Function", "old-fn", OpType.DELETE),
        one_resource("Function", "stale", "aws:lambda/function:Function", "st-fn", OpType.DISCARD),
    ]

    assert styled_completion(events, operation="preview") == (
        "✓ Analyzed in 0s\n"
        "  5 components: [green]1[/green][green] resource to create[/green], "
        "[yellow]1[/yellow][yellow] to update[/yellow], "
        "[blue]1[/blue][blue] to replace[/blue], "
        "[red]1[/red][red] to delete[/red], "
        "[white]1[/white][white] to change[/white]\n"
    )


def test_preview_completion_counts_a_diff_driven_replacement_as_replace():
    # The operation is UPDATE; only the diff kind says "this forces a replacement". The counts
    # line must follow the diff, not the operation, or a replacement reads as a plain update.
    events = [
        _pre_event(
            _resource_urn("aws:lambda/function:Function", "api-fn", "Function"),
            "aws:lambda/function:Function",
            op=OpType.UPDATE,
            parent_urn=_component_urn("Function", "api"),
            detailed_diff={"runtime": _pdiff(DiffKind.UPDATE_REPLACE)},
        )
    ]

    assert completion(events, operation="preview") == dedent("""\
        ✓ Analyzed in 0s
          1 component: 1 resource to replace
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


def test_deploy_completion_counts_exclude_read_resources():
    # a READ (data-source lookup) deploys nothing — it must not inflate the counts line
    fn = _component_urn("Function", "api")
    role = _resource_urn("aws:iam/role:Role", "api-role", "Function")
    cert = f"urn:pulumi:{STACK}::{PROJECT}::aws:acm/certificate:Certificate::external-cert"
    events = [
        _pre_event(role, "aws:iam/role:Role", parent_urn=fn),
        _outputs_event(role, "aws:iam/role:Role"),
        _pre_event(cert, "aws:acm/certificate:Certificate", op=OpType.READ),
        _outputs_event(cert, "aws:acm/certificate:Certificate", op=OpType.READ),
        _summary_event(),
    ]

    assert completion(events) == dedent("""\
        ✓ Deployed in 0s
          1 component (1 resource) deployed
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


def test_completion_reports_minutes_and_seconds():
    # runs over a minute render "Nm Ss", not raw seconds
    assert completion([], duration=(2, 5)) == "✓ Deployed in 2m 5s\n"


def test_preview_completion_omits_counts_for_noop():
    fn = _resource_urn("aws:lambda/function:Function", "api-fn", "Function")
    events = [
        _pre_event(
            fn,
            "aws:lambda/function:Function",
            op=OpType.SAME,
            parent_urn=_component_urn("Function", "api"),
        ),
    ]

    # an all-SAME preview shows the header alone: SAME resources are skipped from the
    # counts, and empty counts print nothing (mirror of the deploy noop test)
    assert completion(events, operation="preview") == "✓ Analyzed in 0s\n"


def test_preview_completion_uses_singular_component_word():
    fn = _resource_urn("aws:lambda/function:Function", "api-fn", "Function")
    events = [
        _pre_event(
            fn,
            "aws:lambda/function:Function",
            op=OpType.CREATE,
            parent_urn=_component_urn("Function", "api"),
        ),
    ]

    assert completion(events, operation="preview") == dedent("""\
        ✓ Analyzed in 0s
          1 component: 1 resource to create
        """)


def test_preview_completion_counts_orphan_resources_without_component_prefix():
    urn = f"urn:pulumi:{STACK}::{PROJECT}::aws:s3/bucketV2:BucketV2::manual-bucket"
    events = [_pre_event(urn, "aws:s3/bucketV2:BucketV2")]

    # only an orphan changes: bare op counts, no "N components:" prefix
    assert completion(events, operation="preview") == dedent("""\
        ✓ Analyzed in 0s
          1 resource to create
        """)


def test_deploy_completion_counts_orphan_resources_without_component_prefix():
    urn = f"urn:pulumi:{STACK}::{PROJECT}::aws:s3/bucketV2:BucketV2::manual-bucket"
    events = [
        _pre_event(urn, "aws:s3/bucketV2:BucketV2"),
        _outputs_event(urn, "aws:s3/bucketV2:BucketV2"),
    ]

    # only an orphan changed: bare resource count, no "N components (…)" wrapper
    assert completion(events) == dedent("""\
        ✓ Deployed in 0s
          1 resource deployed
        """)


def test_refresh_completion_reports_totals_and_drift():
    comp = _component_urn("Function", "api")
    role = _resource_urn("aws:iam/role:Role", "api-role", "Function")
    fn = _resource_urn("aws:lambda/function:Function", "api-fn", "Function")
    events = [
        _pre_event(role, "aws:iam/role:Role", op=OpType.REFRESH, parent_urn=comp),
        _outputs_event(role, "aws:iam/role:Role", op=OpType.REFRESH),
        _pre_event(fn, "aws:lambda/function:Function", op=OpType.REFRESH, parent_urn=comp),
        _outputs_event(fn, "aws:lambda/function:Function", op=OpType.UPDATE),
    ]

    # refresh counts ALL resources (in-sync ones included — unlike deploy) and appends
    # the drift count; a resource whose refresh came back UPDATE has drifted
    assert completion(events, operation="refresh") == dedent("""\
        ✓ Refreshed in 0s
          1 component (2 resources) refreshed, 1 resource drifted
        """)


def test_refresh_completion_omits_drift_suffix_when_clean():
    fn = _resource_urn("aws:lambda/function:Function", "api-fn", "Function")
    queue = _resource_urn("aws:sqs/queue:Queue", "tasks-q", "Queue")
    events = [
        _pre_event(
            fn,
            "aws:lambda/function:Function",
            op=OpType.REFRESH,
            parent_urn=_component_urn("Function", "api"),
        ),
        _outputs_event(fn, "aws:lambda/function:Function", op=OpType.REFRESH),
        _pre_event(
            queue,
            "aws:sqs/queue:Queue",
            op=OpType.REFRESH,
            parent_urn=_component_urn("Queue", "tasks"),
        ),
        _outputs_event(queue, "aws:sqs/queue:Queue", op=OpType.REFRESH),
    ]

    assert completion(events, operation="refresh") == dedent("""\
        ✓ Refreshed in 0s
          2 components (2 resources) refreshed
        """)


def test_destroy_completion_uses_destroy_verbs():
    comp = _component_urn("Function", "api")
    fn = _resource_urn("aws:lambda/function:Function", "api-fn", "Function")
    events = [
        _pre_event(fn, "aws:lambda/function:Function", op=OpType.DELETE, parent_urn=comp),
        _outputs_event(fn, "aws:lambda/function:Function", op=OpType.DELETE),
    ]

    assert completion(events, operation="destroy") == dedent("""\
        ✓ Destroyed in 0s
          1 component (1 resource) destroyed
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

    assert summary_json(events, outputs={"function_api_arn": "arn:aws:lambda:demo"}) == {
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
                    {
                        "name": "myapp-dev-api",
                        "type": "aws:lambda/function:Function",
                        "operation": "create",
                    }
                ],
            }
        ],
        "summary": {
            "created": 1,
            "updated": 0,
            "deleted": 0,
            "replaced": 0,
            "failed": 0,
            "unchanged": 0,
        },
        "warnings": [],
        "errors": [],
        "outputs": {"function_api_arn": "arn:aws:lambda:demo"},
    }


def test_build_json_summary_for_deploy_counts_each_resource_outcome():
    comp_urn = _component_urn("Function", "api")
    updated_urn = _resource_urn("aws:lambda/function:Function", "myapp-dev-api", "Function")
    failed_urn = _resource_urn("aws:iam/role:Role", "myapp-dev-api-r", "Function")
    deleted_urn = _resource_urn("aws:iam/policy:Policy", "myapp-dev-api-p", "Function")
    same_urn = _resource_urn("aws:cloudwatch/logGroup:LogGroup", "myapp-dev-api-lg", "Function")
    events = [
        _pre_event(comp_urn, "stelvio:aws:Function", op=OpType.UPDATE, parent_urn=STACK_URN),
        _pre_event(
            updated_urn, "aws:lambda/function:Function", op=OpType.UPDATE, parent_urn=comp_urn
        ),
        _outputs_event(
            updated_urn, "aws:lambda/function:Function", op=OpType.UPDATE, parent_urn=comp_urn
        ),
        _pre_event(failed_urn, "aws:iam/role:Role", parent_urn=comp_urn),
        _diagnostic_event("creation failed", failed_urn),
        _pre_event(deleted_urn, "aws:iam/policy:Policy", op=OpType.DELETE, parent_urn=comp_urn),
        _outputs_event(
            deleted_urn, "aws:iam/policy:Policy", op=OpType.DELETE, parent_urn=comp_urn
        ),
        _pre_event(
            same_urn, "aws:cloudwatch/logGroup:LogGroup", op=OpType.SAME, parent_urn=comp_urn
        ),
    ]

    # a failed resource lands in "failed", not in the bucket of its attempted operation
    assert summary_json(events, status="failed", exit_code=1)["summary"] == {
        "created": 0,
        "updated": 1,
        "deleted": 1,
        "replaced": 0,
        "failed": 1,
        "unchanged": 1,
    }


def test_build_json_summary_for_noop_deploy_reports_unchanged_component():
    comp_urn = _component_urn("Function", "api")
    res_urn = _resource_urn("aws:lambda/function:Function", "myapp-dev-api", "Function")
    events = [
        _pre_event(comp_urn, "stelvio:aws:Function", op=OpType.SAME, parent_urn=STACK_URN),
        _pre_event(res_urn, "aws:lambda/function:Function", op=OpType.SAME, parent_urn=comp_urn),
    ]

    # unchanged components stay listed (labelled "unchanged"); their SAME resources are hidden
    assert summary_json(events)["components"] == [
        {"type": "Function", "name": "api", "operation": "unchanged", "resources": []}
    ]


def test_build_json_summary_omits_unchanged_nested_component():
    outer_urn = _component_urn("Api", "web")
    inner_urn = _component_urn("Function", "worker")
    stage_urn = _resource_urn("aws:apigateway/stage:Stage", "myapp-dev-web-stage", "Api")
    fn_urn = _resource_urn("aws:lambda/function:Function", "myapp-dev-worker", "Function")
    events = [
        _pre_event(outer_urn, "stelvio:aws:Api", op=OpType.UPDATE, parent_urn=STACK_URN),
        _pre_event(inner_urn, "stelvio:aws:Function", op=OpType.SAME, parent_urn=outer_urn),
        _pre_event(fn_urn, "aws:lambda/function:Function", op=OpType.SAME, parent_urn=inner_urn),
        _pre_event(
            stage_urn, "aws:apigateway/stage:Stage", op=OpType.UPDATE, parent_urn=outer_urn
        ),
        _outputs_event(
            stage_urn, "aws:apigateway/stage:Stage", op=OpType.UPDATE, parent_urn=outer_urn
        ),
    ]

    # the all-unchanged nested Function contributes no "components" key on the parent
    assert summary_json(events)["components"] == [
        {
            "type": "Api",
            "name": "web",
            "operation": "update",
            "resources": [
                {
                    "name": "myapp-dev-web-stage",
                    "type": "aws:apigateway/stage:Stage",
                    "operation": "update",
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


def test_stream_warning_before_tracking_derives_context_from_the_urn():
    """Stream warnings emit the moment the diagnostic arrives — live, provider
    verification warnings land BEFORE the resource's pre event, so the URN lookup finds
    nothing tracked. The fallback parses the urn: leaf provider type as `resource`, the
    stelvio segment before it as `component` (a resource urn doesn't carry the component
    NAME — better absent than the physical resource name), and never the mangled
    `/table:Table` a blind stelvio-prefix strip produces."""
    res_urn = _resource_urn("aws:dynamodb/table:Table", "myapp-dev-users", "DynamoTable")
    events = [
        _diagnostic_event(
            'property "globalSecondaryIndexes" is deprecated',
            res_urn,
            severity="warning",
            timestamp=1000,
        ),
    ]

    assert stream_events(events) == [
        {
            "event": "warning",
            "operation": "deploy",
            "app": "myapp",
            "env": "dev",
            "message": 'property "globalSecondaryIndexes" is deprecated',
            "resource": "aws:dynamodb/table:Table",
            "component": "DynamoTable",
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


def test_stream_labels_preview_events_as_diff():
    comp_urn = _component_urn("Function", "api")
    res_urn = _resource_urn("aws:lambda/function:Function", "myapp-dev-api", "Function")
    events = [
        _pre_event(comp_urn, "stelvio:aws:Function", parent_urn=STACK_URN),
        _pre_event(res_urn, "aws:lambda/function:Function", op=OpType.UPDATE, parent_urn=comp_urn),
        _outputs_event(
            res_urn, "aws:lambda/function:Function", op=OpType.UPDATE, parent_urn=comp_urn
        ),
    ]

    # the envelope operation is "diff" for previews (twin of the JSON summary label);
    # the per-resource operation keeps the real op
    assert stream_events(events, operation="preview") == [
        {
            "event": "resource",
            "operation": "diff",
            "app": "myapp",
            "env": "dev",
            "resource": {
                "name": "myapp-dev-api",
                "type": "aws:lambda/function:Function",
                "operation": "update",
            },
            "component": {"type": "Function", "name": "api"},
        }
    ]


def test_stream_emits_error_event_for_failed_tracked_resource():
    comp_urn = _component_urn("Function", "api")
    res_urn = _resource_urn("aws:lambda/function:Function", "myapp-dev-api", "Function")
    events = [
        _pre_event(comp_urn, "stelvio:aws:Function", parent_urn=STACK_URN),
        _pre_event(res_urn, "aws:lambda/function:Function", parent_urn=comp_urn),
        _diagnostic_event("creating Lambda: InvalidParameterValueException", res_urn),
    ]

    # a failed resource never reaches outputs, so the error event is its only stream
    # trace; the payload is the same resource shape the untracked path emits
    assert stream_events(events) == [
        {
            "event": "error",
            "operation": "deploy",
            "app": "myapp",
            "env": "dev",
            "error": {
                "name": "myapp-dev-api",
                "type": "aws:lambda/function:Function",
                "operation": "create",
                "error": "creating Lambda: InvalidParameterValueException",
            },
        }
    ]


def test_stream_emits_error_event_for_untracked_failed_resource():
    res_urn = _resource_urn("aws:dynamodb/table:Table", "standalone-users")
    events = [_diagnostic_event("all attributes must be indexed", res_urn)]

    assert stream_events(events) == [
        {
            "event": "error",
            "operation": "deploy",
            "app": "myapp",
            "env": "dev",
            "error": {
                "name": "standalone-users",
                "type": "aws:dynamodb/table:Table",
                "operation": "create",
                "error": "all attributes must be indexed",
            },
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


def test_build_json_summary_for_diff_lists_added_and_removed_properties_sorted():
    comp_urn = _component_urn("Function", "api")
    res_urn = _resource_urn("aws:lambda/function:Function", "myapp-dev-api", "Function")
    diff_kwargs = {
        "detailed_diff": {
            "timeout": _pdiff(DiffKind.ADD),
            "description": _pdiff(DiffKind.DELETE),
        },
        "old_inputs": {"description": "legacy"},
        "new_inputs": {"timeout": 30},
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

    # changes sort by path; an added property carries only "new", a removed one only "old"
    assert summary_json(events, operation="preview")["components"] == [
        {
            "type": "Function",
            "name": "api",
            "operation": "update",
            "resources": [
                {
                    "name": "myapp-dev-api",
                    "type": "aws:lambda/function:Function",
                    "operation": "update",
                    "changes": [
                        {"path": "description", "kind": "delete", "old": "legacy"},
                        {"path": "timeout", "kind": "add", "new": 30},
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


def test_build_json_summary_for_refresh_reports_drift_values_from_outputs():
    """Refresh drift values come from the OUTPUTS event's outputs — the live cloud read.
    The inputs are the program's declared values, identical on both sides, and the pre
    event carries the recorded state on both sides; either would report `128 -> 128` for
    a resource whose live memory is 512."""
    comp_urn = _component_urn("Function", "api")
    res_urn = _resource_urn("aws:lambda/function:Function", "myapp-dev-api", "Function")
    events = [
        _pre_event(comp_urn, "stelvio:aws:Function", op=OpType.REFRESH, parent_urn=STACK_URN),
        _pre_event(
            res_urn,
            "aws:lambda/function:Function",
            op=OpType.REFRESH,
            parent_urn=comp_urn,
            old_inputs={"memorySize": 128},
            new_inputs={"memorySize": 128},
            old_outputs={"memorySize": 128},
            new_outputs={"memorySize": 128},
        ),
        _outputs_event(
            res_urn,
            "aws:lambda/function:Function",
            op=OpType.UPDATE,
            parent_urn=comp_urn,
            detailed_diff={"memorySize": _pdiff(DiffKind.UPDATE)},
            old_inputs={"memorySize": 128},
            new_inputs={"memorySize": 128},
            old_outputs={"memorySize": 128},
            new_outputs={"memorySize": 512},
        ),
    ]

    assert summary_json(events, operation="refresh")["components"][0]["resources"][0][
        "changes"
    ] == [
        {
            "path": "memorySize",
            "kind": "update",
            "old": 128,
            "new": 512,
        }
    ]


def test_build_json_summary_reports_skipped_childless_component_as_skipped():
    """A component the engine registered but never sent child events for (skipped because
    its dependency failed mid-deploy) has no real operation — ComponentInfo.operation
    falls back to CREATE on empty children, and that fabrication must not reach the JSON.
    It reports "skipped", distinct from "unchanged" (= engine explicitly reported SAME)."""
    table_urn = _component_urn("DynamoTable", "users")
    res_urn = _resource_urn("aws:dynamodb/table:Table", "myapp-dev-users", "DynamoTable")
    events = [
        _pre_event(table_urn, "stelvio:aws:DynamoTable", op=OpType.UPDATE, parent_urn=STACK_URN),
        _pre_event(res_urn, "aws:dynamodb/table:Table", op=OpType.UPDATE, parent_urn=table_urn),
        _failed_event(res_urn, "aws:dynamodb/table:Table"),
        _component_event("Function", "api"),
    ]

    assert summary_json(events, status="failed", exit_code=1)["components"] == [
        {
            "type": "DynamoTable",
            "name": "users",
            "operation": "update",
            "resources": [
                {
                    "name": "myapp-dev-users",
                    "type": "aws:dynamodb/table:Table",
                    "operation": "update",
                }
            ],
        },
        {
            "type": "Function",
            "name": "api",
            "operation": "skipped",
            "resources": [],
        },
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


def test_destroy_events_group_resources_by_the_parent_on_their_old_state():
    # real destroy steps carry no ``new`` state — the parent URN rides on ``old``.
    # Without that fallback every destroyed resource would degrade to an orphan.
    fn = _component_urn("Function", "api")
    role = _resource_urn("aws:iam/role:Role", "api-role", "Function")
    events = [
        _destroy_pre_event(fn, "stelvio:aws:Function", STACK_URN),
        _destroy_pre_event(role, "aws:iam/role:Role", fn),
        _destroy_outputs_event(role, "aws:iam/role:Role", fn),
    ]

    assert summary_json(events, operation="destroy") == {
        "operation": "destroy",
        "app": "myapp",
        "env": "dev",
        "status": "success",
        "exit_code": 0,
        "components": [
            {
                "type": "Function",
                "name": "api",
                "operation": "delete",
                "resources": [
                    {"name": "api-role", "type": "aws:iam/role:Role", "operation": "delete"}
                ],
            }
        ],
        "summary": {
            "created": 0,
            "updated": 0,
            "deleted": 1,
            "replaced": 0,
            "failed": 0,
            "unchanged": 0,
        },
        "warnings": [],
        "errors": [],
    }


def test_build_json_summary_for_diff_counts_discard_as_delete():
    comp_urn = _component_urn("Queue", "tasks")
    res_urn = _resource_urn("aws:sqs/queue:Queue", "myapp-dev-tasks", "Queue")
    events = [
        _pre_event(comp_urn, "stelvio:aws:Queue", op=OpType.DISCARD, parent_urn=STACK_URN),
        _pre_event(res_urn, "aws:sqs/queue:Queue", op=OpType.DISCARD, parent_urn=comp_urn),
        _outputs_event(res_urn, "aws:sqs/queue:Queue", op=OpType.DISCARD, parent_urn=comp_urn),
    ]

    payload = summary_json(events, operation="preview")

    # a discarded (read) resource is presented as a plain delete
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
                {"name": "myapp-dev-tasks", "type": "aws:sqs/queue:Queue", "operation": "delete"}
            ],
        }
    ]


def test_build_json_summary_for_failed_deploy_includes_warnings_errors_and_orphans():
    orphan_urn = _resource_urn("aws:sqs/queue:Queue", "orphan-queue")
    events = [
        _diagnostic_event("Provider warning", severity="warning", timestamp=999),
        _diagnostic_event("queue failed", orphan_urn),
    ]

    # the CLI always passes fallback_error on failure — with a real resource
    # error present it must not appear as a duplicate entry
    payload = summary_json(
        events,
        status="failed",
        outputs={},
        exit_code=1,
        message="Deploy failed",
        fallback_error="Deploy failed",
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


def test_build_json_summary_deduplicates_identical_resource_errors():
    comp_urn = _component_urn("Vpc", "main")
    subnet_a_urn = _resource_urn("aws:ec2/subnet:Subnet", "myapp-dev-main-a", "Vpc")
    subnet_b_urn = _resource_urn("aws:ec2/subnet:Subnet", "myapp-dev-main-b", "Vpc")
    events = [
        _pre_event(comp_urn, "stelvio:aws:Vpc", parent_urn=STACK_URN),
        _pre_event(subnet_a_urn, "aws:ec2/subnet:Subnet", parent_urn=comp_urn),
        _pre_event(subnet_b_urn, "aws:ec2/subnet:Subnet", parent_urn=comp_urn),
        _diagnostic_event("subnet quota exceeded", subnet_a_urn),
        _diagnostic_event("subnet quota exceeded", subnet_b_urn),
    ]

    # two same-type resources failing identically make ONE "errors" entry; the
    # per-resource detail stays on each resource under "components"
    assert summary_json(events, status="failed", exit_code=1)["errors"] == [
        {
            "resource": "aws:ec2/subnet:Subnet",
            "message": "subnet quota exceeded",
            "component": "Vpc",
            "name": "main",
        }
    ]


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


def test_failed_preview_summary_keeps_component_with_error_on_unchanged_resource():
    comp_urn = _component_urn("DynamoTable", "users")
    res_urn = _resource_urn("aws:dynamodb/table:Table", "myapp-dev-users", "DynamoTable")
    events = [
        _pre_event(comp_urn, "stelvio:aws:DynamoTable", op=OpType.SAME, parent_urn=STACK_URN),
        _pre_event(res_urn, "aws:dynamodb/table:Table", op=OpType.SAME, parent_urn=comp_urn),
        _diagnostic_event("provider error while diffing", res_urn),
    ]

    # the SAME-op resource is hidden, so the component's error is its only signal —
    # the empty-discovered-components filter must not drop it
    assert summary_json(events, operation="preview", status="failed", exit_code=1)[
        "components"
    ] == [
        {
            "type": "DynamoTable",
            "name": "users",
            "operation": "unchanged",
            "resources": [],
            "error": "provider error while diffing",
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


def test_debug_and_info_diagnostics_are_ignored():
    comp_urn = _component_urn("Function", "api")
    res_urn = _resource_urn("aws:lambda/function:Function", "myapp-dev-api", "Function")
    events = [
        _pre_event(res_urn, "aws:lambda/function:Function", parent_urn=comp_urn),
        _outputs_event(res_urn, "aws:lambda/function:Function"),
        _diagnostic_event(
            "Registering resource outputs", res_urn, severity="debug", timestamp=1001
        ),
        _diagnostic_event("retrying request", severity="info", timestamp=1002),
    ]

    # real deploys interleave ~75% debug diagnostics — they must not mark the
    # resource failed or surface anywhere in the output
    assert summary_json(events) == {
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
                    {
                        "name": "myapp-dev-api",
                        "type": "aws:lambda/function:Function",
                        "operation": "create",
                    }
                ],
            }
        ],
        "summary": {
            "created": 1,
            "updated": 0,
            "deleted": 0,
            "replaced": 0,
            "failed": 0,
            "unchanged": 0,
        },
        "warnings": [],
        "errors": [],
    }
    assert completion(events) == dedent("""\
        ✓ Deployed in 0s
          1 component (1 resource) deployed
        """)


def test_debug_diagnostics_do_not_suppress_the_nothing_to_deploy_message():
    # "Nothing to deploy" and the Finalizing spinner print during the summary event,
    # not through the four output helpers — same below-seam class as the live-disabled
    # silence test. Both are suppressed when error diagnostics exist, so a debug
    # diagnostic counting as an error would kill them on every real noop deploy.
    fake_console = Mock()
    with patch.object(Live, "start"), patch.object(Live, "stop"), patch.object(Live, "refresh"):
        handler = build_handler(
            [_diagnostic_event("Registering resource outputs", severity="debug")]
        )
        handler.console = fake_console
        handler.handle_event(_summary_event())

    assert fake_console.print.call_args_list == [call("Nothing to deploy"), call()]
    assert handler.cleanup_status is not None


def _summary_console(events, operation="deploy", **kw):
    """Replay events, then feed the summary event with a mocked console.

    Returns (print call list, cleanup_status) — the noop message + Finalizing spinner
    print during the summary event, below the four output helpers (H1a pattern).
    """
    fake_console = Mock()
    with patch.object(Live, "start"), patch.object(Live, "stop"), patch.object(Live, "refresh"):
        handler = build_handler(events, operation=operation, **kw)
        handler.console = fake_console
        handler.handle_event(_summary_event())
    return fake_console.print.call_args_list, handler.cleanup_status


def test_destroy_noop_prints_nothing_to_destroy():
    calls, cleanup_status = _summary_console([], operation="destroy")

    assert calls == [call("Nothing to destroy"), call()]
    assert cleanup_status is not None  # destroy finalizes (pushes state) like deploy


def test_preview_noop_prints_no_differences_and_skips_finalizing():
    calls, cleanup_status = _summary_console([], operation="preview")

    assert calls == [call("No differences found"), call()]
    assert cleanup_status is None  # previews push no state — no Finalizing spinner


def test_preview_with_only_a_state_discard_still_reports_no_differences():
    # DISCARD is state-only cleanup, not an infrastructure difference — the noop
    # message must survive it (the discarded resource still shows in the tree/JSON)
    fn = _component_urn("Function", "api")
    dep = _resource_urn("aws:apigateway/deployment:Deployment", "api-deploy", "Function")
    events = [
        _pre_event(dep, "aws:apigateway/deployment:Deployment", op=OpType.DISCARD, parent_urn=fn),
    ]

    calls, _ = _summary_console(events, operation="preview")

    assert calls == [call("No differences found"), call()]


def test_visible_read_resource_does_not_suppress_the_noop_message():
    # a READ (data-source lookup) with a visible name is not deployment work —
    # "Nothing to deploy" must still print
    cert = f"urn:pulumi:{STACK}::{PROJECT}::aws:acm/certificate:Certificate::external-cert"
    events = [
        _pre_event(cert, "aws:acm/certificate:Certificate", op=OpType.READ),
        _outputs_event(cert, "aws:acm/certificate:Certificate", op=OpType.READ),
    ]

    calls, _ = _summary_console(events)

    assert calls == [call("Nothing to deploy"), call()]


def test_failed_run_with_no_resource_steps_prints_no_noop_message():
    # a program-level failure before any resource step: printing "Nothing to deploy"
    # would misread the failure as a clean noop, and Finalizing must not start
    calls, cleanup_status = _summary_console(
        [_diagnostic_event("program on fire", severity="error")]
    )

    assert calls == []
    assert cleanup_status is None


def test_dev_mode_skips_the_finalizing_spinner(monkeypatch):
    # dev mode keeps the session alive after deploy — a lingering "Finalizing..."
    # spinner would be a lie. The noop message still prints. (build_handler has no
    # dev_mode arg, so construct directly — same below-seam class as the H1a tests.)
    fake_console = Mock()
    monkeypatch.setattr("stelvio.rich_deployment_handler.Console", lambda: fake_console)
    with patch.object(Live, "start"), patch.object(Live, "stop"), patch.object(Live, "refresh"):
        handler = RichDeploymentHandler("myapp", "dev", "deploy", dev_mode=True)
        handler.handle_event(_summary_event())

    assert fake_console.print.call_args_list == [call("Nothing to deploy"), call()]
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


def test_warning_on_untracked_component_urn_parses_context_from_urn():
    events = [
        _diagnostic_event("provider timeout", _component_urn("Queue", "jobs"), severity="warning")
    ]

    # no pre event ever tracked the component — context still parses from the urn itself
    assert completion(events) == dedent("""\
        ✓ Deployed in 0s

        ⚠ 1 warning
          Queue jobs:
            provider timeout
        """)
    assert summary_json(events)["warnings"] == [
        {"message": "provider timeout", "component": "Queue", "name": "jobs"}
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


def test_untracked_failed_resource_attaches_to_longest_matching_component():
    api_urn = _component_urn("Function", "api")
    api_v2_urn = _component_urn("Function", "api-v2")
    role_urn = _resource_urn("aws:iam/role:Role", "myapp-dev-api-v2-r", "Function")
    events = [
        _pre_event(api_urn, "stelvio:aws:Function", parent_urn=STACK_URN),
        _pre_event(api_v2_urn, "stelvio:aws:Function", parent_urn=STACK_URN),
        _diagnostic_event("role creation failed", role_urn),
    ]

    payload = summary_json(events)

    # "api-v2-r" prefix-matches BOTH "api" and "api-v2" — the most specific
    # (longest) component name wins; the untouched bystander reads skipped
    assert payload["components"] == [
        {"type": "Function", "name": "api", "operation": "skipped", "resources": []},
        {
            "type": "Function",
            "name": "api-v2",
            "operation": "create",
            "resources": [
                {
                    "name": "myapp-dev-api-v2-r",
                    "type": "aws:iam/role:Role",
                    "operation": "create",
                    "error": "role creation failed",
                }
            ],
            "error": "role creation failed",
        },
    ]
    assert payload["errors"] == [
        {
            "resource": "aws:iam/role:Role",
            "message": "role creation failed",
            "component": "Function",
            "name": "api-v2",
        }
    ]
    assert "other_resources" not in payload


def test_untracked_failed_resource_attaches_by_component_type_not_name_alone():
    fn_urn = _component_urn("Function", "users")
    table_comp_urn = _component_urn("DynamoTable", "users")
    table_urn = _resource_urn("aws:dynamodb/table:Table", "myapp-dev-users", "DynamoTable")
    events = [
        _pre_event(fn_urn, "stelvio:aws:Function", parent_urn=STACK_URN),
        _pre_event(table_comp_urn, "stelvio:aws:DynamoTable", parent_urn=STACK_URN),
        _diagnostic_event("table creation failed", table_urn),
    ]

    payload = summary_json(events)

    # a Function and a DynamoTable may share one name — the urn's parent type
    # (DynamoTable) decides the attach, not the name match alone; the untouched
    # bystander reads skipped
    assert payload["components"] == [
        {"type": "Function", "name": "users", "operation": "skipped", "resources": []},
        {
            "type": "DynamoTable",
            "name": "users",
            "operation": "create",
            "resources": [
                {
                    "name": "myapp-dev-users",
                    "type": "aws:dynamodb/table:Table",
                    "operation": "create",
                    "error": "table creation failed",
                }
            ],
            "error": "table creation failed",
        },
    ]
    assert payload["errors"] == [
        {
            "resource": "aws:dynamodb/table:Table",
            "message": "table creation failed",
            "component": "DynamoTable",
            "name": "users",
        }
    ]
    assert "other_resources" not in payload


def test_error_diagnostic_on_component_urn_attaches_to_component():
    comp_urn = _component_urn("DynamoTable", "users")
    res_urn = _resource_urn("aws:dynamodb/table:Table", "myapp-dev-users", "DynamoTable")
    events = [
        _pre_event(comp_urn, "stelvio:aws:DynamoTable", parent_urn=STACK_URN),
        _pre_event(res_urn, "aws:dynamodb/table:Table", parent_urn=comp_urn),
        _diagnostic_event("Duplicate resource URN", comp_urn),
    ]

    # an error on the component's OWN urn belongs to the component — not to a
    # stelvio-typed orphan in other_resources (and summary stays resource-granular)
    assert summary_json(events, status="failed", exit_code=1) == {
        "operation": "deploy",
        "app": "myapp",
        "env": "dev",
        "status": "failed",
        "exit_code": 1,
        "components": [
            {
                "type": "DynamoTable",
                "name": "users",
                "operation": "create",
                "resources": [
                    {
                        "name": "myapp-dev-users",
                        "type": "aws:dynamodb/table:Table",
                        "operation": "create",
                    }
                ],
                "error": "Duplicate resource URN",
            }
        ],
        "summary": {
            "created": 1,
            "updated": 0,
            "deleted": 0,
            "replaced": 0,
            "failed": 0,
            "unchanged": 0,
        },
        "warnings": [],
        "errors": [
            {"component": "DynamoTable", "name": "users", "message": "Duplicate resource URN"}
        ],
    }


def test_error_diagnostic_on_childless_component_renders_failed_header():
    comp_urn = _component_urn("DynamoTable", "users")
    events = [
        _pre_event(comp_urn, "stelvio:aws:DynamoTable", parent_urn=STACK_URN),
        _diagnostic_event("Duplicate resource URN", comp_urn),
    ]

    # a component that errored before creating any child must render as failed,
    # not hide as an unchanged placeholder
    assert rendered([*events, _summary_event()]) == dedent("""
        ✗ DynamoTable users
            Duplicate resource URN

        """)
    assert completion(events) == "✗ Deployed in 0s with errors\n"


def test_error_diagnostic_on_component_urn_streams_component_context():
    comp_urn = _component_urn("DynamoTable", "users")
    events = [
        _pre_event(comp_urn, "stelvio:aws:DynamoTable", parent_urn=STACK_URN),
        _diagnostic_event("Duplicate resource URN", comp_urn),
    ]

    assert stream_events(events) == [
        {
            "event": "error",
            "operation": "deploy",
            "app": "myapp",
            "env": "dev",
            "error": {
                "component": "DynamoTable",
                "name": "users",
                "message": "Duplicate resource URN",
            },
        }
    ]


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


def test_failed_unchanged_orphan_stays_visible_in_preview():
    orphan_urn = _resource_urn("aws:s3/bucketV2:BucketV2", "manual-bucket")
    events = [
        _pre_event(orphan_urn, "aws:s3/bucketV2:BucketV2", op=OpType.SAME),
        _diagnostic_event("provider error while diffing", orphan_urn, timestamp=1001),
    ]

    # an unchanged orphan would normally be hidden — an error must override that
    # on BOTH surfaces, or the failure context vanishes from the preview
    assert rendered(events, operation="preview") == dedent("""
        Other resources
          ✗ S3 Bucket
                provider error while diffing

        ⠋ Analyzing differences  1/1 complete  0s
        """)
    assert summary_json(events, operation="preview", status="failed", exit_code=1)[
        "other_resources"
    ] == [
        {
            "name": "manual-bucket",
            "type": "aws:s3/bucketV2:BucketV2",
            "operation": "unchanged",
            "error": "provider error while diffing",
        }
    ]


def test_hidden_unchanged_components_leave_no_blank_before_other_resources():
    comp_urn = _component_urn("Function", "api")
    lambda_urn = _resource_urn("aws:lambda/function:Function", "api-fn", "Function")
    orphan_urn = _resource_urn("aws:iam/role:Role", "apigw-cloudwatch-role")
    events = [
        _pre_event(
            lambda_urn, "aws:lambda/function:Function", op=OpType.SAME, parent_urn=comp_urn
        ),
        _outputs_event(lambda_urn, "aws:lambda/function:Function", op=OpType.SAME),
        _pre_event(orphan_urn, "aws:iam/role:Role"),
        _outputs_event(orphan_urn, "aws:iam/role:Role"),
    ]

    # orphan-only deploy: the unchanged component is hidden, so the separator blank
    # that belongs between a rendered tree and "Other resources" must not appear —
    # seen live 2026-08-01 as a double blank under the CLI header
    assert rendered(events) == dedent("""
        Other resources
          ✓ IAM Role (1.0s)

        ⠋ Deploying  1/1 complete  0s
        """)
    assert rendered([*events, _summary_event()]) == dedent("""
        Other resources
          ✓ IAM Role (1.0s)

        """)


def test_multi_error_diagnostic_keeps_the_last_bullet():
    resource_urn = _resource_urn("aws:dynamodb/table:Table", "standalone-users")
    events = [
        _diagnostic_event(
            "creating urn:pulumi:dev::myapp::aws:dynamodb/table:Table::users: "
            "2 errors occurred:\n"
            "\t* all attributes must be indexed\n"
            "\t* invalid billing mode",
            resource_urn,
        )
    ]

    # documented current rule: of a multi-error diagnostic only the LAST bullet
    # survives — earlier bullets are dropped with the preamble
    assert summary_json(events)["other_resources"] == [
        {
            "name": "standalone-users",
            "type": "aws:dynamodb/table:Table",
            "operation": "create",
            "error": "invalid billing mode",
        }
    ]


def test_error_diagnostic_collapses_multiline_message_to_one_line():
    resource_urn = _resource_urn("aws:iam/role:Role", "standalone-role")
    events = [
        _diagnostic_event(
            "failed to create role:\nAccessDenied: user is not authorized\n"
            "to perform iam:CreateRole",
            resource_urn,
        )
    ]

    # multiline provider messages are collapsed for inline display
    assert summary_json(events)["other_resources"] == [
        {
            "name": "standalone-role",
            "type": "aws:iam/role:Role",
            "operation": "create",
            "error": "failed to create role: AccessDenied: user is not authorized"
            " to perform iam:CreateRole",
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


def test_same_warning_on_different_resources_is_reported_for_each():
    checkout_comp = _component_urn("Function", "checkout")
    checkout_fn = _resource_urn("aws:lambda/function:Function", "checkout-fn", "Function")
    billing_comp = _component_urn("Function", "billing")
    billing_fn = _resource_urn("aws:lambda/function:Function", "billing-fn", "Function")
    warning = "Node.js 18.x runtime is deprecated"
    events = [
        _pre_event(checkout_fn, "aws:lambda/function:Function", parent_urn=checkout_comp),
        _outputs_event(checkout_fn, "aws:lambda/function:Function"),
        _pre_event(billing_fn, "aws:lambda/function:Function", parent_urn=billing_comp),
        _outputs_event(billing_fn, "aws:lambda/function:Function"),
        _diagnostic_event(warning, checkout_fn, severity="warning", timestamp=1002),
        _diagnostic_event(warning, billing_fn, severity="warning", timestamp=1003),
    ]

    # dedup is per-resource: the same message on two resources is two warnings,
    # each with its own context — not collapsed into one
    assert completion(events, width=160) == dedent("""\
        ✓ Deployed in 0s
          2 components (2 resources) deployed

        ⚠ 2 warnings
          Function checkout → checkout-fn (Lambda Function):
            Node.js 18.x runtime is deprecated
          Function billing → billing-fn (Lambda Function):
            Node.js 18.x runtime is deprecated
        """)


def test_blank_warning_diagnostic_is_dropped():
    events = [_diagnostic_event("   \n\t ", severity="warning")]

    # a whitespace-only warning would render as an empty bullet — it's dropped
    assert completion(events) == "✓ Deployed in 0s\n"
    assert summary_json(events)["warnings"] == []


def test_completion_lists_distinct_context_free_warnings_under_plural_header():
    events = [
        _diagnostic_event("Provider warning one", severity="warning"),
        _diagnostic_event("Provider warning two", severity="warning", timestamp=1002),
    ]

    # distinct messages are NOT deduplicated; a warning with no resource urn prints bare
    assert completion(events, width=160) == dedent("""\
        ✓ Deployed in 0s

        ⚠ 2 warnings
          Provider warning one
          Provider warning two
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

    # the JSON surface carries the hint as its own field, plus the resource type parsed
    # from the pending-create urn (the resource is stale state — never tracked)
    assert summary_json(events)["warnings"] == [
        {
            "message": "A previous deploy appears to have been interrupted while creating"
            " this resource.",
            "hint": "Run `stlv state repair` to clear stale pending operations.",
            "resource": "aws:iam/role:Role",
        }
    ]


# ---------------------------------------------------------------------------
# API Gateway internal resource filtering — render, JSON, and stream
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
# Render and completion: internal resource filtering
# ===========================================================================
# The point of the filter: a deploy after the first Api deploy must look clean —
# the Account/Role .apply() state cleanup is not a user-visible change.


def test_render_hides_apigw_state_cleanup_on_second_deploy():
    """2nd deploy: managed Account/Role DELETE (.apply() cleanup) must not render."""
    events = [
        *_apigw_internal_pre_events(OpType.DELETE),
        *_apigw_internal_outputs_events(OpType.DELETE),
        _summary_event(),
    ]

    assert rendered(events) == "\n"


def test_second_deploy_apigw_cleanup_still_prints_nothing_to_deploy():
    # "Nothing to deploy" prints during the summary event, not through the four output
    # helpers — same below-seam class as the debug-diagnostics noop test. Hidden cleanup
    # DELETEs counting as visible changes would kill the message on every deploy after
    # the first one that touches an Api.
    fake_console = Mock()
    with patch.object(Live, "start"), patch.object(Live, "stop"), patch.object(Live, "refresh"):
        handler = build_handler(
            [
                *_apigw_internal_pre_events(OpType.DELETE),
                *_apigw_internal_outputs_events(OpType.DELETE),
            ]
        )
        handler.console = fake_console
        handler.handle_event(_summary_event())

    assert fake_console.print.call_args_list == [call("Nothing to deploy"), call()]


def test_completion_omits_counts_when_only_hidden_cleanup_ran():
    """2nd deploy: hidden Account/Role cleanup must not produce a counts line."""
    events = [
        *_apigw_internal_pre_events(OpType.DELETE),
        *_apigw_internal_outputs_events(OpType.DELETE),
    ]

    assert completion(events) == "✓ Deployed in 0s\n"


def test_render_hides_read_orphan_resource():
    # A .get() read outside any component is not a change — the render skips it.
    # (JSON other_resources DOES list read orphans — a render-vs-JSON divergence
    # noted in the audit and deliberately left alone.)
    zone_urn = _resource_urn("aws:route53/zone:Zone", "my-zone")
    events = [
        *_create_function_events(),
        _pre_event(zone_urn, "aws:route53/zone:Zone", op=OpType.READ),
        _outputs_event(zone_urn, "aws:route53/zone:Zone", op=OpType.READ),
        _summary_event(),
    ]

    assert rendered(events) == "\n✓ Function api  (1.0s)\n\n"


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


def test_json_other_resources_lists_managed_internals_on_destroy():
    """destroy: managed Account/Role DELETE appear in other_resources."""
    events = [
        *_apigw_internal_pre_events(OpType.DELETE),
        *_apigw_internal_outputs_events(OpType.DELETE),
    ]

    assert summary_json(events, operation="destroy")["other_resources"] == [
        {
            "name": "api-gateway-account",
            "type": "aws:apigateway/account:Account",
            "operation": "delete",
        },
        {
            "name": "StelvioAPIGatewayPushToCloudWatchLogsRole",
            "type": "aws:iam/role:Role",
            "operation": "delete",
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
