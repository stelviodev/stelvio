"""Literal, complete tables for the canonical diagnostic report."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.table import Table
from rich.text import Text

if TYPE_CHECKING:
    from rich.console import Console

MIN_COLUMN_WIDTH = 22
MAX_COLUMNS = 6


def _label(name: str) -> str:
    return name.replace("_", " ").title()


def _scalar(value: object) -> bool:
    return not isinstance(value, (dict, list, tuple)) or (
        isinstance(value, (list, tuple))
        and all(not isinstance(item, (dict, list, tuple)) for item in value)
    )


def _text(value: object) -> Text:
    if value is None:
        return Text("Unavailable")
    if isinstance(value, bool):
        return Text("Yes" if value else "No")
    if isinstance(value, (list, tuple)):
        return Text(", ".join(_text(item).plain for item in value) if value else "None")
    return Text(str(value))


def _table(title: str, columns: list[str]) -> Table:
    table = Table(title=Text(title), show_lines=True)
    for name in columns:
        table.add_column(Text(_label(name)), overflow="fold")
    return table


def _child_title(title: str, key: str) -> str:
    label = _label(key)
    return title if title.rsplit(" · ", 1)[-1] == label else f"{title} · {label}"


def _render_mapping(value: dict, console: Console, title: str) -> None:
    fields = {key: item for key, item in value.items() if _scalar(item)}
    if fields:
        table = _table(title, ["Field", "Value"])
        for key, item in fields.items():
            table.add_row(
                Text(_label(key)), Text("None") if key == "error" and item is None else _text(item)
            )
        console.print(table)
    elif not value:
        console.print(Text(f"{title}: None"))
    for key, item in value.items():
        if not _scalar(item):
            _render_value(item, console, _child_title(title, key))


def _render_records(value: list[dict] | tuple[dict, ...], console: Console, title: str) -> None:
    nested = {key for row in value for key, item in row.items() if not _scalar(item)}
    columns = list(dict.fromkeys(key for row in value for key in row if key not in nested))
    if columns:
        identity = next(
            (
                key
                for key in (
                    "name",
                    "destination",
                    "address",
                    "number",
                    "unit",
                    "session_id",
                    "pid",
                    "code",
                    "service",
                )
                if key in columns
            ),
            columns[0],
        )
        fields = [key for key in columns if key != identity]
        # Split wide records into related tables, repeating the row identity.
        # This keeps all values readable instead of squeezing a dozen columns
        # into single-character cells on a narrow terminal.
        group_size = max(1, min(MAX_COLUMNS, console.width // MIN_COLUMN_WIDTH) - 1)
        groups = [
            fields[index : index + group_size] for index in range(0, len(fields), group_size)
        ] or [[]]
        for group in groups:
            selected = [identity, *group]
            table = _table(title, selected)
            for row in value:
                table.add_row(*(_text(row.get(key)) for key in selected))
            console.print(table)
    for index, row in enumerate(value, 1):
        identity = next(
            (
                row[key]
                for key in ("name", "session_id", "unit", "service", "pid")
                if row.get(key) is not None
            ),
            index,
        )
        for key, item in row.items():
            if key in nested:
                _render_value(item, console, f"{title} · {identity} · {_label(key)}")


def _render_value(value: object, console: Console, title: str) -> None:
    if isinstance(value, dict):
        _render_mapping(value, console, title)
    elif (
        isinstance(value, (list, tuple))
        and value
        and all(isinstance(item, dict) for item in value)
    ):
        _render_records(value, console, title)
    elif isinstance(value, (list, tuple)) and not _scalar(value):
        for index, item in enumerate(value, 1):
            _render_value(item, console, f"{title} · {index}")
    else:
        table = _table(title, ["Value"])
        table.add_row(_text(value))
        console.print(table)


def render(report: dict, console: Console) -> None:
    console.print(Text("Stelvio tunnel diagnostics", style="bold"))
    console.print(
        Text(f"Generated {report['generated_at']} | {report['duration_seconds']} seconds")
    )
    console.print(Text("Local report: resource identities and network addresses are included."))
    if report.get("truncated"):
        console.print(Text("Report incomplete: the report size limit was reached."))
    for name, section in sorted(report["sections"].items()):
        title = _label(name)
        # Render observation metadata separately from its typed data. The same
        # recursion also handles nested observations without repr()/Pretty dumps.
        _render_mapping(
            {key: item for key, item in section.items() if key != "data"},
            console,
            f"{title} · Collection",
        )
        _render_value(section.get("data"), console, title)
    if report["findings"]:
        _render_value(report["findings"], console, "Findings")
