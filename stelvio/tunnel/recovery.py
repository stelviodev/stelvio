"""Explicit stale-session cleanup using the same durable ownership protocol."""

from __future__ import annotations

import json
import os
from base64 import urlsafe_b64encode
from typing import TYPE_CHECKING
from uuid import UUID

from botocore.exceptions import ClientError

from stelvio.tunnel.access_backend import AccessBackend
from stelvio.tunnel.access_inventory import AccessInventory
from stelvio.tunnel.access_metadata import AccessMetadata
from stelvio.tunnel.access_program import CapturedCredentials
from stelvio.tunnel.access_state import UNIT_LENGTH, AccessIntent, AccessJournal
from stelvio.tunnel.access_unit import AccessUnit
from stelvio.tunnel.actors import AWS_VERSION, ActorRegistry, stop_registered_actors
from stelvio.tunnel.credentials import AWS_IO
from stelvio.tunnel.engine import TrackedPulumiCommand
from stelvio.tunnel.processes import ProcessIdentity, identity
from stelvio.tunnel.transport import terminate_owned_session

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import boto3
    from botocore.client import BaseClient

_MAX_RECORD = 65536
_ZOMBIE = 5


def session_prefix(app: str, env: str, session: str) -> str:
    if str(UUID(session)) != session or not app or not env:
        raise ValueError(
            "Recovery requires an application, environment and canonical session UUID"
        )
    parts = [urlsafe_b64encode(s.encode()).decode().rstrip("=") for s in (app, env)]
    return f"tunnel/{parts[0]}/{parts[1]}/{session}/"


def _read(s3: BaseClient, bucket: str, account: str, key: str) -> dict | None:
    try:
        response = s3.get_object(Bucket=bucket, ExpectedBucketOwner=account, Key=key)
    except ClientError as error:
        if error.response["Error"]["Code"] == "NoSuchKey":
            return None
        raise
    with response["Body"] as body:
        # Creation intent/receipt is bounded, never a backend checkpoint or secret.
        data = body.read(_MAX_RECORD + 1)
    if len(data) > _MAX_RECORD:
        raise ValueError("Oversized temporary access recovery record")
    return json.loads(data)


def _recover_unit(
    journal: AccessJournal,
    home: boto3.Session,
    target: boto3.Session,
    config_dir: Path,
    evidence: set[str],
) -> None:
    if journal.read("metadata-cleanup.json") is not None:
        AccessMetadata(journal, lambda: None).remove_records()
        return
    claim = journal.read("claim.json")
    if claim is not None:
        creator = ProcessIdentity(**claim["creator"])
        actual = identity(creator.pid)
        # This command never stops an active developer session, including a
        # suspended creator. Operators must stop it before ownership transfer.
        if actual and creator.same_process(actual) and actual.status != _ZOMBIE:
            raise RuntimeError("Previous network creator is still live; stop it before recovery")
        stopped = stop_registered_actors(journal, creator)
        journal.recover_claim(stopped, identity(os.getpid()))
    else:
        if evidence != {journal.intent.prefix + "intent.json"}:
            raise RuntimeError(
                "Previous claim is missing with actor or creation evidence; retain records"
            )
        journal.claim(identity(os.getpid()))
    registry = ActorRegistry(
        journal,
        config_dir / "bin/pulumi",
        config_dir / ".pulumi/plugins" / f"resource-aws-v{AWS_VERSION}" / "pulumi-resource-aws",
    )
    registry.bind()
    ssm = target.client("ssm", config=AWS_IO)
    for key in tuple(journal.records()):
        name = key.removeprefix(journal.intent.prefix)
        if not name.startswith("transport-start-"):
            continue
        nonce = name.removeprefix("transport-start-").removesuffix(".json")
        if journal.read(f"transport-stop-{nonce}.json") == {"complete": True}:
            continue
        observed = journal.read(f"transport-observed-{nonce}.json")
        if observed is None:
            # An eventually consistent empty SSM listing cannot certify a lost
            # StartSession reply. Keep ownership rather than deleting its proof.
            raise RuntimeError("Unknown SSM start outcome; retain records for reconciliation")
        started = journal.read(name)
        terminate_owned_session(
            ssm, observed["session_id"], started["instance"], started["reason"]
        )
        journal.record(f"transport-stop-{nonce}.json", {"complete": True})
    command = TrackedPulumiCommand(
        registry, provider_credentials=lambda: CapturedCredentials.capture(target)
    )
    unit = AccessUnit(
        journal,
        AccessBackend(journal, home, command=command),
        AccessInventory(journal, target),
        CapturedCredentials.capture(target),
        registry,
        registry.require_stopped,
    )
    try:
        unit.stop()
    finally:
        for _, process in registry.children:
            registry.stop(process)
        registry.require_stopped()


def _load_intent(s3: BaseClient, bucket: str, home_account: str, prefix: str) -> AccessIntent:
    saved = _read(s3, bucket, home_account, prefix + "intent.json")
    if saved is None:
        receipt = _read(s3, bucket, home_account, prefix + "metadata-cleanup.json")
        if not receipt or receipt.get("cleaned") is not True:
            raise RuntimeError("Recovery intent is missing; retain owner records")
        saved = receipt["intent"]
    intent = AccessIntent.from_dict(saved)
    if intent.prefix != prefix or intent.owner_uid != os.geteuid():
        raise RuntimeError("Recovery intent differs from selected namespace or owner UID")
    return intent


def recover_session(  # noqa: C901, PLR0913 - independent ownership fences and aggregate cleanup
    *,
    home: boto3.Session,
    target: boto3.Session,
    bucket: str,
    home_account: str,
    account: str,
    app: str,
    env: str,
    session: str,
    config_dir: Path,
    report: Callable[[str], None],
) -> None:
    """Recover only this UID/session/account/region; aggregate failures across units."""
    if not os.geteuid() or os.getuid() != os.geteuid():
        raise RuntimeError("Recover AWS access as the original ordinary nonroot user")
    prefix = session_prefix(app, env, session)
    for sdk, expected in ((home, home_account), (target, account)):
        if sdk.client("sts", config=AWS_IO).get_caller_identity()["Account"] != expected:
            raise RuntimeError("Recovery AWS credentials belong to another account")
    s3 = home.client("s3", config=AWS_IO)
    units: dict[str, set[str]] = {}
    for page in s3.get_paginator("list_object_versions").paginate(
        Bucket=bucket, Prefix=prefix, ExpectedBucketOwner=home_account
    ):
        for item in page.get("Versions", []) + page.get("DeleteMarkers", []):
            key = item["Key"]
            if not key.startswith(prefix):
                raise RuntimeError("Foreign key in recovery inventory; cleanup refused")
            unit = key[len(prefix) :].split("/", 1)[0]
            if len(unit) != UNIT_LENGTH or any(c not in "0123456789abcdef" for c in unit):
                raise RuntimeError("Invalid recovery unit namespace")
            units.setdefault(unit, set()).add(key)
    failures = []
    for unit in sorted(units):
        try:
            unit_prefix = prefix + unit + "/"
            intent = _load_intent(s3, bucket, home_account, unit_prefix)
            if intent.account != account or intent.region != target.region_name:
                message = (
                    f"{unit}: rerun recovery with --account {intent.account} "
                    f"--region {intent.region} and that provider's credential source"
                )
                failures.append(message)
                report("Retained " + message)
                continue
            journal = AccessJournal(s3, bucket, home_account, intent)
            _recover_unit(journal, home, target, config_dir, units[unit])
            report(f"Recovered {unit} VPC {intent.vpc_id}")
        except Exception as error:
            failures.append(f"{unit}: {error}")
            report(f"Retained {unit}: {error}")
    if failures:
        raise RuntimeError("AWS recovery incomplete: " + "; ".join(failures))
