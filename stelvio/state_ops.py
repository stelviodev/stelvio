"""State operations: list, remove, repair.

These operations manipulate Pulumi state directly without going through Pulumi CLI.
Used for recovery scenarios where state needs manual intervention.

Use Cases:
    - `list`: See all resources in state
    - `remove`: Stop managing a resource without deleting from AWS
    - `repair`: Fix orphaned resources, broken dependencies, and stale pending operations

IMPORTANT: remove_resource() and repair_state() mutate the state dict in place.

State Structure (Pulumi checkpoint format):
    {
        "version": 3,
        "checkpoint": {
            "stack": "...",
            "latest": {
                "resources": [
                    {
                        "urn": "urn:pulumi:stack::project::type::name",
                        "type": "aws:lambda/function:Function",
                        "parent": "urn:pulumi:...",  # optional
                        "dependencies": ["urn:pulumi:..."],  # optional
                        "propertyDependencies": {"prop": ["urn:..."]},  # optional
                        ...
                    }
                ]
            }
        }
    }
"""

from __future__ import annotations

from dataclasses import dataclass, field


def _get_deployment(state: dict) -> dict:
    """Get deployment dict from state. Creates path if missing."""
    checkpoint = state.setdefault("checkpoint", {})
    return checkpoint.setdefault("latest", {})


def _get_resources(state: dict) -> list[dict]:
    """Get resources list from state. Returns empty list if not found."""
    return _get_deployment(state).get("resources", [])


@dataclass(frozen=True)
class StateResource:
    """Resource in Pulumi state."""

    urn: str
    type: str
    name: str  # Logical name (last part of URN)
    parent: str | None = None
    dependencies: list[str] = field(default_factory=list)
    outputs: dict[str, object] = field(default_factory=dict)

    @classmethod
    def from_state(cls, resource: dict, *, include_outputs: bool = False) -> StateResource:
        urn = resource["urn"]
        # URN format: urn:pulumi:stack::project::type::name
        name = urn.split("::")[-1]
        outputs = resource.get("outputs", {}) or {} if include_outputs else {}
        return cls(
            urn=urn,
            type=resource["type"],
            name=name,
            parent=resource.get("parent"),
            dependencies=resource.get("dependencies", []),
            outputs=outputs,
        )

    @property
    def is_component(self) -> bool:
        return self.type.startswith("stelvio:")

    @property
    def component_type(self) -> str | None:
        if not self.is_component:
            return None
        return self.type.split(":")[-1]


@dataclass(frozen=True)
class StateTreeNode:
    """Tree node built from a resource in state."""

    resource: StateResource
    children: tuple[StateTreeNode, ...]


@dataclass(frozen=True)
class GroupedStateResources:
    """State resources grouped into stack, components, providers, and other roots."""

    stack: StateResource | None
    components: tuple[StateTreeNode, ...]
    providers: tuple[StateTreeNode, ...]
    other_roots: tuple[StateTreeNode, ...]


@dataclass
class Mutation:
    """A change applied to state."""

    action: str
    target_urn: str
    detail: str  # Human-readable description


def list_resources(state: dict, *, include_outputs: bool = False) -> list[StateResource]:
    """List all resources in state."""
    return [
        StateResource.from_state(r, include_outputs=include_outputs) for r in _get_resources(state)
    ]


def build_state_tree(state: dict, *, include_outputs: bool = False) -> GroupedStateResources:
    """Group state resources into stack, Stelvio component tree, and other roots."""
    resources = list_resources(state, include_outputs=include_outputs)
    if not resources:
        return GroupedStateResources(stack=None, components=(), providers=(), other_roots=())

    resources_by_urn = {resource.urn: resource for resource in resources}
    component_urns = {resource.urn for resource in resources if resource.is_component}
    provider_urns = [
        resource.urn for resource in resources if resource.type.startswith("pulumi:providers:")
    ]
    provider_urn_set = set(provider_urns)
    stack_resource = next(
        (resource for resource in resources if resource.type == "pulumi:pulumi:Stack"), None
    )

    children_by_parent: dict[str, list[str]] = {}
    for resource in resources:
        if resource.parent and resource.parent in resources_by_urn:
            children_by_parent.setdefault(resource.parent, []).append(resource.urn)

    top_level_component_urns = [
        resource.urn
        for resource in resources
        if resource.is_component
        and (resource.parent is None or resource.parent not in component_urns)
    ]

    assigned_to_components: set[str] = set()

    def collect_descendants(urn: str) -> None:
        if urn in assigned_to_components:
            return
        assigned_to_components.add(urn)
        for child_urn in children_by_parent.get(urn, []):
            collect_descendants(child_urn)

    for component_urn in top_level_component_urns:
        collect_descendants(component_urn)

    def build_node(urn: str, allowed_urns: set[str]) -> StateTreeNode:
        return StateTreeNode(
            resource=resources_by_urn[urn],
            children=tuple(
                build_node(child_urn, allowed_urns)
                for child_urn in children_by_parent.get(urn, [])
                if child_urn in allowed_urns
            ),
        )

    non_component_urns = [
        resource.urn
        for resource in resources
        if resource.urn not in assigned_to_components
        and resource.urn not in provider_urn_set
        and resource.type != "pulumi:pulumi:Stack"
    ]
    other_root_urn_set = set(non_component_urns)
    other_root_urns = [
        urn
        for urn in non_component_urns
        if resources_by_urn[urn].parent is None
        or resources_by_urn[urn].parent not in other_root_urn_set
    ]

    return GroupedStateResources(
        stack=stack_resource,
        components=tuple(
            build_node(urn, assigned_to_components) for urn in top_level_component_urns
        ),
        providers=tuple(build_node(urn, provider_urn_set) for urn in provider_urns),
        other_roots=tuple(build_node(urn, other_root_urn_set) for urn in other_root_urns),
    )


def build_state_tree_json(grouped_state: GroupedStateResources) -> dict[str, object]:
    """Build machine-readable JSON for grouped state resources."""

    def node_to_dict(node: StateTreeNode) -> dict[str, object]:
        data: dict[str, object] = {
            "name": node.resource.name,
            "urn": node.resource.urn,
            "type": node.resource.type,
            "parent": node.resource.parent,
            "dependencies": list(node.resource.dependencies),
            "children": [node_to_dict(child) for child in node.children],
        }
        if node.resource.component_type is not None:
            data["component_type"] = node.resource.component_type
        if node.resource.outputs:
            data["outputs"] = node.resource.outputs
        return data

    data: dict[str, object] = {
        "components": [node_to_dict(node) for node in grouped_state.components]
    }
    if grouped_state.stack is not None:
        stack_data: dict[str, object] = {
            "name": grouped_state.stack.name,
            "urn": grouped_state.stack.urn,
            "type": grouped_state.stack.type,
            "parent": grouped_state.stack.parent,
            "dependencies": list(grouped_state.stack.dependencies),
        }
        if grouped_state.stack.outputs:
            stack_data["outputs"] = grouped_state.stack.outputs
        data["stack"] = stack_data
    if grouped_state.providers:
        data["providers"] = [node_to_dict(node) for node in grouped_state.providers]
    if grouped_state.other_roots:
        data["other_roots"] = [node_to_dict(node) for node in grouped_state.other_roots]
    return data


def find_resource(state: dict, name: str) -> StateResource | None:
    """Find resource by name (last part of URN).

    Names are matched exactly. For ambiguous names, use the full URN.
    """
    for r in list_resources(state):
        if r.name == name:
            return r
    return None


def find_resources_by_name(state: dict, name: str) -> list[StateResource]:
    """Find all resources matching name (for ambiguity detection)."""
    return [r for r in list_resources(state) if r.name == name]


def _get_name_from_urn(urn: str) -> str:
    """Extract resource name from URN."""
    return urn.split("::")[-1]


def _pending_operation_urn(operation: dict) -> str | None:
    """Extract resource URN from a pending operation entry."""
    resource = operation.get("resource")
    if isinstance(resource, dict):
        urn = resource.get("urn")
        return urn if isinstance(urn, str) else None
    if isinstance(resource, str):
        return resource
    return None


def remove_resource(state: dict, urn: str) -> list[Mutation]:
    """Remove resource from state and return mutations applied.

    Does NOT delete from cloud - just removes from state.
    Automatically repairs dangling references after removal.
    Mutates state in place.
    """
    mutations = []
    deployment = _get_deployment(state)
    resources = deployment.get("resources", [])

    # Find and remove the resource
    new_resources = []
    removed = False
    for r in resources:
        if r["urn"] == urn:
            mutations.append(
                Mutation(
                    action="remove_resource",
                    target_urn=urn,
                    detail=f"Remove {r['type']} '{_get_name_from_urn(urn)}'",
                )
            )
            removed = True
        else:
            new_resources.append(r)

    if not removed:
        raise ValueError(f"Resource not found: {urn}")

    deployment["resources"] = new_resources

    # Repair after removal (fix dangling refs)
    repair_mutations = repair_state(state)
    mutations.extend(repair_mutations)

    return mutations


def repair_state(state: dict) -> list[Mutation]:
    """Repair state by fixing orphaned resources and broken dependencies.

    Fixes:
        1. Orphaned resources (parent doesn't exist) - removed recursively
        2. Broken dependencies (dependency doesn't exist) - removed from list
        3. Broken property dependencies - removed from list
        4. Stale pending operations - removed from checkpoint metadata

    Mutates state in place. Returns list of mutations applied.
    Safe to call multiple times (idempotent when no issues remain).
    """
    mutations = []
    deployment = _get_deployment(state)
    resources = deployment.get("resources", [])

    # Clear stale pending operations left by interrupted updates.
    pending_operations = deployment.get("pending_operations", [])
    if pending_operations:
        for operation in pending_operations:
            operation_type = operation.get("type", "unknown")
            operation_urn = _pending_operation_urn(operation)
            operation_target = (
                _get_name_from_urn(operation_urn) if operation_urn else "<unknown-resource>"
            )
            mutations.append(
                Mutation(
                    action="remove_pending_operation",
                    target_urn=operation_urn or "",
                    detail=(
                        f"Clear stale pending operation '{operation_type}' "
                        f"for '{operation_target}'"
                    ),
                )
            )
        deployment["pending_operations"] = []

    # Build set of existing URNs
    existing_urns = {r["urn"] for r in resources}

    # Track resources to remove (orphans)
    to_remove = set()

    for resource in resources:
        urn = resource["urn"]

        # Check parent - if parent missing, this is an orphan
        parent = resource.get("parent")
        if parent and parent not in existing_urns:
            to_remove.add(urn)
            mutations.append(
                Mutation(
                    action="remove_resource",
                    target_urn=urn,
                    detail=f"Remove orphan '{_get_name_from_urn(urn)}' "
                    f"(parent '{_get_name_from_urn(parent)}' missing)",
                )
            )
            continue  # Skip dependency checks for resources being removed

        # Check dependencies
        deps = resource.get("dependencies", [])
        valid_deps = [d for d in deps if d in existing_urns]
        if len(valid_deps) != len(deps):
            removed_deps = set(deps) - set(valid_deps)
            mutations.extend(
                Mutation(
                    action="remove_dependency",
                    target_urn=urn,
                    detail=f"Remove broken dependency '{_get_name_from_urn(dep)}' "
                    f"from '{_get_name_from_urn(urn)}'",
                )
                for dep in removed_deps
            )
            resource["dependencies"] = valid_deps

        # Check propertyDependencies
        prop_deps = resource.get("propertyDependencies", {})
        for prop, prop_dep_urns in list(prop_deps.items()):
            valid_prop_deps = [d for d in prop_dep_urns if d in existing_urns]
            if len(valid_prop_deps) != len(prop_dep_urns):
                removed = set(prop_dep_urns) - set(valid_prop_deps)
                mutations.extend(
                    Mutation(
                        action="remove_property_dependency",
                        target_urn=urn,
                        detail=f"Remove broken property dependency "
                        f"'{_get_name_from_urn(dep)}' from "
                        f"'{_get_name_from_urn(urn)}.{prop}'",
                    )
                    for dep in removed
                )
                if valid_prop_deps:
                    prop_deps[prop] = valid_prop_deps
                else:
                    del prop_deps[prop]

    # Remove orphans
    if to_remove:
        deployment["resources"] = [r for r in resources if r["urn"] not in to_remove]
        # Recursively repair (removing orphan may create new orphans)
        more_mutations = repair_state(state)
        mutations.extend(more_mutations)

    return mutations
