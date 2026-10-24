"""Textual presentation for detached, current-user tunnel observations."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, ClassVar

from rich.table import Table
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Label, OptionList, Static
from textual.widgets.option_list import Option

from stelvio.tunnel.diagnostics.resource_checks import targets
from stelvio.tunnel.monitor.client import MonitorClient
from stelvio.tunnel.monitor.model import Dashboard, number

KIB = 1024
MIN_WIDTH = 60
MIN_HEIGHT = 20
COMPACT_WIDTH = 110


if TYPE_CHECKING:
    from textual.events import Resize


EVENT_LABELS = {
    "phase-authenticated-ssh": "Connecting SSH",
    "phase-forwarding": "SSH connected",
    "phase-host-configuration": "Configuring routes and private DNS",
    "phase-readiness": "Checking DNS and resource readiness",
    "vpc-ready": "VPC ready; dependent invocations admitted",
    "vpc-retrying": "VPC reconnecting; dependent invocations blocked",
    "vpc-failed": "VPC blocked; dependent invocations rejected",
}


def rate(value: float | None) -> str:
    if value is None:
        return "—"
    for unit in ("B/s", "KiB/s", "MiB/s", "GiB/s"):
        if value < KIB or unit == "GiB/s":
            return f"{value:.1f} {unit}"
        value /= KIB
    return "—"


def literal(value: object) -> Text:
    return Text("—" if value is None else str(value))


def chart(history: list, width: int) -> Text:
    result = Text("Traffic · last 60 seconds · inner IP\n", style="bold")
    for index, label, color in ((2, "Download", "cyan"), (1, "Upload  ", "magenta")):
        window = [item[index] for item in history][-60:]
        window = [None] * (60 - len(window)) + window
        columns = max(1, min(60, width - 14))
        values = []
        for column in range(columns):
            bucket = window[column * 60 // columns : (column + 1) * 60 // columns]
            values.append(None if any(value is None for value in bucket) else max(bucket))
        scale = max((value for value in values if value is not None), default=0) or 1
        result.append(label + "  ", style=color)
        for value in values:
            result.append(
                "·" if value is None else "▁▂▃▄▅▆▇█"[min(7, int(value / scale * 7))], style=color
            )
        result.append("\n")
    result.append("· = unavailable sample", style="dim")
    return result


class SessionPicker(ModalScreen[str]):
    BINDINGS: ClassVar = [("escape", "cancel", "Cancel")]

    def __init__(self, sessions: list[dict]) -> None:
        super().__init__()
        self.sessions = sessions

    def compose(self) -> ComposeResult:
        yield Label("Select a tunnel session")
        yield OptionList(
            *(
                Option(
                    Text(
                        f"{value['session'].get('app', '?')} / "
                        f"{value['session'].get('environment', '?')}  "
                        f"{value['session']['session_id']}"
                    ),
                    id=value["session"]["session_id"],
                )
                for value in self.sessions
            )
        )

    @on(OptionList.OptionSelected)
    def selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    def action_cancel(self) -> None:
        self.dismiss(None)


class Sample(Message):
    def __init__(self, value: dict) -> None:
        super().__init__()
        self.value = value


class TunnelMonitor(App):
    """This app owns only diagnostic clients; quit has no tunnel shutdown path."""

    TITLE = "Stelvio Tunnel"
    BINDINGS: ClassVar = [
        ("q", "quit", "Quit"),
        ("ctrl+c", "quit", "Quit"),
        ("s", "sessions", "Sessions"),
        ("r", "refresh", "Refresh"),
    ]
    CSS = """
    Screen { background: $surface; }
    #body { height: 1fr; padding: 0 2; }
    #heading { height: auto; margin-bottom: 1; }
    #summary { height: auto; margin-bottom: 1; }
    #traffic { height: 6; border: round $primary; padding: 0 1; }
    #vpcs { height: 7; margin-top: 1; }
    #lower { height: auto; }
    #details { height: auto; width: 2fr; margin-top: 1; }
    #events { height: auto; width: 1fr; margin-top: 1; }
    Screen.compact #lower { layout: vertical; }
    Screen.compact #details, Screen.compact #events { width: 100%; }
    #resize { display: none; height: 1fr; content-align: center middle; }
    Screen.small #body { display: none; }
    Screen.small #resize { display: block; }
    SessionPicker { align: center middle; background: $background 90%; }
    SessionPicker Label { width: 90%; height: 2; }
    SessionPicker OptionList { width: 90%; height: auto; max-height: 70%; }
    """

    def __init__(self, session: str | None = None, *, client: MonitorClient | None = None) -> None:
        super().__init__()
        self.client = client or MonitorClient(session)
        self.dashboard = Dashboard()
        self.sessions = []
        self.requested = None
        self.selected_unit = None
        self.polling = False
        self.selector_shown = False
        self.error = None

    def compose(self) -> ComposeResult:
        yield Static("Resize the terminal to at least 60 x 20. Press q to quit.", id="resize")
        with VerticalScroll(id="body"):
            yield Static(id="heading")
            yield Static(id="summary")
            yield Static(id="traffic")
            yield DataTable(id="vpcs", cursor_type="row")
            with Horizontal(id="lower"):
                yield Static(id="details")
                yield Static(id="events")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one(DataTable).add_columns("VPC", "State", "Streams", "Upload", "Download")
        self.query_one(DataTable).focus()
        self.set_interval(1, self.action_refresh)
        self.action_refresh()
        self.render_dashboard()

    def on_resize(self, event: Resize) -> None:
        self.screen.set_class(
            event.size.width < MIN_WIDTH or event.size.height < MIN_HEIGHT, "small"
        )
        self.screen.set_class(event.size.width < COMPACT_WIDTH, "compact")
        self.render_dashboard()

    def action_refresh(self) -> None:
        if not self.polling:
            self.polling = True
            self.poll(self.requested)

    @work(thread=True, exclusive=True)
    def poll(self, requested: str | None) -> None:
        try:
            value = self.client.poll(requested)
        except (OSError, ValueError, TypeError, KeyError, EOFError):
            value = {"snapshot": None, "sessions": [], "error": "Diagnostics unavailable"}
        self.post_message(Sample(value))

    def on_sample(self, message: Sample) -> None:
        self.polling = False
        self.sessions = message.value["sessions"]
        self.error = message.value["error"]
        value = message.value["snapshot"]
        if value is None:
            self.dashboard.missing(time.monotonic())
        else:
            self.dashboard.update(value, time.monotonic())
        self.render_dashboard()
        if value is None and len(self.sessions) > 1 and not self.selector_shown:
            self.selector_shown = True
            self.action_sessions()

    def action_sessions(self) -> None:
        if self.sessions and not self.client.pinned and len(self.screen_stack) == 1:
            self.push_screen(SessionPicker(self.sessions), self.select_session)

    def select_session(self, session: str | None) -> None:
        if session is not None:
            self.requested = session
            self.action_refresh()

    @on(DataTable.RowHighlighted)
    def vpc_selected(self, event: DataTable.RowHighlighted) -> None:
        self.selected_unit = event.row_key.value
        self.render_details()

    def render_dashboard(self) -> None:  # noqa: C901 - render the dashboard sections from one projection
        if not self.is_mounted or not self.query("#heading"):
            return
        model = self.dashboard
        session = (model.snapshot or {}).get("session", {})
        heading = Text("Stelvio Tunnel", style="bold")
        heading.append(
            f"                       {session.get('app', '—')} / "
            f"{session.get('environment', '—')}\n"
        )
        heading.append(model.state, style="green" if model.state == "READY" else "yellow")
        connected = [row.get("connected_seconds") for row in model.rows]
        if model.state == "READY" and connected and all(number(value) for value in connected):
            seconds = int(min(connected))
            heading.append(
                f"                      Connected for {seconds // 60}m {seconds % 60:02d}s"
            )
        age = time.monotonic() - model.last_received if model.last_received is not None else None
        if age is not None:
            heading.append(f"\nSnapshot age: {age:.1f}s", style="dim")
        if self.error:
            heading.append("\n" + self.error, style="yellow")
        self.query_one("#heading", Static).update(heading)
        available = not model.disconnected
        ready = sum(row.get("state") == "ready" for row in model.rows) if available else 0
        streams = model.total("streams") if available else None
        drops = model.total("drops") if available else None
        queues = model.total("queue_drops") if available else None
        dns = (
            "healthy"
            if available and model.rows and all(row["dns_healthy"] for row in model.rows)
            else "unavailable / not ready"
        )
        summary = Text(
            f"{ready} / {len(model.rows)} VPCs       "
            f"{streams if streams is not None else '—'} TCP streams       DNS {dns}\n"
            f"↑ {rate(model.total('upload') if available else None)}      "
            f"↓ {rate(model.total('download') if available else None)}      "
            f"{drops if drops is not None else '—'} packet drops; "
            f"{queues if queues is not None else '—'} queue drops"
        )
        if any(model.total(field) is None for field in ("streams", "upload", "download", "drops")):
            summary.append("\nPartial observations · unavailable totals are —", style="dim")
        if not session:
            summary.append("\nStart a session with: stlv tunnel start [ENV]")
        self.query_one("#summary", Static).update(summary)
        self.query_one("#traffic", Static).update(chart(list(model.history), self.size.width - 8))
        table = self.query_one(DataTable)
        cursor = table.cursor_row
        table.clear()
        table.styles.height = min(10, max(4, len(model.rows) + 2))
        for row in model.rows:
            table.add_row(
                literal(row["identity"]),
                literal(row.get("state") if available else "stale"),
                literal(row["streams"] if available else None),
                literal(rate(row["upload"] if available else None)),
                literal(rate(row["download"] if available else None)),
                key=row["identity"],
            )
        if model.rows:
            cursor = next(
                (
                    index
                    for index, row in enumerate(model.rows)
                    if row["identity"] == self.selected_unit
                ),
                min(cursor, len(model.rows) - 1),
            )
            table.move_cursor(row=cursor)
            self.selected_unit = model.rows[cursor]["identity"]
        self.render_details()
        events = Text("Recent events\n", style="bold")
        for entry in list(model.events)[-8:]:
            when = str(entry.get("timestamp", ""))[11:19]
            code = entry.get("code", "")
            label = EVENT_LABELS.get(code, code)
            unit = entry.get("unit")
            prefix = f"[{unit}] " if unit else ""
            events.append(f"  {when}  {prefix}{label}", style="")
            if entry.get("detail"):
                events.append(f" · {entry['detail']}")
            events.append("\n")
        self.query_one("#events", Static).update(events)

    def render_details(self) -> None:
        if not self.is_mounted or not self.query("#details"):
            return
        row = next(
            (item for item in self.dashboard.rows if item["identity"] == self.selected_unit), None
        )
        if row is None:
            self.query_one("#details", Static).update(Text("Select a VPC for details."))
            return
        table = Table.grid(padding=(0, 2))
        table.add_column()
        table.add_column(overflow="fold")
        network = row["network"]
        table.add_row(Text("Selected", style="bold"), literal(row["identity"]))
        for label, value in (
            ("Bastion", (network.get("access") or {}).get("instance_id")),
            ("AWS", f"{network.get('account', '—')} / {network.get('region', '—')}"),
            (
                "SSH trust",
                f"{row.get('ssh_trust', 'unavailable')} · {row.get('ssh_fingerprint') or '—'}",
            ),
            ("Interface", row["host"].get("interface_name")),
            ("Generation / attempt", f"{row.get('generation', '—')} / {row.get('attempt', '—')}"),
            ("Routes", ", ".join(row.get("ranges", [])) or None),
            (
                "DNS",
                f"127.0.0.1:{row.get('dns_port') or '—'} · "
                f"revision {row.get('dns_revision', '—')}",
            ),
            (
                "Resources",
                ", ".join(
                    host for item in row.get("resources", []) for host in item.get("hostnames", [])
                )
                or None,
            ),
            ("Readiness checks", row.get("checks", {}).get("result", "unavailable")),
        ):
            table.add_row(literal(label), literal(value))
        checks = (self.dashboard.snapshot or {}).get("resource_checks", {})
        expected = {
            (item["resource"], item["host"], item["port"])
            for item in targets(self.dashboard.snapshot["session"])
            if item["unit"] == row.get("unit")
        }
        current = [
            item
            for item in checks.get("endpoints", [])
            if item.get("unit") == row.get("unit")
            and all(
                item.get(key) == row.get(key) for key in ("generation", "attempt", "dns_revision")
            )
        ]
        actual = {(item.get("resource"), item.get("host"), item.get("port")) for item in current}
        status = "unavailable" if expected else "not applicable"
        if not self.dashboard.disconnected and not checks.get("stale", True) and expected:
            status = (
                "passed"
                if actual == expected and all(item.get("result") == "passed" for item in current)
                else "failed / partial"
            )
        table.add_row(
            literal("TLS/member checks"),
            literal(
                f"{status} · {len(current)}/{len(expected)} endpoints · active diagnostic probes"
            ),
        )
        if checks.get("error"):
            table.add_row(literal("Probe status"), literal(checks["error"]))
        self.query_one("#details", Static).update(table)
