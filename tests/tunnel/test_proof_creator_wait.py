"""The CLI proof waits for fenced EOF disposal, without stealing live claims."""

import io
import json
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import Mock

from botocore.exceptions import ClientError
from pytest import fixture, mark, raises

from stelvio.tunnel.processes import ProcessIdentity
from tests.integration import cli_session


@fixture
def owner():
    session = cli_session.CliSession.__new__(cli_session.CliSession)
    session.sdk = Mock()
    session.bucket, session.account, session.project = "state", "123456789012", "retained-proof"
    intent = SimpleNamespace(prefix="tunnel/proof/")
    s3 = session.sdk.client.return_value
    s3.list_objects_v2.return_value = {"Contents": [{"Key": intent.prefix + "claim.json"}]}
    creator = ProcessIdentity(123, 1, 123, 502, 1700000000, 42, 2)
    s3.get_object.return_value = {
        "Body": io.BytesIO(
            json.dumps(
                {
                    "creator": asdict(creator),
                }
            ).encode()
        )
    }
    return session, intent, creator


@mark.parametrize("code", ["NoSuchKey", "NoSuchBucket", "AccessDenied"])
def test_creator_claim_disposal_race_only_allows_missing_key(owner, code):
    session, intent, _creator = owner
    session.sdk.client.return_value.get_object.side_effect = ClientError(
        {"Error": {"Code": code}}, "GetObject"
    )
    if code == "NoSuchKey":
        session.wait_creator_dead(intent)
    else:
        with raises(ClientError):
            session.wait_creator_dead(intent)
    session.sdk.client.return_value.delete_object.assert_not_called()


def test_fenced_sdk_creator_can_finish_after_old_sixty_second_bound(owner, monkeypatch):
    session, intent, creator = owner
    observed = Mock(side_effect=[creator, None])
    monkeypatch.setattr(cli_session, "identity", observed)
    ticks = iter([0, 61])
    monkeypatch.setattr(cli_session.time, "monotonic", lambda: next(ticks, 61))
    monkeypatch.setattr(cli_session.time, "sleep", lambda _seconds: None)
    session.wait_creator_dead(intent)
    assert observed.call_count == 2


def test_same_birth_creator_still_live_at_shutdown_bound_is_refused(owner, monkeypatch):
    session, intent, creator = owner
    monkeypatch.setattr(cli_session, "identity", lambda _pid: creator)
    ticks = iter([0, cli_session.SHUTDOWN_SECONDS])
    monkeypatch.setattr(cli_session.time, "monotonic", lambda: next(ticks, 700))
    with raises(RuntimeError, match="Creator still live"):
        session.wait_creator_dead(intent)
    session.sdk.client.return_value.delete_object.assert_not_called()
