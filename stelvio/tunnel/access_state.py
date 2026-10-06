"""Independent, account-bound recovery records for temporary AWS access.

No application checkpoint is read or rewritten here. Immutable intent and observed
identities survive the creating process and its Python environment. A conditional
remote claim excludes simultaneous mutation; recovery never steals a claim.
"""

from __future__ import annotations

import json
import re
from base64 import urlsafe_b64encode
from dataclasses import asdict, dataclass, field, fields
from hashlib import sha256
from typing import TYPE_CHECKING, Any, final
from uuid import UUID, uuid4

from botocore.exceptions import ClientError

from stelvio.tunnel.bastion import assume_role_policy, identity_document, ssm_policy, user_data
from stelvio.tunnel.policy import BastionPolicy
from stelvio.tunnel.processes import ProcessIdentity, identity

UNIT_LENGTH = 8
_ACCOUNT = re.compile(r"[0-9]{12}\Z")
_IDENTITY = re.compile(r"[a-zA-Z0-9_-]+\Z")
_MAX_PORT = 65535

if TYPE_CHECKING:
    from collections.abc import Iterator

    from botocore.client import BaseClient

    from stelvio.tunnel.manifest import SessionDescription, VpcNetwork
    from stelvio.tunnel.processes import CreatorStopped


def _validate_names(app: str, environment: str) -> None:
    for value in (app, environment):
        if not value or any(c.isspace() or not c.isprintable() for c in value):
            raise ValueError("Invalid temporary access application or environment")


def _validate_profile(document: str, policy: str, trust: str, boot: str) -> None:
    if not isinstance(boot, str) or not boot.startswith("#!/bin/bash\n"):
        raise ValueError("Malformed temporary access bootstrap profile")
    for content in (document, policy, trust):
        if not isinstance(content, str) or not isinstance(json.loads(content), dict):
            raise TypeError("Malformed temporary access creation profile")


@final
@dataclass(frozen=True)
class AccessTarget:
    security_group_id: str
    port: int

    def __post_init__(self) -> None:
        if (
            not _IDENTITY.fullmatch(self.security_group_id)
            or not self.security_group_id.startswith("sg-")
            or type(self.port) is not int
            or not 1 <= self.port <= _MAX_PORT
        ):
            raise ValueError("Invalid temporary access target")


@final
@dataclass(frozen=True)
class AccessIntent:
    session: str
    unit: str
    app: str
    environment: str
    owner_uid: int
    account: str
    region: str
    vpc_id: str
    subnet_id: str
    availability_zone: str
    ami: str
    targets: tuple[AccessTarget, ...]
    version: int = 1
    identity_document_content: str = field(default_factory=identity_document)
    ssm_policy_content: str = field(default_factory=ssm_policy)
    user_data_content: str = field(default_factory=user_data)
    assume_role_policy_content: str = field(default_factory=assume_role_policy)

    def __post_init__(self) -> None:
        if self.version != 1 or type(self.version) is not int:
            raise ValueError("Unsupported temporary access intent version")
        if str(UUID(self.session)) != self.session:
            raise ValueError("Temporary access session must be a canonical UUID")
        if len(self.unit) != UNIT_LENGTH or any(c not in "0123456789abcdef" for c in self.unit):
            raise ValueError("Invalid temporary access unit identity")
        if not _ACCOUNT.fullmatch(self.account):
            raise ValueError("Invalid temporary access account")
        if type(self.owner_uid) is not int or self.owner_uid <= 0:
            raise ValueError("Temporary access requires a nonroot owner")
        _validate_names(self.app, self.environment)
        _validate_profile(
            self.identity_document_content,
            self.ssm_policy_content,
            self.assume_role_policy_content,
            self.user_data_content,
        )
        object.__setattr__(self, "targets", tuple(self.targets))
        for value in (
            self.region,
            self.vpc_id,
            self.subnet_id,
            self.availability_zone,
            self.ami,
        ):
            if not _IDENTITY.fullmatch(value):
                raise ValueError("Invalid temporary access identity or namespace")
        if not self.ami.startswith("ami-"):
            raise ValueError("Temporary access requires a resolved AMI ID")
        if len(set(self.targets)) != len(self.targets):
            raise ValueError("Duplicate temporary access targets")

    @property
    def name(self) -> str:
        return f"stlv-tunnel-{UUID(self.session).hex}-{self.unit}"

    @property
    def tags(self) -> dict[str, str]:
        return {"stlv:tunnel-session": self.session, "stlv:tunnel-unit": self.unit}

    @property
    def prefix(self) -> str:
        # URL-safe, reversible segments, without backend URL unescaping changing
        # object-key separators. Do not restrict existing application names.
        app = urlsafe_b64encode(self.app.encode()).decode().rstrip("=")
        environment = urlsafe_b64encode(self.environment.encode()).decode().rstrip("=")
        return f"tunnel/{app}/{environment}/{self.session}/{self.unit}/"

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "targets": [asdict(target) for target in self.targets]}

    @classmethod
    def from_dict(cls, value: dict) -> AccessIntent:
        if (
            not isinstance(value, dict)
            or value.keys() != {field.name for field in fields(cls)}
            or not isinstance(value["targets"], list)
            or any(
                not isinstance(target, dict) or target.keys() != {"security_group_id", "port"}
                for target in value["targets"]
            )
        ):
            raise ValueError("Malformed temporary access recovery intent")
        try:
            targets = [AccessTarget(**target) for target in value["targets"]]
            return cls(**(value | {"targets": tuple(targets)}))
        except (TypeError, ValueError, AttributeError) as error:
            raise ValueError("Malformed temporary access recovery intent") from error


def plan_access(session: SessionDescription, network: VpcNetwork, ami: str) -> AccessIntent:
    if network not in session.manifest.enabled_vpcs or network.policy != BastionPolicy.TEMPORARY:
        raise ValueError("Temporary access requires a used VPC with temporary policy")
    if not ami.startswith("ami-") or not ami.replace("-", "").isalnum():
        raise ValueError("Temporary access requires a resolved Amazon AMI ID")
    subnet = network.public_subnets[0]
    targets = {
        AccessTarget(group, port)
        for resource in session.manifest.resources
        if resource.vpc == network.identity
        for group in resource.security_groups
        for port in resource.ports
    }
    return AccessIntent(
        session.session_id,
        sha256(network.identity.encode()).hexdigest()[:UNIT_LENGTH],
        session.app,
        session.environment,
        session.owner_uid,
        network.account,
        network.region,
        network.vpc_id,
        subnet.subnet_id,
        subnet.availability_zone,
        ami,
        tuple(sorted(targets, key=lambda target: (target.security_group_id, target.port))),
    )


class AccessJournal:
    """S3 conditional records; credentials belong only to the nonroot caller."""

    def __init__(
        self, s3: BaseClient, bucket: str, home_account: str, intent: AccessIntent
    ) -> None:
        self.s3 = s3
        self.bucket = bucket
        self.home_account = home_account
        self.intent = intent
        self._claim: tuple[str, str | None] | None = None
        self._claim_body: dict | None = None

    def _arguments(self, key: str) -> dict[str, str]:
        if not key or "/" in key or key in {".", ".."}:
            raise ValueError("Invalid access journal record name")
        return {
            "Bucket": self.bucket,
            "Key": self.intent.prefix + key,
            "ExpectedBucketOwner": self.home_account,
        }

    def read(self, key: str) -> dict | None:
        try:
            response = self.s3.get_object(**self._arguments(key))
        except ClientError as error:
            if error.response["Error"]["Code"] == "NoSuchKey":
                return None
            raise
        return json.loads(response["Body"].read())

    def record(self, key: str, value: dict) -> None:
        """Persist before dependent changes; a conflicting value is never replaced."""
        try:
            self.s3.put_object(
                **self._arguments(key),
                Body=json.dumps(value, sort_keys=True, separators=(",", ":")).encode(),
                IfNoneMatch="*",
                ContentType="application/json",
                ServerSideEncryption="AES256",
            )
        except ClientError as error:
            if error.response["Error"]["Code"] != "PreconditionFailed":
                raise
        # Covers an existing record and verifies successful writes before mutation.
        if self.read(key) != json.loads(json.dumps(value)):
            raise RuntimeError("Temporary access recovery record conflicts; mutations refused")

    def initialize(self) -> None:
        self.record("intent.json", self.intent.to_dict())

    def _body(self, token: str, creator: ProcessIdentity | None) -> dict:
        body = {"token": token}
        if creator is not None:
            actual = identity(creator.pid)
            if (
                not actual
                or not creator.same_process(actual)
                or creator.uid != self.intent.owner_uid
                or actual.status in {4, 5}
            ):
                raise RuntimeError("Require the live intended creator before claiming access")
            body["creator"] = asdict(creator)
        return body

    def claim(self, creator: ProcessIdentity | None = None) -> None:
        if self._claim is not None:
            raise RuntimeError("Temporary access unit already claimed by this caller")
        if self.read("metadata-cleanup.json") is not None:
            raise RuntimeError("Temporary access is disposed; only metadata cleanup may resume")
        self.initialize()
        token = str(uuid4())
        self._claim_body = self._body(token, creator)
        # Retain our nonce even if the successful write loses its response.
        self._claim = token, None
        try:
            response = self.s3.put_object(
                **self._arguments("claim.json"),
                Body=json.dumps(self._claim_body).encode(),
                IfNoneMatch="*",
                ContentType="application/json",
                ServerSideEncryption="AES256",
            )
        except ClientError as error:
            if error.response["Error"]["Code"] == "PreconditionFailed":
                self._claim = None
                raise RuntimeError(
                    "Temporary access owner remains claimed; retain recovery records and "
                    "verify the previous engine is stopped before recovery"
                ) from error
            raise
        self._claim = token, response["ETag"]

    def recover_claim(self, stopped: CreatorStopped, creator: ProcessIdentity) -> None:
        """Replace a claim only after the trusted supervisor stops its entire creator tree.

        The recovering worker has its own birth identity in the replacement claim,
        so a second recovery must prove that worker stopped as well. A dead PID or
        an old proof alone cannot take a claim from a newer recovery generation.
        """
        if self._claim is not None or self.read("metadata-cleanup.json") is not None:
            raise RuntimeError("Access is already claimed or disposed; recovery claim refused")
        self.initialize()
        response = self.s3.get_object(**self._arguments("claim.json"))
        previous = json.loads(response["Body"].read())
        try:
            root = ProcessIdentity(**previous["creator"])
        except (KeyError, TypeError) as error:
            raise RuntimeError("Previous creator identity is missing; recovery refused") from error
        if (
            root.uid != self.intent.owner_uid
            or not stopped.processes
            or not root.same_process(stopped.processes[0])
            or root.parent != stopped.processes[0].parent
            or root.group != stopped.processes[0].group
        ):
            raise RuntimeError("Stopped proof differs from previous creator; recovery refused")
        stopped.require_stopped()
        token = str(uuid4())
        self._claim_body = self._body(token, creator)
        self._claim = token, None
        try:
            receipt = self.s3.put_object(
                **self._arguments("claim.json"),
                Body=json.dumps(self._claim_body).encode(),
                IfMatch=response["ETag"],
                ContentType="application/json",
                ServerSideEncryption="AES256",
            )
        except ClientError as error:
            if error.response["Error"]["Code"] == "PreconditionFailed":
                self._claim = None
                raise RuntimeError("Previous claim changed; recovery takeover refused") from error
            raise
        self._claim = token, receipt["ETag"]
        self.require_claim()

    def require_claim(self) -> None:
        if self.read("metadata-cleanup.json") is not None:
            raise RuntimeError("Temporary access is disposed; resource mutation refused")
        if self._claim is None or self.read("claim.json") != self._claim_body:
            raise RuntimeError("Temporary access claim is missing or changed; mutations refused")
        if self.read("intent.json") != json.loads(json.dumps(self.intent.to_dict())):
            raise RuntimeError("Temporary access intent changed; mutations refused")
        if self._claim[1] is None:
            response = self.s3.get_object(**self._arguments("claim.json"))
            if json.loads(response["Body"].read()) != self._claim_body:
                raise RuntimeError("Temporary access claim changed while recovering its receipt")
            self._claim = self._claim[0], response["ETag"]

    def release(self) -> None:
        if self._claim is None:
            return
        if self.read("claim.json") is None:
            # A successful conditional deletion may have lost its response.
            self._claim = None
            return
        self.require_claim()
        self.s3.delete_object(**self._arguments("claim.json"), IfMatch=self._claim[1])
        self._claim = None

    def records(self) -> Iterator[str]:
        """Exact owner prefix only, for diagnostics and later cleanup inventory."""
        pages = self.s3.get_paginator("list_objects_v2").paginate(
            Bucket=self.bucket,
            Prefix=self.intent.prefix,
            ExpectedBucketOwner=self.home_account,
        )
        for page in pages:
            for item in page.get("Contents", []):
                yield item["Key"]
