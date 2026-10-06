"""Independent Pulumi backend and encryption key for one temporary access unit."""

from __future__ import annotations

import secrets
from hashlib import sha256
from typing import TYPE_CHECKING

from botocore.exceptions import ClientError
from pulumi.automation import (
    LocalWorkspaceOptions,
    ProjectBackend,
    ProjectSettings,
    PulumiCommand,
    create_or_select_stack,
    select_stack,
)

from stelvio.pulumi import get_stelvio_config_dir
from stelvio.tunnel.access_program import CapturedCredentials
from stelvio.tunnel.engine import TrackedPulumiCommand

if TYPE_CHECKING:
    from collections.abc import Callable

    import boto3
    from pulumi.automation import Stack

    from stelvio.tunnel.access_state import AccessJournal

PROJECT = "stelvio-tunnel"
STACK = "access"


class AccessBackend:
    def __init__(
        self,
        journal: AccessJournal,
        home_session: boto3.Session,
        *,
        command: PulumiCommand | None = None,
    ) -> None:
        self.journal = journal
        self.home_session = home_session
        self.parameter = f"/stlv/tunnel/{journal.intent.session}/{journal.intent.unit}/passphrase"
        self.state_prefix = journal.intent.prefix + "pulumi/"
        self.command = command

    def _context(self, credentials: CapturedCredentials) -> dict:
        if credentials.account != self.journal.home_account:
            raise RuntimeError("Temporary backend AWS account changed; mutations refused")
        return {
            "project": PROJECT,
            "stack": STACK,
            "url": (
                f"s3://{self.journal.bucket}/{self.state_prefix.rstrip('/')}"
                f"?region={credentials.region}"
            ),
            "key_parameter": self.parameter,
            "home_region": credentials.region,
            "home_account": credentials.account,
        }

    def refresh_credentials(self, stack: Stack) -> None:
        """Refresh each engine's credentials from the isolated supervisor session."""
        self.journal.require_claim()
        credentials = CapturedCredentials.capture(self.home_session)
        saved = self.journal.read("backend.json")
        if saved != self._context(credentials):
            raise RuntimeError("Temporary backend AWS context changed; mutations refused")
        stack.workspace.env_vars.update(
            {
                "AWS_REGION": credentials.region,
                "AWS_DEFAULT_REGION": credentials.region,
                "AWS_ACCESS_KEY_ID": credentials.access_key,
                "AWS_SECRET_ACCESS_KEY": credentials.secret_key,
                "AWS_SESSION_TOKEN": credentials.token or "",
                "AWS_PROFILE": "",
                "AWS_DEFAULT_PROFILE": "",
                "PULUMI_BACKEND_URL": saved["url"],
            }
        )

    def _passphrase(self, *, create: bool = True) -> str:
        saved = self.journal.read("backend.json")
        if not saved:
            raise RuntimeError("Temporary backend context is missing; retain recovery key")
        ssm = self.home_session.client("ssm", region_name=saved["home_region"])
        try:
            response = ssm.get_parameter(Name=self.parameter, WithDecryption=True)
        except ClientError as error:
            if error.response["Error"]["Code"] != "ParameterNotFound":
                raise
            if not create:
                raise
            if self.journal.read("key.json") is not None:
                raise RuntimeError("Recorded recovery key is missing; retain backend") from error
            objects = self.journal.s3.list_objects_v2(
                Bucket=self.journal.bucket,
                Prefix=self.state_prefix,
                ExpectedBucketOwner=self.journal.home_account,
                MaxKeys=1,
            )
            if objects.get("Contents"):
                raise RuntimeError(
                    "Temporary access encryption key is missing; retain backend"
                ) from error
            self.journal.record("key-creation-started.json", {"started": True})
            ssm.put_parameter(
                Name=self.parameter,
                Type="SecureString",
                Value=secrets.token_urlsafe(48),
                Overwrite=False,
                Tags=[
                    {"Key": key, "Value": value} for key, value in self.journal.intent.tags.items()
                ],
                Description="Stelvio tunnel recovery key; retain until cleanup completes",
            )
            response = ssm.get_parameter(Name=self.parameter, WithDecryption=True)
        tags = {
            tag["Key"]: tag["Value"]
            for tag in ssm.list_tags_for_resource(
                ResourceType="Parameter", ResourceId=self.parameter
            )["TagList"]
        }
        if any(tags.get(key) != value for key, value in self.journal.intent.tags.items()):
            raise RuntimeError(
                "Temporary access recovery key ownership differs; mutations refused"
            )
        parameter = response["Parameter"]
        self.journal.record(
            "key.json",
            {
                "name": self.parameter,
                "version": parameter["Version"],
                "modified": parameter["LastModifiedDate"].isoformat(),
                "sha256": sha256(parameter["Value"].encode()).hexdigest(),
            },
        )
        return parameter["Value"]

    def remove_key(self) -> None:  # noqa: C901 - ordered key-generation and absence fences
        """Remove only the recorded key generation after certified backend cleanup."""
        self.journal.require_claim()
        if self.journal.read("backend-cleaned.json") != {"cleaned": True}:
            raise RuntimeError("Temporary backend cleanup is not certified; retain recovery key")
        credentials = CapturedCredentials.capture(self.home_session)
        context = self.journal.read("backend.json")
        saved = self.journal.read("key.json")
        unattempted = self.journal.read("creation-started.json") is None
        if not context and unattempted and not saved:
            if credentials.account != self.journal.home_account:
                raise RuntimeError("Temporary recovery key AWS account changed")
            # Context is recorded before the key write. Nothing may have used
            # this key without that record and the resource-create boundary.
            self.journal.record("key-removed.json", {"removed": True})
            return
        if context != self._context(credentials):
            raise RuntimeError("Temporary recovery key AWS context changed; deletion refused")
        pages = self.journal.s3.get_paginator("list_object_versions").paginate(
            Bucket=self.journal.bucket,
            Prefix=self.state_prefix,
            ExpectedBucketOwner=self.journal.home_account,
        )
        if any(page.get("Versions") or page.get("DeleteMarkers") for page in pages):
            raise RuntimeError("Encrypted backend versions remain; retain recovery key")
        if not saved and not unattempted:
            raise RuntimeError("Temporary key identity is missing; retain recovery records")
        ssm = self.home_session.client("ssm", region_name=context["home_region"])
        try:
            self._passphrase(create=False)
        except ClientError as error:
            if error.response["Error"]["Code"] != "ParameterNotFound" or (
                not (unattempted and saved is None)
                and self.journal.read("key-removal-started.json") != saved
            ):
                raise
            if saved is None and self.journal.read("key-creation-started.json") is not None:
                raise RuntimeError(
                    "Recovery key creation has an uncertain result; retain ownership records"
                ) from error
            self.journal.record("key-removed.json", {"removed": True})
            return
        # A startup interrupted after PutParameter may lack the receipt. The
        # read-only passphrase path validates its planned name and owner tags,
        # then persists the generation before deletion. It never recreates it.
        saved = self.journal.read("key.json")
        self.journal.record("key-removal-started.json", saved)
        ssm.delete_parameter(Name=self.parameter)
        try:
            ssm.get_parameter(Name=self.parameter, WithDecryption=True)
        except ClientError as error:
            if error.response["Error"]["Code"] == "ParameterNotFound":
                self.journal.record("key-removed.json", {"removed": True})
                return
            raise
        raise RuntimeError("Temporary recovery key deletion is not confirmed")

    def open(self, program: Callable[[], None] | None = None, *, create: bool = False) -> Stack:
        if not isinstance(self.command, TrackedPulumiCommand):
            raise TypeError("Temporary backend requires the registered native actor runner")
        self.journal.require_claim()
        credentials = CapturedCredentials.capture(self.home_session)
        context = self._context(credentials)
        backend = context["url"]
        self.journal.record("backend.json", context)
        phrase = self._passphrase()
        options = LocalWorkspaceOptions(
            pulumi_command=self.command,
            pulumi_home=str(get_stelvio_config_dir() / ".pulumi"),
            project_settings=ProjectSettings(
                name=PROJECT, runtime="python", backend=ProjectBackend(backend)
            ),
            env_vars={
                "PULUMI_CONFIG_PASSPHRASE": phrase,
                "PULUMI_BACKEND_URL": backend,
                "AWS_REGION": credentials.region,
                "AWS_DEFAULT_REGION": credentials.region,
                "AWS_ACCESS_KEY_ID": credentials.access_key,
                "AWS_SECRET_ACCESS_KEY": credentials.secret_key,
                "AWS_SESSION_TOKEN": credentials.token or "",
                # Every engine receives a freshly captured credential snapshot.
                # Do not persist expiring keys in provider state, or inherit a
                # handler-selected profile alongside those explicit credentials.
                "AWS_PROFILE": "",
                "AWS_DEFAULT_PROFILE": "",
            },
        )
        factory = create_or_select_stack if create else select_stack
        return factory(
            stack_name=STACK,
            project_name=PROJECT,
            program=program or (lambda: None),
            opts=options,
        )
