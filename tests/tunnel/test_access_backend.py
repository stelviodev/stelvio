"""Each engine uses fresh isolated AWS credentials without storing them in state."""

from datetime import UTC, datetime
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import Mock

from botocore.exceptions import ClientError
from pytest import mark, raises

from stelvio.tunnel.access_backend import AccessBackend
from stelvio.tunnel.access_state import AccessJournal
from tests.tunnel.storage import Storage, VersionedStorage


def test_backend_refreshes_expiring_credentials_and_refuses_account_drift(access_intent):
    journal = AccessJournal(Storage(), "state-bucket", access_intent.account, access_intent)
    journal.claim()
    journal.record(
        "backend.json",
        {"home_account": access_intent.account, "home_region": access_intent.region},
    )
    session = Mock(region_name=access_intent.region)
    session.client.return_value.get_caller_identity.return_value = {
        "Account": access_intent.account
    }
    session.get_credentials.return_value.get_frozen_credentials.side_effect = [
        SimpleNamespace(access_key="first", secret_key="secret-1", token="token-1"),  # noqa: S106 - fake credentials
        SimpleNamespace(access_key="second", secret_key="secret-2", token=None),  # noqa: S106 - fake credentials
    ]
    stack = SimpleNamespace(workspace=SimpleNamespace(env_vars={"AWS_PROFILE": "handler-profile"}))
    backend = AccessBackend(journal, session)
    backend.refresh_credentials(stack)
    assert stack.workspace.env_vars["AWS_ACCESS_KEY_ID"] == "first"
    backend.refresh_credentials(stack)
    expected = {
        "AWS_REGION": "us-east-1",
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_ACCESS_KEY_ID": "second",
        "AWS_SECRET_ACCESS_KEY": "secret-2",
        "AWS_SESSION_TOKEN": "",
        "AWS_PROFILE": "",
        "AWS_DEFAULT_PROFILE": "",
    }
    assert stack.workspace.env_vars == expected

    session.client.return_value.get_caller_identity.return_value = {"Account": "999999999999"}
    session.get_credentials.return_value.get_frozen_credentials.side_effect = None
    session.get_credentials.return_value.get_frozen_credentials.return_value = SimpleNamespace(
        access_key="foreign",
        secret_key="foreign-secret",  # noqa: S106 - fake credentials
        token=None,
    )
    with raises(RuntimeError, match="AWS context changed"):
        backend.refresh_credentials(stack)
    assert stack.workspace.env_vars == expected


@mark.parametrize("lost_response", [False, True])
def test_backend_key_disposal_is_generation_bound_and_retryable(access_intent, lost_response):
    journal = AccessJournal(
        VersionedStorage(), "state-bucket", access_intent.account, access_intent
    )
    journal.claim()
    journal.record("backend-cleaned.json", {"cleaned": True})
    journal.record(
        "backend.json",
        {"home_account": access_intent.account, "home_region": access_intent.region},
    )
    session = Mock(region_name=access_intent.region)
    ssm = Mock()
    sts = Mock()
    session.client.side_effect = lambda name, **_: {"ssm": ssm, "sts": sts}[name]
    sts.get_caller_identity.return_value = {"Account": access_intent.account}
    session.get_credentials.return_value.get_frozen_credentials.return_value = SimpleNamespace(
        access_key="fake-key",
        secret_key="fake-secret",  # noqa: S106 - fake credentials
        token=None,
    )
    backend = AccessBackend(journal, session)
    value = "fake-recovery-key"
    modified = datetime(2026, 10, 6, tzinfo=UTC)
    generation = {
        "name": backend.parameter,
        "version": 1,
        "modified": modified.isoformat(),
        "sha256": sha256(value.encode()).hexdigest(),
    }
    journal.record("key.json", generation)
    alive = True

    def get(**request):
        assert request == {"Name": backend.parameter, "WithDecryption": True}
        if not alive:
            raise ClientError({"Error": {"Code": "ParameterNotFound"}}, "GetParameter")
        return {"Parameter": {"Value": value, "Version": 1, "LastModifiedDate": modified}}

    def delete(**request):
        nonlocal alive
        assert request == {"Name": backend.parameter}
        assert journal.read("key-removal-started.json") == generation
        alive = False
        if lost_response:
            raise TimeoutError("successful deletion response lost")

    ssm.get_parameter.side_effect = get
    ssm.delete_parameter.side_effect = delete
    ssm.list_tags_for_resource.return_value = {
        "TagList": [{"Key": key, "Value": val} for key, val in access_intent.tags.items()]
    }
    # A changed generation cannot be adopted, even with the same owner tags.
    ssm.get_parameter.side_effect = lambda **_: {
        "Parameter": {"Value": value, "Version": 2, "LastModifiedDate": modified}
    }
    with raises(RuntimeError, match="record conflicts"):
        backend.remove_key()
    ssm.delete_parameter.assert_not_called()
    ssm.get_parameter.side_effect = get
    if lost_response:
        with raises(TimeoutError, match="response lost"):
            backend.remove_key()
    else:
        backend.remove_key()
    backend.remove_key()
    assert journal.read("key-removed.json") == {"removed": True}
    ssm.delete_parameter.assert_called_once_with(Name=backend.parameter)
    ssm.put_parameter.assert_not_called()
