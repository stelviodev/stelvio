"""Independent Pulumi backend and encryption key for one temporary access unit."""

from __future__ import annotations

import secrets
from importlib.metadata import version
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
from semver import VersionInfo

from stelvio.pulumi import ensure_pulumi, get_stelvio_config_dir
from stelvio.tunnel.access_program import CapturedCredentials

if TYPE_CHECKING:
    from collections.abc import Callable

    import boto3
    from pulumi.automation import Stack

    from stelvio.tunnel.access_state import AccessJournal

PROJECT = "stelvio-tunnel"
STACK = "access"


class AccessBackend:
    def __init__(self, journal: AccessJournal, home_session: boto3.Session) -> None:
        self.journal = journal
        self.home_session = home_session
        self.parameter = f"/stlv/tunnel/{journal.intent.session}/{journal.intent.unit}/passphrase"
        self.state_prefix = journal.intent.prefix + "pulumi/"

    def _passphrase(self) -> str:
        ssm = self.home_session.client("ssm")
        try:
            response = ssm.get_parameter(Name=self.parameter, WithDecryption=True)
        except ClientError as error:
            if error.response["Error"]["Code"] != "ParameterNotFound":
                raise
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
        return response["Parameter"]["Value"]

    def open(self, program: Callable[[], None] | None = None, *, create: bool = False) -> Stack:
        self.journal.require_claim()
        credentials = CapturedCredentials.capture(self.home_session)
        if credentials.account != self.journal.home_account:
            raise RuntimeError("Temporary backend AWS account changed; mutations refused")
        backend = (
            f"s3://{self.journal.bucket}/{self.state_prefix.rstrip('/')}"
            f"?region={credentials.region}"
        )
        self.journal.record(
            "backend.json",
            {
                "project": PROJECT,
                "stack": STACK,
                "url": backend,
                "key_parameter": self.parameter,
                "home_region": credentials.region,
                "home_account": credentials.account,
            },
        )
        phrase = self._passphrase()
        ensure_pulumi(show_status=False)
        options = LocalWorkspaceOptions(
            pulumi_command=PulumiCommand(
                root=str(get_stelvio_config_dir()), version=VersionInfo.parse(version("pulumi"))
            ),
            pulumi_home=str(get_stelvio_config_dir() / ".pulumi"),
            project_settings=ProjectSettings(
                name=PROJECT, runtime="python", backend=ProjectBackend(backend)
            ),
            env_vars={
                "PULUMI_CONFIG_PASSPHRASE": phrase,
                "AWS_REGION": credentials.region,
                "AWS_DEFAULT_REGION": credentials.region,
                "AWS_ACCESS_KEY_ID": credentials.access_key,
                "AWS_SECRET_ACCESS_KEY": credentials.secret_key,
                "AWS_SESSION_TOKEN": credentials.token or "",
            },
        )
        factory = create_or_select_stack if create else select_stack
        return factory(
            stack_name=STACK,
            project_name=PROJECT,
            program=program or (lambda: None),
            opts=options,
        )
