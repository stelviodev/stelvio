"""Durable bounded receipts for partially created SSM sessions."""

import json
import os
import stat
from uuid import uuid4

from pytest import raises

from stelvio.tunnel import evidence


def _world(monkeypatch, tmp_path):
    monkeypatch.setattr(evidence, "get_bin_path", lambda: tmp_path)
    monkeypatch.setattr(evidence, "_validate_cache_directory", lambda path: None)
    monkeypatch.setattr(evidence, "_no_acl", lambda *args: None)
    return tmp_path / "tunnel-evidence"


def test_transport_receipts_are_private_atomic_and_retained_until_completion(
    monkeypatch, tmp_path
):
    directory = _world(monkeypatch, tmp_path)
    receipt = uuid4().hex
    evidence.record_transport_event(
        f"transport-start-{receipt}.json",
        {"instance": "i-0123456789abcdef0", "reason": "stelvio/session/receipt"},
    )
    evidence.record_transport_event(
        f"transport-observed-{receipt}.json", {"session_id": "session-0123456789abcdef"}
    )
    files = sorted(directory.glob("*.json"))
    assert len(files) == 2
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in files)
    assert json.loads(files[0].read_text())
    assert not list(directory.glob(".receipt-*.tmp"))

    evidence.record_transport_event(f"transport-stop-{receipt}.json", {"complete": True})
    assert len(list(directory.glob("*.json"))) == 3
    assert directory.stat().st_uid == os.geteuid()
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700


def test_transport_receipts_reject_unbounded_or_incomplete_ownership(monkeypatch, tmp_path):
    directory = _world(monkeypatch, tmp_path)
    name = f"transport-start-{uuid4().hex}.json"
    with raises(evidence.TransportEvidenceError, match="fields"):
        evidence.record_transport_event(name, {"instance": "i-12345678"})
    with raises(evidence.TransportEvidenceError, match="value"):
        evidence.record_transport_event(name, {"instance": "i-12345678", "reason": "x" * 2048})
    assert not list(directory.glob("*.json"))


def test_transport_receipt_never_overwrites_existing_evidence(monkeypatch, tmp_path):
    directory = _world(monkeypatch, tmp_path)
    receipt = uuid4().hex
    name = f"transport-start-{receipt}.json"
    target = directory / name
    directory.mkdir(mode=0o700)
    target.write_text('{"foreign":true}')
    with raises(evidence.TransportEvidenceError, match="preserve"):
        evidence.record_transport_event(
            name, {"instance": "i-0123456789abcdef0", "reason": "owned"}
        )
    assert json.loads(target.read_text()) == {"foreign": True}
