"""Persistent SSH trust survives restarts and rejects unsafe or conflicting state."""

import base64
import json
import os
from dataclasses import replace
from threading import Event, Thread

import click
import pytest
from click.testing import CliRunner

from stelvio.cli.tunnel import tunnel
from stelvio.tunnel import host_trust
from stelvio.tunnel.host_trust import (
    HOST_KEY_PREFIX,
    HostIdentity,
    HostTrustError,
    fingerprint,
    locked_host_trust,
    parse_host_key,
)
from stelvio.tunnel.socks import TransportInterruptedError

KEY = "ssh-ed25519 " + base64.b64encode(HOST_KEY_PREFIX + bytes(range(32))).decode()
OTHER_KEY = (
    "ssh-ed25519 " + base64.b64encode(HOST_KEY_PREFIX + bytes(reversed(range(32)))).decode()
)
TARGET = HostIdentity("aws", "123456789012", "us-east-1", "i-12345678")


@pytest.fixture
def store(tmp_path, monkeypatch):
    cache = tmp_path.resolve() / "cache"
    cache.mkdir(mode=0o700)
    monkeypatch.setattr(host_trust, "get_bin_path", lambda: cache)
    # The trust-store algorithms run on Linux CI too. Darwin ACL adaptation is
    # independently covered by the asset tests and native acceptance.
    monkeypatch.setattr("stelvio.tunnel.assets._no_acl", lambda *args: None)
    monkeypatch.setattr(host_trust, "_acl", lambda *args: None)
    return cache / "ssh-host-trust"


def test_pin_survives_reopening_and_identical_acceptance_is_idempotent(store):
    with locked_host_trust(TARGET, Event()) as entry:
        assert entry.key is None
        entry.accept(KEY)
        path = store / entry.name
        before = path.stat().st_ino
    with locked_host_trust(TARGET, Event()) as entry:
        assert entry.key == KEY
        entry.accept(KEY)
        assert path.stat().st_ino == before
        with pytest.raises(HostTrustError, match="changed"):
            entry.accept(OTHER_KEY)
    assert json.loads(path.read_text()) == {"identity": list(TARGET.scope), "key": KEY}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("partition", "aws-cn"),
        ("account", "999999999999"),
        ("region", "us-west-2"),
        ("instance_id", "i-abcdef12"),
    ],
)
def test_each_aws_scope_has_independent_trust(store, field, value):
    with locked_host_trust(TARGET, Event()) as entry:
        entry.accept(KEY)
    another = replace(TARGET, **{field: value})
    with locked_host_trust(another, Event()) as entry:
        assert entry.key is None
        entry.accept(OTHER_KEY)
    with locked_host_trust(TARGET, Event()) as entry:
        assert entry.key == KEY


@pytest.mark.parametrize(
    "unsafe", ["symlink", "hardlink", "permissions", "corrupt", "identity", "oversize"]
)
def test_unsafe_or_invalid_pin_is_retained_and_rejected(store, unsafe):
    with locked_host_trust(TARGET, Event()) as entry:
        entry.accept(KEY)
        path = store / entry.name
    if unsafe == "symlink":
        target = store / "original"
        path.rename(target)
        path.symlink_to(target)
    elif unsafe == "hardlink":
        os.link(path, store / "extra-link")
    elif unsafe == "permissions":
        path.chmod(0o644)
    elif unsafe == "corrupt":
        path.write_text("{")
    elif unsafe == "identity":
        path.write_text(
            json.dumps({"identity": list(replace(TARGET, region="us-west-2").scope), "key": KEY})
        )
    elif unsafe == "oversize":
        path.write_bytes(b"x" * (host_trust.MAX_RECORD_BYTES + 1))
    before = path.read_bytes()
    with pytest.raises(HostTrustError), locked_host_trust(TARGET, Event()):
        pytest.fail("unsafe pin accepted")
    assert path.read_bytes() == before


@pytest.mark.parametrize("unsafe", ["symlink", "mode", "hardlink"])
def test_unsafe_lock_is_rejected_without_deleting_it(store, unsafe):
    with locked_host_trust(TARGET, Event()):
        pass
    lock = store / (TARGET.alias + ".lock")
    if unsafe == "symlink":
        target = store / "original-lock"
        lock.rename(target)
        lock.symlink_to(target)
    elif unsafe == "mode":
        lock.chmod(0o666)
    else:
        os.link(lock, store / "second-lock")
    with pytest.raises(HostTrustError), locked_host_trust(TARGET, Event()):
        pytest.fail("unsafe lock accepted")
    assert lock.exists()


def test_symlinked_store_is_rejected(store):
    real = store.parent / "elsewhere"
    real.mkdir(mode=0o700)
    store.symlink_to(real, target_is_directory=True)
    with pytest.raises(HostTrustError), locked_host_trust(TARGET, Event()):
        pytest.fail("unsafe directory accepted")
    assert list(real.iterdir()) == []


def test_atomic_publication_failure_never_creates_a_pin(store, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("publication interrupted")

    monkeypatch.setattr(host_trust.os, "replace", fail)
    with pytest.raises(HostTrustError), locked_host_trust(TARGET, Event()) as entry:
        entry.accept(KEY)
    assert not list(store.glob("*.json*"))


def test_concurrent_first_connections_cannot_replace_a_pin(store):
    waiting, finished = Event(), Event()
    failures = []

    def contender():
        waiting.set()
        try:
            with locked_host_trust(TARGET, Event()) as entry:
                assert entry.key == KEY
                entry.accept(OTHER_KEY)
        except HostTrustError as error:
            failures.append(error)
        finally:
            finished.set()

    with locked_host_trust(TARGET, Event()) as entry:
        thread = Thread(target=contender)
        thread.start()
        assert waiting.wait(2)
        assert not finished.wait(0.1)
        entry.accept(KEY)
    thread.join(3)
    assert not thread.is_alive()
    assert len(failures) == 1
    with locked_host_trust(TARGET, Event()) as entry:
        assert entry.key == KEY


def test_waiting_for_lock_is_bounded_and_cancellable(store, monkeypatch):
    import fcntl

    monkeypatch.setattr(host_trust, "LOCK_SECONDS", 0)

    def busy(*args):
        raise BlockingIOError

    monkeypatch.setattr(fcntl, "flock", busy)
    with pytest.raises(HostTrustError, match="busy"), locked_host_trust(TARGET, Event()):
        pytest.fail("lock acquired")
    stop = Event()
    stop.set()
    with pytest.raises(TransportInterruptedError), locked_host_trust(TARGET, stop):
        pytest.fail("cancelled lock acquired")


def test_reset_requires_expected_key_and_preserves_other_identities(store):
    other = replace(TARGET, instance_id="i-abcdef12")
    for target in (TARGET, other):
        with locked_host_trust(target, Event()) as entry:
            entry.accept(KEY)
    with locked_host_trust(TARGET, Event()) as entry:
        with pytest.raises(HostTrustError, match="changed before reset"):
            entry.reset(fingerprint(OTHER_KEY))
        entry.reset(fingerprint(KEY))
    with locked_host_trust(TARGET, Event()) as entry:
        assert entry.key is None
    with locked_host_trust(other, Event()) as entry:
        assert entry.key == KEY


@pytest.mark.parametrize("confirm", ["n\n", "y\n"])
def test_cli_reset_displays_identity_and_requires_confirmation(store, confirm):
    with locked_host_trust(TARGET, Event()) as entry:
        entry.accept(KEY)
    result = CliRunner().invoke(
        tunnel,
        [
            "trust",
            "reset",
            "--account",
            TARGET.account,
            "--region",
            TARGET.region,
            "--instance-id",
            TARGET.instance_id,
        ],
        input=confirm,
    )
    assert TARGET.label in result.output
    assert fingerprint(KEY) in result.output
    with locked_host_trust(TARGET, Event()) as entry:
        assert entry.key == (None if confirm == "y\n" else KEY)
    assert result.exit_code == (0 if confirm == "y\n" else 1)


@pytest.mark.parametrize("key", ["", "ssh-rsa AAAA", "ssh-ed25519 !!!!", "ssh-ed25519 AAAA"])
def test_invalid_host_keys_are_rejected(key):
    with pytest.raises(HostTrustError):
        parse_host_key(key)


def test_duplicate_record_fields_are_rejected(store):
    with locked_host_trust(TARGET, Event()) as entry:
        entry.accept(KEY)
        path = store / entry.name
    path.write_text(
        '{"identity":'
        + json.dumps(TARGET.scope)
        + ',"key":'
        + json.dumps(KEY)
        + ',"key":'
        + json.dumps(OTHER_KEY)
        + "}"
    )
    with pytest.raises(HostTrustError, match="duplicate"), locked_host_trust(TARGET, Event()):
        pytest.fail("duplicate key accepted")


def test_reset_refuses_a_pin_changed_during_confirmation(store, monkeypatch):
    with locked_host_trust(TARGET, Event()) as entry:
        entry.accept(KEY)

    def confirm(*args, **kwargs):
        with locked_host_trust(TARGET, Event()) as entry:
            entry.reset(fingerprint(KEY))
            entry.accept(OTHER_KEY)
        return True

    monkeypatch.setattr(click, "confirm", confirm)
    result = CliRunner().invoke(
        tunnel,
        [
            "trust",
            "reset",
            "--account",
            TARGET.account,
            "--region",
            TARGET.region,
            "--instance-id",
            TARGET.instance_id,
        ],
    )
    assert result.exit_code == 1
    assert "changed before reset" in result.output
    with locked_host_trust(TARGET, Event()) as entry:
        assert entry.key == OTHER_KEY


@pytest.mark.parametrize("error_type", [ConnectionError, TransportInterruptedError])
def test_transport_errors_inside_lock_keep_their_classification(store, error_type):
    with pytest.raises(error_type, match="transport failure"), locked_host_trust(TARGET, Event()):
        raise error_type("transport failure")


def test_uncertain_directory_sync_retains_published_pin(store, monkeypatch):
    import stat

    original = os.fsync

    def sync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("directory sync failed")
        original(fd)

    monkeypatch.setattr(host_trust.os, "fsync", sync)
    with (
        locked_host_trust(TARGET, Event()) as entry,
        pytest.raises(HostTrustError, match="publication failed"),
    ):
        entry.accept(KEY)
    with locked_host_trust(TARGET, Event()) as entry:
        assert entry.key == KEY
