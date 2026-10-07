"""Dispose versioned owner records only after AWS and engine cleanup is certified."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from stelvio.tunnel.access_state import AccessJournal

_RECEIPT = "metadata-cleanup.json"
_BATCH_SIZE = 1000


class AccessMetadata:
    def __init__(self, journal: AccessJournal, require_stopped: Callable[[], None]) -> None:
        self.journal = journal
        self.require_stopped = require_stopped

    def _versions(self) -> list[dict]:
        pages = self.journal.s3.get_paginator("list_object_versions").paginate(
            Bucket=self.journal.bucket,
            Prefix=self.journal.intent.prefix,
            ExpectedBucketOwner=self.journal.home_account,
        )
        versions = []
        for page in pages:
            for version in page.get("Versions", []) + page.get("DeleteMarkers", []):
                if not version["Key"].startswith(self.journal.intent.prefix):
                    raise RuntimeError(
                        "Foreign key in temporary metadata inventory; purge refused"
                    )
                versions.append({"Key": version["Key"], "VersionId": version["VersionId"]})
        return versions

    def _check(self) -> None:
        self.require_stopped()
        for key in self.journal.records():
            relative = key.removeprefix(self.journal.intent.prefix)
            if relative.startswith("transport-start-"):
                completion = relative.replace("transport-start-", "transport-stop-", 1)
                if self.journal.read(completion) != {"complete": True}:
                    raise RuntimeError(
                        "SSM session cleanup is not certified; retain recovery records"
                    )
        receipt = self.journal.read(_RECEIPT)
        if receipt is not None:
            if receipt != self._receipt():
                raise RuntimeError("Temporary cleanup receipt differs; metadata retained")
            return
        self.journal.require_claim()
        if self.journal.read("backend-cleaned.json") != {"cleaned": True}:
            raise RuntimeError("Temporary backend is not certified clean; metadata retained")
        if self.journal.read("creation-started.json") and not self.journal.read(
            "aws-cleaned.json"
        ):
            raise RuntimeError("Temporary AWS cleanup is not certified; metadata retained")

    def _receipt(self) -> dict:
        return {"intent": json.loads(json.dumps(self.journal.intent.to_dict())), "cleaned": True}

    def _delete(self, versions: list[dict]) -> None:
        for offset in range(0, len(versions), _BATCH_SIZE):
            self.require_stopped()
            self._check()
            response = self.journal.s3.delete_objects(
                Bucket=self.journal.bucket,
                ExpectedBucketOwner=self.journal.home_account,
                Delete={"Objects": versions[offset : offset + _BATCH_SIZE], "Quiet": True},
            )
            if response.get("Errors"):
                raise RuntimeError("Temporary metadata deletion failed; retain recovery records")

    def remove_backend_versions(self) -> None:
        """The recovery key must outlive every encrypted backend version."""
        self._check()
        prefix = self.journal.intent.prefix + "pulumi/"
        self._delete(
            [version for version in self._versions() if version["Key"].startswith(prefix)]
        )
        if any(version["Key"].startswith(prefix) for version in self._versions()):
            raise RuntimeError("Temporary backend versions remain; retain recovery key")

    def remove_records(self) -> None:
        """Retain a disposal receipt across partial deletion and interrupted retry."""
        self.require_stopped()
        if not self._versions():
            return
        self._check()
        self.remove_backend_versions()
        if self.journal.read(_RECEIPT) is None:
            if self.journal.read("key-removed.json") != {"removed": True}:
                raise RuntimeError("Temporary recovery key removal is not certified")
            # This immutable tombstone prevents new claims while ownership and
            # cleanup certificates are deleted. A new caller can resume only
            # metadata disposal using it, without any AWS resource mutations.
            self.journal.record(_RECEIPT, self._receipt())
        versions = self._versions()
        last = self.journal.intent.prefix + _RECEIPT
        self._delete([version for version in versions if version["Key"] != last])
        remaining = self._versions()
        if any(version["Key"] != last for version in remaining):
            raise RuntimeError("Temporary metadata versions remain; retain ownership records")
        # Immutable conditional writes produce one receipt version. Never remove
        # an ambiguous replacement receipt or leave an older version exposed.
        if len(remaining) != 1:
            raise RuntimeError("Ambiguous temporary cleanup receipt versions; retain receipt")
        self._delete(remaining)
        if self._versions():
            raise RuntimeError("Temporary owner record deletion is not confirmed")
