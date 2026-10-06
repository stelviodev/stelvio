"""Disposal survives partial version deletion without reviving an access owner."""

from pytest import raises

from stelvio.tunnel.access_metadata import AccessMetadata
from stelvio.tunnel.access_state import AccessJournal
from tests.tunnel.storage import VersionedStorage


def test_disposal_retains_receipt_for_partial_retry_and_preserves_other_namespaces(
    access_intent, monkeypatch
):
    storage = VersionedStorage()
    journal = AccessJournal(storage, "state-bucket", access_intent.account, access_intent)
    journal.claim()
    journal.record("backend-cleaned.json", {"cleaned": True})
    journal.record("key-removed.json", {"removed": True})
    storage.versions.append({"Key": "application/state", "VersionId": "application-v1"})
    storage.delete_markers.extend(
        [
            {"Key": "application/previous", "VersionId": "foreign-marker"},
            {"Key": access_intent.prefix + "previous.json", "VersionId": "owned-marker"},
        ]
    )
    cleanup = AccessMetadata(journal, lambda: None)
    original = storage.delete_objects

    def partial(**request):
        objects = request["Delete"]["Objects"]
        original(**(request | {"Delete": {"Objects": objects[:1], "Quiet": True}}))
        return {"Errors": [{"Code": "AccessDenied", "Key": objects[-1]["Key"]}]}

    monkeypatch.setattr(storage, "delete_objects", partial)
    with raises(RuntimeError, match="metadata deletion failed"):
        cleanup.remove_records()
    receipt = journal.read("metadata-cleanup.json")
    assert receipt["cleaned"] is True
    recovered = AccessJournal(storage, "state-bucket", access_intent.account, access_intent)
    with raises(RuntimeError, match="only metadata cleanup may resume"):
        recovered.claim()
    monkeypatch.setattr(storage, "delete_objects", original)
    AccessMetadata(recovered, lambda: None).remove_records()
    AccessMetadata(recovered, lambda: None).remove_records()
    assert storage.versions == [{"Key": "application/state", "VersionId": "application-v1"}]
    assert storage.delete_markers == [
        {"Key": "application/previous", "VersionId": "foreign-marker"}
    ]


def test_encrypted_backend_versions_removed_before_key_and_receipt(access_intent):
    storage = VersionedStorage()
    journal = AccessJournal(storage, "state-bucket", access_intent.account, access_intent)
    journal.claim()
    journal.record("backend-cleaned.json", {"cleaned": True})
    prefix = access_intent.prefix + "pulumi/.pulumi/stacks/access.json"
    storage.versions.extend(
        [
            {"Key": prefix, "VersionId": "old-state"},
            {"Key": prefix, "VersionId": "current-state"},
        ]
    )
    storage.delete_markers.append({"Key": prefix, "VersionId": "backend-delete-marker"})
    cleanup = AccessMetadata(journal, lambda: None)
    cleanup.remove_backend_versions()
    assert not any(v["Key"].startswith(access_intent.prefix + "pulumi/") for v in storage.versions)
    assert storage.delete_markers == []
    assert journal.read("intent.json") is not None
    assert journal.read("claim.json") is not None
    assert journal.read("backend-cleaned.json") == {"cleaned": True}
    with raises(RuntimeError, match="key removal is not certified"):
        cleanup.remove_records()
    assert journal.read("metadata-cleanup.json") is None
