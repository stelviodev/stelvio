"""Recovery storage is an independent boundary, usable without the app program."""

import json
from dataclasses import replace
from io import BytesIO

from botocore.exceptions import ClientError
from pytest import fixture, raises

from stelvio.tunnel.access_state import AccessIntent, AccessJournal, AccessTarget


class Storage:
    def __init__(self):
        self.objects = {}
        self.events = []
        self.fail_read = False

    def put_object(self, **request):
        self.events.append(("put", request))
        assert request["ExpectedBucketOwner"] == "123456789012"
        assert request["IfNoneMatch"] == "*"
        assert request["ServerSideEncryption"] == "AES256"
        key = request["Key"]
        if key in self.objects:
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        self.objects[key] = request["Body"]
        return {"ETag": '"owned-version"'}

    def get_object(self, **request):
        self.events.append(("get", request))
        assert request["ExpectedBucketOwner"] == "123456789012"
        if self.fail_read:
            raise ClientError({"Error": {"Code": "ExpiredToken"}}, "GetObject")
        if request["Key"] not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": BytesIO(self.objects[request["Key"]])}

    def delete_object(self, **request):
        self.events.append(("delete", request))
        assert request["ExpectedBucketOwner"] == "123456789012"
        assert request["IfMatch"] == '"owned-version"'
        del self.objects[request["Key"]]


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
