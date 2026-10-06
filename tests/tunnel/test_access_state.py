"""Recovery storage is an independent boundary, usable without the app program."""

import json
from dataclasses import replace

from botocore.exceptions import ClientError
from pytest import fixture, raises

from stelvio.tunnel.access_state import AccessIntent, AccessJournal, AccessTarget
from tests.tunnel.storage import Storage


@fixture
def intent():
    return AccessIntent(
        session="10e08906-0762-4410-9963-2c56a2e06400",
        unit="21f7a531",
        app="app",
        environment="dev",
        owner_uid=502,
        account="123456789012",
        region="us-east-1",
        vpc_id="vpc-target",
        subnet_id="subnet-public",
        availability_zone="us-east-1a",
        ami="ami-selected",
        targets=(AccessTarget("sg-database", 27018),),
    )


def test_intent_survives_independent_caller_and_claim_excludes_concurrent_mutation(intent):
    storage = Storage()
    first = AccessJournal(storage, "state-bucket", intent.account, intent)
    first.claim()
    first.require_claim()
    assert json.loads(storage.objects[intent.prefix + "intent.json"]) == {
        **intent.to_dict(),
        "targets": [{"security_group_id": "sg-database", "port": 27018}],
    }
    second = AccessJournal(storage, "state-bucket", intent.account, intent)
    with raises(RuntimeError, match="owner remains claimed"):
        second.claim()
    with raises(RuntimeError, match="claim is missing"):
        second.require_claim()
    first.release()
    second.claim()
    second.record("observed.json", {"instance": "i-owned", "group": "sg-owned"})
    second.release()
    del first, second
    restored = AccessJournal(storage, "state-bucket", intent.account, intent)
    assert restored.read("observed.json") == {"instance": "i-owned", "group": "sg-owned"}
    assert restored.read("claim.json") is None
    assert all(request["Key"].startswith(intent.prefix) for _, request in storage.events)


def test_conflicting_intent_blocks_claim_before_resource_mutation(intent):
    storage = Storage()
    AccessJournal(storage, "state-bucket", intent.account, intent).initialize()
    changed = replace(intent, vpc_id="vpc-other")
    with raises(RuntimeError, match="recovery record conflicts"):
        AccessJournal(storage, "state-bucket", intent.account, changed).claim()
    assert intent.prefix + "claim.json" not in storage.objects


def test_changed_claim_cannot_be_released_or_used(intent):
    storage = Storage()
    journal = AccessJournal(storage, "state-bucket", intent.account, intent)
    journal.claim()
    storage.objects[intent.prefix + "claim.json"] = b'{"token":"foreign"}'
    with raises(RuntimeError, match="claim is missing or changed"):
        journal.release()
    assert not any(event == "delete" for event, _ in storage.events)


def test_expired_credentials_retain_intent_claim_and_observed_identities(intent):
    storage = Storage()
    journal = AccessJournal(storage, "state-bucket", intent.account, intent)
    journal.claim()
    journal.record("observed.json", {"instance": "i-owned"})
    before = dict(storage.objects)
    storage.fail_read = True
    with raises(ClientError, match="ExpiredToken"):
        journal.release()
    assert storage.objects == before


def test_record_cannot_replace_observed_identity(intent):
    storage = Storage()
    journal = AccessJournal(storage, "state-bucket", intent.account, intent)
    journal.claim()
    journal.record("observed.json", {"instance": "i-owned"})
    with raises(RuntimeError, match="recovery record conflicts"):
        journal.record("observed.json", {"instance": "i-replacement"})
    assert journal.read("observed.json") == {"instance": "i-owned"}
    journal.release()


def test_claim_release_recovers_a_lost_success_response(intent, monkeypatch):
    storage = Storage()
    journal = AccessJournal(storage, "state-bucket", intent.account, intent)
    journal.claim()
    original = storage.delete_object

    def lose_response(**request):
        original(**request)
        raise TimeoutError("response lost after commit")

    monkeypatch.setattr(storage, "delete_object", lose_response)
    with raises(TimeoutError, match="response lost"):
        journal.release()
    journal.release()
    journal.release()
    assert journal.read("claim.json") is None
    assert sum(event == "delete" for event, _ in storage.events) == 1


def test_namespace_encoding_preserves_names_without_changing_key_scope(intent):
    encoded = replace(intent, app="my/app.λ", environment="../dev")
    prefix = encoded.prefix
    assert len(prefix.split("/")) == 6
    assert "../" not in prefix
    assert "%" not in prefix
    assert encoded.to_dict()["app"] == "my/app.λ"


def test_claim_acquisition_recovers_its_own_lost_success_response(intent, monkeypatch):
    storage = Storage()
    journal = AccessJournal(storage, "state-bucket", intent.account, intent)
    original = storage.put_object

    def lose_claim_response(**request):
        result = original(**request)
        if request["Key"].endswith("claim.json"):
            raise TimeoutError("claim committed, response lost")
        return result

    monkeypatch.setattr(storage, "put_object", lose_claim_response)
    with raises(TimeoutError, match="response lost"):
        journal.claim()
    journal.require_claim()
    journal.release()
    assert journal.read("claim.json") is None


def test_claim_changed_after_validation_cannot_be_conditionally_deleted(intent, monkeypatch):
    storage = Storage()
    journal = AccessJournal(storage, "state-bucket", intent.account, intent)
    journal.claim()
    delete = storage.delete_object

    def concurrent_claim_change(**request):
        current = storage.get_object(
            **{key: value for key, value in request.items() if key != "IfMatch"}
        )
        storage.put_object(
            Bucket=request["Bucket"],
            Key=request["Key"],
            ExpectedBucketOwner=request["ExpectedBucketOwner"],
            Body=b'{"token":"concurrent-new-claim"}',
            IfMatch=current["ETag"],
            ServerSideEncryption="AES256",
        )
        return delete(**request)

    monkeypatch.setattr(storage, "delete_object", concurrent_claim_change)
    with raises(ClientError, match="PreconditionFailed"):
        journal.release()
    assert journal.read("claim.json") == {"token": "concurrent-new-claim"}
    assert journal.read("intent.json") == json.loads(json.dumps(intent.to_dict()))
