"""Mutable owner for an independent temporary AWS access lifecycle.

The process supervisor supplies the operation runner and stopped-engine proof.
This owner never evaluates the application or pushes its checkpoint.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Protocol

from stelvio.tunnel.access_cleanup import AccessCleanup
from stelvio.tunnel.access_metadata import AccessMetadata
from stelvio.tunnel.access_program import access_program, prepare_access_context
from stelvio.tunnel.engine import TrackedPulumiCommand
from stelvio.tunnel.manifest import AccessDescriptor
from stelvio.tunnel.processes import identity

if TYPE_CHECKING:
    from collections.abc import Callable

    from pulumi.automation import Stack

    from stelvio.tunnel.access_backend import AccessBackend
    from stelvio.tunnel.access_inventory import AccessInventory
    from stelvio.tunnel.access_program import CapturedCredentials
    from stelvio.tunnel.access_state import AccessJournal

_OWNED_TYPES = frozenset(
    {
        "pulumi:pulumi:Stack",
        "pulumi:providers:aws",
        "aws:ec2/instance:Instance",
        "aws:ec2/securityGroup:SecurityGroup",
        "aws:vpc/securityGroupEgressRule:SecurityGroupEgressRule",
        "aws:vpc/securityGroupIngressRule:SecurityGroupIngressRule",
        "aws:iam/role:Role",
        "aws:iam/rolePolicy:RolePolicy",
        "aws:iam/instanceProfile:InstanceProfile",
        "aws:ssm/document:Document",
    }
)


class OperationRunner(Protocol):
    def __call__[T](self, label: str, operation: Callable[[], T]) -> T: ...


class AccessUnit:
    def __init__(  # noqa: PLR0913 - distinct lifecycle dependencies and process-proof boundary
        self,
        journal: AccessJournal,
        backend: AccessBackend,
        inventory: AccessInventory,
        credentials: CapturedCredentials,
        run: OperationRunner,
        require_stopped: Callable[[], None],
    ) -> None:
        self.journal = journal
        self.backend = backend
        self.inventory = inventory
        self.credentials = credentials
        self.run = run
        self.require_stopped = require_stopped
        self.stack: Stack | None = None
        self._disposed = False

    def _engine[T](self, label: str, operation: Callable[[], T]) -> T:
        self.require_stopped()
        self.backend.refresh_credentials(self.stack)
        return self.run(label, operation)

    def _require_runner(self) -> None:
        if (
            not isinstance(self.backend.command, TrackedPulumiCommand)
            or self.backend.command.provider_credentials is None
        ):
            raise TypeError("Temporary access requires the registered native actor runner")

    def start(self) -> AccessDescriptor:
        self._require_runner()
        prepare_access_context(self.journal.intent.region)
        self.journal.claim(identity(os.getpid()))
        self.backend.command.registry.bind()
        self.inventory.validate_owner()
        if self.journal.read("creation-started.json") is not None:
            raise RuntimeError("Temporary access creation already attempted; recover before reuse")
        self.stack = self.run(
            "backend-create",
            lambda: self.backend.open(
                lambda: access_program(self.journal, self.credentials), create=True
            ),
        )
        # The backend and recovery key already have their independent owner.
        # Intent precedes the very first resource create, including lost returns.
        self.journal.record("creation-started.json", {"started": True})
        self._engine("access-create", self.stack.up)
        self.require_stopped()
        descriptor = self._engine("access-outputs", self.stack.outputs)["access"].value
        required = {"instance_id", "security_group_id", "az", "identity_document", "owner"}
        if not isinstance(descriptor, dict) or descriptor.keys() != required:
            raise RuntimeError("Malformed temporary access outputs; retain recovery records")
        actual = self.inventory.observe()
        if (
            descriptor["instance_id"] != actual["instance"]
            or descriptor["security_group_id"] != actual["group"]
            or descriptor["az"] != self.journal.intent.availability_zone
            or descriptor["owner"] != self.journal.intent.session
            or not actual["document"]
            or descriptor["identity_document"] != actual["document"]["name"]
        ):
            raise RuntimeError("Temporary access outputs differ from observed AWS ownership")
        self.journal.record(
            "creation-finished.json", {"descriptor": descriptor, "observed": actual}
        )
        return AccessDescriptor(
            *(
                descriptor[key]
                for key in (
                    "instance_id",
                    "security_group_id",
                    "az",
                    "identity_document",
                    "owner",
                )
            )
        )

    def _checkpoint(self) -> dict:
        checkpoint = self._engine("access-checkpoint", self.stack.export_stack).deployment
        records = checkpoint.get("resources", []) + [
            operation["resource"] for operation in checkpoint.get("pending_operations", [])
        ]
        provider_reference = self._provider_reference(records)
        observed = self.inventory.observe()
        saved = self.journal.read("cleanup-observed.json")
        finished = self.journal.read("creation-finished.json")
        inventories = [observed]
        if saved:
            inventories.append(saved)
        if finished and finished.get("observed"):
            inventories.append(finished["observed"])
        for resource in records:
            kind = resource.get("type")
            if kind not in _OWNED_TYPES:
                raise RuntimeError("Unexpected resource in temporary backend; cleanup refused")
            if kind in {"pulumi:pulumi:Stack", "pulumi:providers:aws"}:
                continue
            if resource.get("provider") != provider_reference or provider_reference is None:
                raise RuntimeError("Unexpected provider reference in temporary backend")
            inputs = resource.get("inputs", {})
            if kind == "aws:iam/rolePolicy:RolePolicy":
                if inputs.get("role") != self.journal.intent.name + "-role":
                    raise RuntimeError("Foreign role policy in temporary backend; cleanup refused")
            elif any(
                inputs.get("tags", {}).get(key) != value
                for key, value in self.journal.intent.tags.items()
            ):
                raise RuntimeError("Foreign ownership in temporary backend; cleanup refused")
            self._physical_identity(resource, inventories)
        return checkpoint

    def _physical_identity(self, resource: dict, inventories: list[dict]) -> None:
        """Tags on inputs never authorize a different physical AWS resource."""
        identity = resource.get("id")
        if not identity:
            # A pending create has no physical handle to refresh or delete.
            # Its unrecorded AWS effects still require SDK reconciliation.
            return
        kind = resource["type"]
        inputs = resource.get("inputs", {})
        outputs = resource.get("outputs", {})
        intent = self.journal.intent
        field = {
            "aws:ec2/instance:Instance": "instance",
            "aws:ec2/securityGroup:SecurityGroup": "group",
            "aws:vpc/securityGroupEgressRule:SecurityGroupEgressRule": "rules",
            "aws:vpc/securityGroupIngressRule:SecurityGroupIngressRule": "rules",
        }.get(kind)
        if field:
            matches = any(
                identity in inventory[field] if field == "rules" else identity == inventory[field]
                for inventory in inventories
            )
        elif kind in {"aws:iam/role:Role", "aws:iam/instanceProfile:InstanceProfile"}:
            field = "role" if kind == "aws:iam/role:Role" else "profile"
            matches = identity == intent.name + "-" + field and any(
                inventory[field]
                and (not outputs.get("uniqueId") or outputs["uniqueId"] == inventory[field])
                for inventory in inventories
            )
        elif kind == "aws:iam/rolePolicy:RolePolicy":
            role, separator, policy = identity.partition(":")
            matches = (
                role == intent.name + "-role"
                and separator == ":"
                and policy
                and outputs.get("name", policy) == policy
                and json.loads(inputs.get("policy", "null"))
                == json.loads(intent.ssm_policy_content)
                and any(inventory["role"] for inventory in inventories)
            )
        else:
            matches = any(
                inventory["document"]
                and identity == inventory["document"]["name"]
                and (not outputs.get("hash") or outputs["hash"] == inventory["document"]["hash"])
                for inventory in inventories
            )
        if not matches:
            raise RuntimeError("Foreign physical identity in temporary backend; cleanup refused")

    def _provider_reference(self, records: list[dict]) -> str | None:
        providers = [
            resource for resource in records if resource.get("type") == "pulumi:providers:aws"
        ]
        if len(providers) > 1:
            raise RuntimeError("Unexpected providers in temporary backend; cleanup refused")
        provider_reference = None
        if providers:
            provider = providers[0]
            inputs = provider.get("inputs", {})
            accounts = inputs.get("allowedAccountIds", "[]")
            if isinstance(accounts, str):
                accounts = json.loads(accounts)
            if (
                inputs.get("region") != self.journal.intent.region
                or accounts != [self.journal.intent.account]
                or any(key in inputs for key in ("accessKey", "secretKey", "token", "profile"))
            ):
                raise RuntimeError("Temporary provider context differs; cleanup refused")
            if provider.get("id"):
                provider_reference = f"{provider['urn']}::{provider['id']}"
        return provider_reference

    def stop(self) -> None:
        self.require_stopped()
        if self._disposed:
            return
        if self.journal.read("metadata-cleanup.json") is not None:
            AccessMetadata(self.journal, self.require_stopped).remove_records()
            self._disposed = True
            return
        self.journal.require_claim()
        self.inventory.validate_owner()
        if self.journal.read("backend-cleaned.json") == {"cleaned": True}:
            self._dispose()
            return
        if self.journal.read("creation-started.json") is None:
            # Backend initialization may leave a key or an empty stack, but no
            # access resource program can have run before this durable boundary.
            # Contradictory SDK effects must be retained rather than adopted.
            if self.journal.read("creation-finished.json") or any(
                self.inventory.observe().values()
            ):
                raise RuntimeError("Unattempted access has AWS effects; cleanup refused")
            self.journal.record("backend-cleaned.json", {"cleaned": True})
            self._dispose()
            return
        self._require_runner()
        if self.stack is None:
            self.stack = self.run("backend-select", self.backend.open)
        # Cancel only this exact owner namespace, after the supervisor's proof.
        self._engine("access-cancel", self.stack.cancel)
        self._checkpoint()
        if self.journal.read("creation-started.json") is not None:
            AccessCleanup(self.inventory, self.require_stopped).remove()
        self._checkpoint()
        # Pending creates are cleared only after SDK reconciliation has disposed
        # of the AWS effects and confirmed known-ID absence.
        self._engine("access-refresh", lambda: self.stack.refresh(clear_pending_creates=True))
        self._checkpoint()
        self._engine("access-destroy", self.stack.destroy)
        checkpoint = self._checkpoint()
        if checkpoint.get("pending_operations") or any(
            resource.get("custom") for resource in checkpoint.get("resources", [])
        ):
            raise RuntimeError("Temporary backend resources remain; retain recovery records")
        self.journal.record("backend-cleaned.json", {"cleaned": True})
        self._dispose()

    def _dispose(self) -> None:
        self.require_stopped()
        metadata = AccessMetadata(self.journal, self.require_stopped)
        metadata.remove_backend_versions()
        self.backend.remove_key()
        metadata.remove_records()
        self.journal.release()
        self._disposed = True
