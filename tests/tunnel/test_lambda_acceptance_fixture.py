"""Non-AWS checks for the additive private-Lambda acceptance fixture."""

import hashlib
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from tests.integration import lambda_tunnel_session as fixture


class NotFoundError(Exception):
    pass


@pytest.fixture
def api(tmp_path):
    client = Mock()
    client.exceptions.NotFoundException = NotFoundError
    client.create_rest_api.return_value = {"id": "api123"}
    return fixture.PrivateApi(
        Mock(client=Mock(return_value=client)),
        "proof-app",
        "123456789012",
        "us-east-1",
        tmp_path / "api.json",
    )


def test_api_receipt_survives_later_failure(api):
    api.create()
    assert api.api_id == "api123"
    assert '"api_id": "api123"' in api.evidence.read_text()
    api.client.get_resources.side_effect = RuntimeError("integration failure")
    with pytest.raises(RuntimeError, match="integration failure"):
        api.configure("vpce-owned", "arn:aws:lambda:us-east-1:123456789012:function:owned")
    assert api.evidence.is_file()
    policy = api.client.update_rest_api.call_args.kwargs["patchOperations"][0]["value"]
    assert '"aws:SourceVpce": "vpce-owned"' in policy


@pytest.mark.parametrize(
    "record",
    [
        {"name": "other", "tags": {"stelvio:app": "proof-app", "stelvio:env": "proof"}},
        {"name": "proof-app", "tags": {"stelvio:app": "other", "stelvio:env": "proof"}},
        {"name": "proof-app", "tags": {"stelvio:app": "proof-app", "stelvio:env": "prod"}},
    ],
)
def test_foreign_api_is_never_deleted(api, record):
    api.create()
    api.client.get_rest_api.return_value = record
    with pytest.raises(RuntimeError, match="foreign API"):
        api.destroy()
    api.client.delete_rest_api.assert_not_called()


def test_owned_api_deletion_requires_independent_absence(api):
    api.create()
    api.client.get_rest_api.side_effect = [
        {"name": "proof-app", "tags": {"stelvio:app": "proof-app", "stelvio:env": "proof"}},
        NotFoundError(),
    ]
    api.destroy()
    api.client.delete_rest_api.assert_called_once_with(restApiId="api123")
    assert api.client.get_rest_api.call_count == 2


def test_absent_api_cleanup_is_idempotent(api):
    api.create()
    api.client.get_rest_api.side_effect = NotFoundError()
    api.destroy()
    api.client.delete_rest_api.assert_not_called()


@pytest.mark.parametrize("field", ["backend", "nonce", "sha256"])
def test_echo_assertion_rejects_wrong_backend_or_content(field):
    response = {"backend": "app", "nonce": "nonce", "sha256": hashlib.sha256(b"body").hexdigest()}
    fixture.assert_echo(response, app="app", nonce="nonce", payload="body")
    response[field] = "wrong"
    with pytest.raises(AssertionError):
        fixture.assert_echo(response, app="app", nonce="nonce", payload="body")


def test_wait_is_bounded(monkeypatch):
    clock = Mock(side_effect=[0, 0, 2])
    sleep = Mock()
    monkeypatch.setattr(fixture, "time", SimpleNamespace(monotonic=clock, sleep=sleep))
    with pytest.raises(TimeoutError, match="target"):
        fixture.wait_until(lambda: False, timeout=1, description="target")
    sleep.assert_called_once_with(1)


def test_wait_does_not_hide_api_errors():
    with pytest.raises(RuntimeError, match="credentials"):
        fixture.wait_until(
            Mock(side_effect=RuntimeError("credentials")), timeout=1, description="target"
        )


def test_app_cleanup_runs_after_api_cleanup_failure(tmp_path, monkeypatch):
    session = object.__new__(fixture.LambdaTunnelSession)
    session.project = tmp_path
    session.timings = {}
    session.api = Mock()
    session.api.destroy.side_effect = RuntimeError("API failure")
    cleanup = Mock()
    monkeypatch.setattr(fixture.StandaloneCliSession, "destroy", cleanup)
    session.write_evidence = Mock()
    with pytest.raises(ExceptionGroup, match="cleanup incomplete"):
        session.destroy()
    cleanup.assert_called_once()
    session.write_evidence.assert_called_once()


def test_private_http_client_disables_environment_proxies(monkeypatch):
    opener = MagicMock()
    response = opener.open.return_value.__enter__.return_value
    response.status = 200
    response.read.return_value = b'{"nonce":"proof"}'
    factory = Mock(return_value=opener)
    monkeypatch.setattr(fixture, "build_opener", factory)
    assert fixture.private_request("https://private.example/proof/", "proof", "body") == {
        "nonce": "proof"
    }
    assert factory.call_args.args[0].proxies == {}
    request = opener.open.call_args.args[0]
    assert request.full_url == "https://private.example/proof/"
    assert request.get_method() == "POST"


def test_target_readiness_retries_connection_failure(monkeypatch):
    session = object.__new__(fixture.LambdaTunnelSession)
    session.check_private = Mock(side_effect=[fixture.URLError("TLS timeout"), {"ok": True}])
    monkeypatch.setattr(fixture, "time", SimpleNamespace(monotonic=lambda: 0, sleep=Mock()))
    assert session.wait_private("nonce") == {"ok": True}
    assert session.check_private.call_count == 2


def test_target_readiness_does_not_hide_wrong_response():
    session = object.__new__(fixture.LambdaTunnelSession)
    session.check_private = Mock(side_effect=AssertionError("wrong backend"))
    with pytest.raises(AssertionError, match="wrong backend"):
        session.wait_private("nonce")


@pytest.mark.parametrize("url", ["http://example.test/", "file:///etc/hosts"])
def test_private_request_requires_https(url):
    with pytest.raises(ValueError, match="HTTPS"):
        fixture.private_request(url, "nonce", "body")


def test_shared_app_captures_dev_logs_even_if_standalone_ran_last(monkeypatch):
    session = object.__new__(fixture.LambdaTunnelSession)
    session.aws = Mock()
    session.app = "proof-app"
    session.environment_name = "proof"
    session._mode = "standalone"
    session._proof_lambda_function_names = ("proof-app-proof-client", "proof-app-proof-echo")
    groups = ("/aws/lambda/proof-app-proof-client",)
    capture = Mock(return_value=groups)
    remove = Mock()
    monkeypatch.setattr(fixture, "capture_proof_lambda_log_groups", capture)
    monkeypatch.setattr(fixture, "delete_proof_lambda_log_groups", remove)
    assert session._capture_dev_lambda_log_groups() == (groups, [])
    assert session._remove_dev_lambda_log_groups(groups) == []
    assert remove.call_args.kwargs["function_names"] == session._proof_lambda_function_names
    assert remove.call_args.kwargs["log_group_names"] == groups
