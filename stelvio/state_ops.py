"""State operations: list, remove, repair.

These operations manipulate Pulumi state directly without going through Pulumi CLI.
Used for recovery scenarios where state needs manual intervention.

Use Cases:
    - `list`: See all resources in state
    - `remove`: Stop managing a resource without deleting from AWS
    - `repair`: Fix orphaned resources and broken dependencies after manual edits

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

from dataclasses import dataclass, field


def _get_deployment(state: dict) -> dict:
    """Get deployment dict from state. Creates path if missing."""
    checkpoint = state.setdefault("checkpoint", {})
    return checkpoint.setdefault("latest", {})


def _get_resources(state: dict) -> list[dict]:
    """Get resources list from state. Returns empty list if not found."""
    return _get_deployment(state).get("resources", [])


@dataclass
class StateResource:
    """Resource in Pulumi state."""

    urn: str
    type: str
    name: str  # Logical name (last part of URN)
    parent: str | None = None
    dependencies: list[str] = field(default_factory=list)

    @classmethod
    def from_state(cls, resource: dict) -> "StateResource":
        urn = resource["urn"]
        # URN format: urn:pulumi:stack::project::type::name
        name = urn.split("::")[-1]
        return cls(
            urn=urn,
            type=resource["type"],
            name=name,
            parent=resource.get("parent"),
            dependencies=resource.get("dependencies", []),
        )


@dataclass
class Mutation:
    """A change applied to state."""

    action: str  # "remove_resource", "remove_dependency", "remove_property_dependency"
    target_urn: str
    detail: str  # Human-readable description


def list_resources(state: dict) -> list[StateResource]:
    """List all resources in state."""
    return [StateResource.from_state(r) for r in _get_resources(state)]


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

    Mutates state in place. Returns list of mutations applied.
    Safe to call multiple times (idempotent when no issues remain).
    """
    mutations = []
    deployment = _get_deployment(state)
    resources = deployment.get("resources", [])

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
