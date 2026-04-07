from __future__ import annotations

from dataclasses import dataclass, field
from shutil import get_terminal_size
from textwrap import wrap
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import MutableMapping

    from pulumi.automation import OutputValue


@dataclass(frozen=True)
class DeployedComponent:
    """A Stelvio component found in Pulumi state."""

    urn: str
    type_token: str
    type_name: str
    name: str
    parent_urn: str | None
    outputs: dict[str, object] = field(default_factory=dict)

    @property
    def display_name(self) -> str:
        return f"{self.type_name}/{self.name}"


@dataclass(frozen=True)
class OutputEntry:
    key: str
    value: object
    secret: bool = False

    @property
    def display_value(self) -> str:
        return "[secret]" if self.secret else str(self.value)


@dataclass(frozen=True)
class ComponentOutputGroup:
    component: DeployedComponent
    outputs: tuple[OutputEntry, ...]
    children: tuple[ComponentOutputGroup, ...] = ()


@dataclass(frozen=True)
class GroupedOutputs:
    components: tuple[ComponentOutputGroup, ...]
    user_defined: tuple[OutputEntry, ...]

    def has_outputs(self) -> bool:
        return bool(self.components or self.user_defined)


def _state_resources(state: dict | None) -> list[dict]:
    """Extract raw resource dicts from Pulumi state."""
    if not state:
        return []
    deployment = state.get("checkpoint", state).get("latest", {})
    return deployment.get("resources", [])


def get_deployed_components(state: dict | None) -> list[DeployedComponent]:
    """Read Stelvio components and their outputs from Pulumi state."""
    resources = _state_resources(state)
    component_urns = {r["urn"] for r in resources if r["type"].startswith("stelvio:")}

    components: list[DeployedComponent] = []
    for resource in resources:
        if not resource["type"].startswith("stelvio:"):
            continue

        urn = resource["urn"]
        type_token = resource["type"]
        type_name = type_token.split(":")[-1]
        name = urn.split("::")[-1]
        parent = resource.get("parent")
        parent_urn = parent if parent in component_urns else None

        # Read display outputs (non-_-prefixed keys from register_outputs)
        raw_outputs = resource.get("outputs", {}) or {}
        display_outputs = {k: v for k, v in raw_outputs.items() if not k.startswith("_")}

        components.append(
            DeployedComponent(
                urn=urn,
                type_token=type_token,
                type_name=type_name,
                name=name,
                parent_urn=parent_urn,
                outputs=display_outputs,
            )
        )

    return components


def _build_tree(
    components: list[DeployedComponent],
) -> tuple[dict[str, DeployedComponent], dict[str, list[str]], list[str]]:
    """Build parent-child relationships. Returns (by_urn, children_by_parent, root_urns)."""
    by_urn = {c.urn: c for c in components}
    children: dict[str, list[str]] = {c.urn: [] for c in components}
    roots: list[str] = []

    for c in components:
        if c.parent_urn and c.parent_urn in children:
            children[c.parent_urn].append(c.urn)
        else:
            roots.append(c.urn)

    return by_urn, children, roots


def _build_groups(
    by_urn: dict[str, DeployedComponent],
    children_by_parent: dict[str, list[str]],
    root_urns: list[str],
) -> tuple[ComponentOutputGroup, ...]:
    """Build component output groups, only including components with display outputs."""

    def build(urn: str) -> ComponentOutputGroup | None:
        component = by_urn[urn]
        child_groups = tuple(
            g for child_urn in children_by_parent[urn] for g in [build(child_urn)] if g is not None
        )
        entries = tuple(OutputEntry(key=k, value=v) for k, v in sorted(component.outputs.items()))

        if not entries and not child_groups:
            return None

        return ComponentOutputGroup(component=component, outputs=entries, children=child_groups)

    return tuple(g for urn in root_urns for g in [build(urn)] if g is not None)


def _user_defined_entries(
    stack_outputs: MutableMapping[str, OutputValue] | None,
) -> tuple[OutputEntry, ...]:
    """Extract user-defined exports from stack outputs."""
    if not stack_outputs:
        return ()

    return tuple(
        OutputEntry(
            key=key,
            value=output.value,
            secret=output.secret,
        )
        for key, output in sorted(stack_outputs.items())
    )


def group_outputs(
    state: dict | None,
    stack_outputs: MutableMapping[str, OutputValue] | None = None,
) -> GroupedOutputs:
    """Build grouped outputs from state (component values) and stack outputs (user exports)."""
    components = get_deployed_components(state)
    by_urn, children_by_parent, root_urns = _build_tree(components)
    component_groups = _build_groups(by_urn, children_by_parent, root_urns)
    user_defined = _user_defined_entries(stack_outputs)

    return GroupedOutputs(components=component_groups, user_defined=user_defined)


# Display formatting


def _output_display_width() -> int:
    return get_terminal_size((100, 20)).columns


def _format_value_lines(key: str, value: str, *, key_width: int, indent_spaces: int) -> list[str]:
    key_markup = f"[cyan]{key.ljust(key_width)}[/cyan]"
    indent = " " * indent_spaces
    inline_prefix = f"{indent}{key_markup}  "
    value_indent = " " * (indent_spaces + key_width + 2)
    inline_width = indent_spaces + key_width + 2
    terminal_width = _output_display_width()

    if inline_width + len(value) <= terminal_width:
        return [f"{inline_prefix}{value}"]

    wrap_width = max(terminal_width - len(value_indent), 20)
    wrapped = wrap(
        value,
        width=wrap_width,
        break_long_words=True,
        break_on_hyphens=False,
        drop_whitespace=False,
        replace_whitespace=False,
    )
    if not wrapped:
        return [inline_prefix]

    return [
        f"{inline_prefix}{wrapped[0]}",
        *(f"{value_indent}{part}" for part in wrapped[1:]),
    ]


def _render_component(lines: list[str], group: ComponentOutputGroup, *, level: int) -> None:
    indent = "  " * (1 + level)
    lines.append(f"{indent}[bold]{group.component.type_name}[/bold] {group.component.name}")

    if group.outputs:
        max_key_len = max(len(e.key) for e in group.outputs)
        output_indent = 4 + (2 * level)
        for entry in group.outputs:
            lines.extend(
                _format_value_lines(
                    entry.key,
                    entry.display_value,
                    key_width=max_key_len,
                    indent_spaces=output_indent,
                )
            )

    for child in group.children:
        _render_component(lines, child, level=level + 1)


def format_outputs(grouped: GroupedOutputs) -> list[str]:
    """Format grouped outputs for human-readable display."""
    if not grouped.has_outputs():
        return []

    lines: list[str] = ["", "[bold]Outputs:"]
    for group in grouped.components:
        _render_component(lines, group, level=0)

    if grouped.user_defined:
        lines.append("  [bold]User defined[/bold]")
        max_key_len = max(len(e.key) for e in grouped.user_defined)
        for entry in grouped.user_defined:
            lines.extend(
                _format_value_lines(
                    entry.key, entry.display_value, key_width=max_key_len, indent_spaces=4
                )
            )

    return lines


# JSON output


def _component_json(group: ComponentOutputGroup) -> dict[str, object]:
    data: dict[str, object] = {
        "type": group.component.type_name,
        "name": group.component.name,
    }
    if group.outputs:
        data["outputs"] = {e.key: e.value for e in group.outputs}
    if group.children:
        data["components"] = [_component_json(child) for child in group.children]
    return data


def build_outputs_json(grouped: GroupedOutputs) -> dict[str, object]:
    """Build JSON representation of outputs."""
    data: dict[str, object] = {}
    if grouped.components:
        data["components"] = [_component_json(g) for g in grouped.components]
    if grouped.user_defined:
        data["user_defined"] = {
            e.key: e.display_value if e.secret else e.value for e in grouped.user_defined
        }
    return data
