"""Compose P2 ownership inside the protected nonroot runtime, before handlers."""

from __future__ import annotations

from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING

from stelvio.tunnel.access_backend import AccessBackend
from stelvio.tunnel.access_inventory import AccessInventory
from stelvio.tunnel.access_state import AccessJournal, plan_access
from stelvio.tunnel.access_unit import AccessUnit
from stelvio.tunnel.actors import ActorRegistry
from stelvio.tunnel.bastion import AMI_PARAMETER
from stelvio.tunnel.credentials import AWS_IO
from stelvio.tunnel.engine import TrackedPulumiCommand

if TYPE_CHECKING:
    from stelvio.tunnel.credentials import AwsExecutionContext
    from stelvio.tunnel.manifest import AccessDescriptor, SessionDescription, VpcNetwork

# Only Pulumi access programs serialize; network/DNS health runs independently.
_ACCESS_PROGRAM = Lock()


class RuntimeAccess:
    def __init__(
        self,
        description: SessionDescription,
        network: VpcNetwork,
        target: AwsExecutionContext,
        home: AwsExecutionContext,
        settings: dict,
    ) -> None:
        target_session = target.open_session()
        home_session = home.open_session()
        ami = target_session.client("ssm", config=AWS_IO).get_parameter(Name=AMI_PARAMETER)[
            "Parameter"
        ]["Value"]
        intent = plan_access(description, network, ami)
        self.journal = AccessJournal(
            home_session.client("s3", config=AWS_IO), settings["bucket"], home.account, intent
        )
        registry = ActorRegistry(
            self.journal, Path(settings["cli"]), Path(settings["aws_provider"])
        )
        self.registry = registry
        command = TrackedPulumiCommand(
            registry, provider_credentials=lambda: target.frozen_credentials(target_session)
        )
        backend = AccessBackend(self.journal, home_session, command=command)
        self.unit = AccessUnit(
            self.journal,
            backend,
            AccessInventory(self.journal, target_session),
            target.frozen_credentials(target_session),
            registry,
            registry.require_stopped,
        )

    def start(self) -> AccessDescriptor:
        with _ACCESS_PROGRAM:
            return self.unit.start()

    def close(self) -> None:
        with _ACCESS_PROGRAM:
            self.unit.stop()

    def abort(self) -> None:
        for process in self.registry.revoke():
            self.registry.stop(process)
        self.registry.require_stopped()
