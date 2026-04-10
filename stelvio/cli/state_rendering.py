from textwrap import wrap

from rich.markup import escape

from stelvio.state_ops import GroupedStateResources, StateTreeNode


def wrap_state_value(
    *, prefix: str, prefix_visible: str, value: str, width: int, style: str | None = None
) -> list[str]:
    available_width = max(width - len(prefix_visible), 10)
    value = escape(value)
    wrapped = wrap(
        value,
        width=available_width,
        break_long_words=False,
        break_on_hyphens=True,
    ) or [value]
    first_value = wrapped[0] if style is None else f"[{style}]{wrapped[0]}[/{style}]"
    lines = [f"{prefix}{first_value}"]
    continuation_prefix = " " * len(prefix_visible)
    for part in wrapped[1:]:
        continuation_value = part if style is None else f"[{style}]{part}[/{style}]"
        lines.append(f"{continuation_prefix}{continuation_value}")
    return lines


def format_state_node_lines(node: StateTreeNode, indent: int, *, width: int) -> list[str]:
    pad = "  " * indent
    if node.resource.component_type is not None:
        lines = wrap_state_value(
            prefix=f"{pad}[bold]{node.resource.component_type}[/bold] ",
            prefix_visible=f"{pad}{node.resource.component_type} ",
            value=node.resource.name,
            width=width,
        )
    else:
        lines = wrap_state_value(
            prefix=pad, prefix_visible=pad, value=node.resource.name, width=width, style="cyan"
        )

    lines.append(f"{pad}  Type: {node.resource.type}")
    if node.resource.outputs:
        for key, value in sorted(node.resource.outputs.items()):
            lines.extend(
                wrap_state_value(
                    prefix=f"{pad}  [dim]{key}:[/dim] ",
                    prefix_visible=f"{pad}  {key}: ",
                    value=str(value),
                    width=width,
                )
            )
    if node.resource.dependencies:
        dependency_names = [
            dependency.split("::")[-1] for dependency in node.resource.dependencies
        ]
        lines.extend(
            wrap_state_value(
                prefix=f"{pad}  Depends on: ",
                prefix_visible=f"{pad}  Depends on: ",
                value=", ".join(dependency_names),
                width=width,
            )
        )
    for child in node.children:
        lines.extend(format_state_node_lines(child, indent + 1, width=width))
    return lines


def _append_state_section(
    lines: list[str], title: str, nodes: tuple[StateTreeNode, ...], *, indent: int, width: int
) -> None:
    if not nodes:
        return

    lines.append(title)
    for node in nodes:
        lines.extend(format_state_node_lines(node, indent, width=width))
        lines.append("")


def format_state_tree_lines(grouped_state: GroupedStateResources, *, width: int) -> list[str]:
    lines: list[str] = []

    if grouped_state.stack is not None:
        lines.append(f"[bold]Stack[/bold] {grouped_state.stack.name}")
        if grouped_state.stack.outputs:
            for key, value in sorted(grouped_state.stack.outputs.items()):
                lines.extend(
                    wrap_state_value(
                        prefix=f"  [dim]{key}:[/dim] ",
                        prefix_visible=f"  {key}: ",
                        value=str(value),
                        width=width,
                    )
                )
        for node in grouped_state.components:
            lines.extend(format_state_node_lines(node, 1, width=width))
            lines.append("")
    else:
        for node in grouped_state.components:
            lines.extend(format_state_node_lines(node, 0, width=width))
            lines.append("")

    _append_state_section(
        lines, "[bold]Providers[/bold]", grouped_state.providers, indent=1, width=width
    )
    _append_state_section(
        lines, "[bold]Other roots[/bold]", grouped_state.other_roots, indent=1, width=width
    )

    if lines and lines[-1] == "":
        lines.pop()
    return lines
