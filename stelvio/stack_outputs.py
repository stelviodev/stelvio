from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from stelvio.state_ops import list_resources

if TYPE_CHECKING:
    from collections.abc import MutableMapping

    from pulumi.automation import OutputValue


_EXPORT_PREFIXES_BY_COMPONENT_TYPE: dict[str, str] = {
    "Api": "api",
    "AppSync": "appsync",
    "Bucket": "s3bucket",
    "CloudFrontDistribution": "cloudfront",
    "Cron": "cron",
    "DynamoTable": "dynamotable",
    "Email": "email",
    "Function": "function",
    "Layer": "layer",
    "Queue": "queue",
    "Router": "router",
    "S3StaticWebsite": "s3_static_website",
    "Topic": "topic",
}


@dataclass(frozen=True)
class DeployedComponent:
    urn: str
    type_name: str
    name: str
    export_prefix: str

    @property
    def display_name(self) -> str:
        return f"{self.type_name}/{self.name}"

    @property
    def output_prefix(self) -> str:
        return f"{self.export_prefix}_{self.name}_"


@dataclass(frozen=True)
class OutputEntry:
    key: str
    attribute: str
    output: OutputValue


@dataclass(frozen=True)
class ComponentOutputGroup:
    component: DeployedComponent
    outputs: tuple[OutputEntry, ...]


@dataclass(frozen=True)
class GroupedStackOutputs:
    components: tuple[ComponentOutputGroup, ...]
    user_defined: tuple[OutputEntry, ...]

    def has_outputs(self) -> bool:
        return bool(self.components or self.user_defined)


def _value_for_display(output: OutputValue) -> str:
    return "[secret]" if output.secret else str(output.value)


def get_deployed_components(state: dict | None) -> list[DeployedComponent]:
    if not state:
        return []

    components: list[DeployedComponent] = []
    for resource in list_resources(state):
        if not resource.type.startswith("stelvio:"):
            continue

        type_name = resource.type.split(":")[-1]
        export_prefix = _EXPORT_PREFIXES_BY_COMPONENT_TYPE.get(type_name)
        if export_prefix is None:
            continue

        components.append(
            DeployedComponent(
                urn=resource.urn,
                type_name=type_name,
                name=resource.name,
                export_prefix=export_prefix,
            )
        )

    return components


def group_stack_outputs(
    outputs: MutableMapping[str, OutputValue],
    state: dict | None,
    *,
    component_name: str | None = None,
) -> GroupedStackOutputs:
    deployed_components = get_deployed_components(state)
    matchers = sorted(
        ((component.output_prefix, component) for component in deployed_components),
        key=lambda item: len(item[0]),
        reverse=True,
    )

    grouped: dict[str, list[OutputEntry]] = {
        component.urn: [] for component in deployed_components
    }
    user_defined: list[OutputEntry] = []

    for key, output in outputs.items():
        matched_component: DeployedComponent | None = None
        matched_prefix: str | None = None
        for prefix, component in matchers:
            if key.startswith(prefix):
                matched_component = component
                matched_prefix = prefix
                break

        if matched_component is None:
            if component_name is None:
                user_defined.append(OutputEntry(key=key, attribute=key, output=output))
            continue

        if component_name is not None and matched_component.name != component_name:
            continue

        grouped[matched_component.urn].append(
            OutputEntry(
                key=key,
                attribute=key[len(matched_prefix) :],
                output=output,
            )
        )

    component_groups = tuple(
        ComponentOutputGroup(
            component=component,
            outputs=tuple(sorted(grouped[component.urn], key=lambda entry: entry.attribute)),
        )
        for component in deployed_components
        if grouped[component.urn] and (component_name is None or component.name == component_name)
    )

    return GroupedStackOutputs(
        components=component_groups,
        user_defined=tuple(sorted(user_defined, key=lambda entry: entry.key)),
    )


def flatten_grouped_outputs(grouped_outputs: GroupedStackOutputs) -> list[tuple[str, OutputValue]]:
    flattened: list[tuple[str, OutputValue]] = []
    for group in grouped_outputs.components:
        flattened.extend((entry.key, entry.output) for entry in group.outputs)
    flattened.extend((entry.key, entry.output) for entry in grouped_outputs.user_defined)
    return flattened


def format_flat_outputs(
    outputs: MutableMapping[str, OutputValue],
    state: dict | None,
    *,
    component_name: str | None = None,
) -> list[str]:
    grouped_outputs = group_stack_outputs(outputs, state, component_name=component_name)
    return [
        f"[cyan]{key}[/cyan]: {_value_for_display(output)}"
        for key, output in flatten_grouped_outputs(grouped_outputs)
    ]


def format_grouped_outputs(grouped_outputs: GroupedStackOutputs) -> list[str]:
    if not grouped_outputs.has_outputs():
        return []

    lines = ["", "[bold]Outputs:"]
    for group in grouped_outputs.components:
        lines.append(f"  [bold]{group.component.type_name}[/bold]  {group.component.name}")
        max_attribute_length = max(len(entry.attribute) for entry in group.outputs)
        lines.extend(
            [
                f"    [cyan]{entry.attribute.ljust(max_attribute_length)}[/cyan]  "
                f"{_value_for_display(entry.output)}"
                for entry in group.outputs
            ]
        )

    if grouped_outputs.user_defined:
        lines.append("  [bold]User defined[/bold]")
        max_key_length = max(len(entry.key) for entry in grouped_outputs.user_defined)
        lines.extend(
            [
                f"    [cyan]{entry.key.ljust(max_key_length)}[/cyan]  "
                f"{_value_for_display(entry.output)}"
                for entry in grouped_outputs.user_defined
            ]
        )

    lines.append("")
    return lines


def build_grouped_outputs_json(grouped_outputs: GroupedStackOutputs) -> dict[str, object]:
    components = {
        group.component.name: {
            entry.attribute: "[secret]" if entry.output.secret else entry.output.value
            for entry in group.outputs
        }
        for group in grouped_outputs.components
    }
    user_defined = {
        entry.key: "[secret]" if entry.output.secret else entry.output.value
        for entry in grouped_outputs.user_defined
    }

    data: dict[str, object] = {"components": components}
    if user_defined:
        data["user_defined"] = user_defined
    return data


def build_flat_outputs_json(
    outputs: MutableMapping[str, OutputValue],
    state: dict | None,
    *,
    component_name: str | None = None,
) -> dict[str, object]:
    grouped_outputs = group_stack_outputs(outputs, state, component_name=component_name)
    return {
        key: "[secret]" if output.secret else output.value
        for key, output in flatten_grouped_outputs(grouped_outputs)
    }
