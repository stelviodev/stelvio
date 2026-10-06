"""No-AWS-resource proof of registered actors across Pulumi lifecycle commands."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pulumi
import pulumi_aws as aws
from pulumi.automation import (
    CommandError,
    LocalWorkspaceOptions,
    ProjectBackend,
    ProjectSettings,
    create_or_select_stack,
)

from stelvio.pulumi import get_stelvio_config_dir
from stelvio.tunnel.actors import AWS_VERSION, ActorRegistry
from stelvio.tunnel.engine import TrackedPulumiCommand
from stelvio.tunnel.processes import identity

if TYPE_CHECKING:
    import subprocess
    from collections.abc import Mapping, Sequence


class FaultRegistry(ActorRegistry):
    """Inject a provider death after the runner's version handshake, before CLI."""

    fail_provider = False

    def spawn(
        self,
        kind: str,
        args: Sequence[str],
        environment: Mapping[str, str],
        cwd: str | None = None,
    ) -> subprocess.Popen:
        if kind == "engine" and self.fail_provider:
            self.fail_provider = False
            self.stop(self.children[-1][1])
        return super().spawn(kind, args, environment, cwd)


class LocalJournal:
    """Immutable in-memory ownership records; this proof makes no AWS calls."""

    def __init__(self) -> None:
        self.intent = SimpleNamespace(owner_uid=os.geteuid())
        self.values = {"claim.json": {"creator": asdict(identity(os.getpid()))}}

    def require_claim(self) -> None:
        actual = identity(os.getpid())
        if actual.uid != self.intent.owner_uid:
            raise RuntimeError("Local proof creator changed")

    def read(self, name: str) -> dict | None:
        return self.values.get(name)

    def record(self, name: str, value: dict) -> None:
        if name in self.values and self.values[name] != value:
            raise RuntimeError("Local proof record conflict")
        self.values[name] = json.loads(json.dumps(value))


def main() -> None:  # noqa: C901 - serial positive/failure lifecycle proof
    root = get_stelvio_config_dir()
    journal = LocalJournal()
    registry = FaultRegistry(
        journal,
        root / "bin" / "pulumi",
        root / ".pulumi" / "plugins" / f"resource-aws-v{AWS_VERSION}" / "pulumi-resource-aws",
    )
    command = TrackedPulumiCommand(registry)
    environment = {
        "AWS_ACCESS_KEY_ID": "fake-preview-key",
        "AWS_SECRET_ACCESS_KEY": "fake-preview-secret",
        "AWS_SESSION_TOKEN": "",
        "AWS_REGION": "us-east-1",
        "AWS_DEFAULT_REGION": "us-east-1",
        "PULUMI_CONFIG_PASSPHRASE": "disposable-local-preview",
    }
    include_group = True

    def program() -> None:
        scoped = aws.Provider(
            "preview-aws",
            region="us-east-1",
            access_key="fake-preview-key",
            secret_key="fake-preview-secret",  # noqa: S106 - deliberately fake preview credentials
            skip_credentials_validation=True,
            skip_requesting_account_id=True,
            skip_metadata_api_check=True,
            skip_region_validation=True,
        )
        if include_group:
            aws.ec2.SecurityGroup(
                "preview-only",
                vpc_id="vpc-not-created",
                opts=pulumi.ResourceOptions(provider=scoped),
            )

    try:
        with tempfile.TemporaryDirectory(prefix="stelvio-actor-preview-") as scratch:
            stack = create_or_select_stack(
                stack_name="actor-proof",
                project_name="stelvio-actor-proof",
                program=program,
                opts=LocalWorkspaceOptions(
                    pulumi_command=command,
                    pulumi_home=str(root / ".pulumi"),
                    project_settings=ProjectSettings(
                        name="stelvio-actor-proof",
                        runtime="python",
                        backend=ProjectBackend(Path(scratch).as_uri()),
                    ),
                    env_vars=environment,
                ),
            )
            preview = stack.preview()
            if preview.change_summary != {"create": 3}:
                raise RuntimeError("Unexpected registered-provider preview changes")
            registry.fail_provider = True
            try:
                stack.preview()
            except CommandError as error:
                if (
                    "aws (resource) plugin [aws] did not begin responding to RPC connections"
                    not in str(error)
                ):
                    raise RuntimeError(
                        "Preview did not fail at the dead provider connection"
                    ) from error
            else:
                raise RuntimeError("Engine replaced its dead registered AWS provider")
            registry.require_stopped()
            # Provider/Stack are logical state only. Never up the AWS SG: fake
            # credentials and skip-auth flags cannot authorize AWS resource creation.
            include_group = False
            stack.up()
            stack.refresh()
            stack.destroy()
            registry.require_stopped()
            kinds = [
                v["kind"]
                for key, v in journal.values.items()
                if key.startswith("actor-") and key != "actor-protocol.json"
            ]
            if kinds.count("engine") != kinds.count("aws-provider"):
                raise RuntimeError("A command did not receive its own fresh registered provider")
            print(  # noqa: T201 - concise proof evidence
                json.dumps(
                    {
                        "preview": preview.change_summary,
                        "commands": kinds.count("engine"),
                        "up_refresh_destroy": "PASS",
                        "all_actors_stopped": True,
                        "dead_provider_no_fallback": "PASS",
                    }
                )
            )
    finally:
        for _, process in registry.children:
            registry.stop(process)
            if process.stdout and not process.stdout.closed:
                process.stdout.close()
            if process.stderr and not process.stderr.closed:
                process.stderr.close()


if __name__ == "__main__":
    main()
