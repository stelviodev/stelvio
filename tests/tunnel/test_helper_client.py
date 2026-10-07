"""Actual socket/descriptor replies exercise the nonroot native-helper client."""

import array
import os
import socket
import struct
from pathlib import Path
from threading import Event, Thread
from unittest.mock import Mock

from pytest import mark, raises

from stelvio.tunnel.helper_client import (
    HelperError,
    HelperStatus,
    NativeHelper,
    NativeLease,
    _receive,
)

CAPABILITY = b"opaque-lease-key"
SESSION = "11111111-1111-4111-8111-111111111111"


def test_eof_revocation_bypasses_in_flight_mutation_and_refuses_future_grants():
    owner, peer = _pair()
    carrier, packets = _pair(socket.SOCK_DGRAM)
    helper = Mock()
    lease = NativeLease(helper, SESSION, CAPABILITY, owner, carrier)
    completed = Event()
    revoke = Thread(target=lambda: (lease.revoke(), completed.set()))
    lease._mutations.acquire()
    try:
        revoke.start()
        assert completed.wait(1)
        assert peer.recv(1) == b""
        assert carrier.fileno() == -1
        assert lease.closed
    finally:
        lease._mutations.release()
        revoke.join(3)
        lease.revoke()
        peer.close()
        packets.close()
    assert not revoke.is_alive()
    with raises(HelperError, match="closed process"):
        lease.configure(
            unit="00000001", generation=1, vpc_id="vpc-12345678", cidrs=("10.254.0.0/16",)
        )
    with raises(HelperError, match="closed process"):
        lease.inspect()
    helper.request.assert_not_called()
    helper.inspect.assert_not_called()


def _pair(kind=socket.SOCK_STREAM):
    sockets = socket.socketpair(socket.AF_UNIX, kind)
    for connection in sockets:
        connection.settimeout(3)
    return sockets


def reply(payload=b"", *, status=0, reserved=0, size=None, magic=b"STLVREP1"):
    return (
        struct.pack("!8sHHI", magic, status, reserved, len(payload) if size is None else size)
        + payload
    )


@mark.parametrize("payload", [b"", b"STLVHLP1" + struct.pack("!III", 1, 1, 0), bytes(range(256))])
def test_bounded_native_reply_is_preserved_exactly(payload):
    first, second = _pair()
    with first, second:
        second.sendall(reply(payload))
        assert _receive(first) == (payload, [])


@mark.parametrize(
    ("packet", "error"),
    [
        (reply(status=1), "request failed: busy"),
        (reply(status=2), "request failed: invalid"),
        (reply(status=3), "request failed: uncertain"),
        (reply(status=4), "request failed: unauthorized"),
        (reply(status=999), "unknown status"),
        (reply(reserved=1), "incompatible reply"),
        (reply(magic=b"OTHERREP"), "incompatible reply"),
        (reply(size=257), "incompatible reply"),
        (reply(size=3)[:-1], "incomplete reply"),
        (b"", "incomplete reply"),
    ],
)
def test_bad_or_truncated_reply_closes_received_descriptor(packet, error):
    first, second = _pair()
    source, sentinel = _pair(socket.SOCK_DGRAM)
    with first, second, source, sentinel:
        before = len(list(Path("/dev/fd").iterdir()))
        if packet:
            second.sendmsg(
                [packet],
                [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [source.fileno()]))],
            )
        second.shutdown(socket.SHUT_WR)
        with raises(HelperError, match=error) as caught:
            _receive(first)
        expected = {
            "request failed: busy": HelperStatus.BUSY,
            "request failed: invalid": HelperStatus.INVALID,
            "request failed: uncertain": HelperStatus.UNCERTAIN,
            "request failed: unauthorized": HelperStatus.UNAUTHORIZED,
        }
        assert caught.value.status == expected.get(error)
        assert len(list(Path("/dev/fd").iterdir())) == before
        assert source.fileno() >= 0
        assert sentinel.fileno() >= 0
        source.send(b"sentinel")
        assert sentinel.recv(32) == b"sentinel"


@mark.parametrize("count", [2, 33, 128])
def test_extra_reply_descriptors_are_all_closed(count):
    first, second = _pair()
    source, sentinel = _pair(socket.SOCK_DGRAM)
    with first, second, source, sentinel:
        before = len(list(Path("/dev/fd").iterdir()))
        second.sendmsg(
            [reply()],
            [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [source.fileno()] * count))],
        )
        with raises(HelperError, match="invalid descriptor metadata"):
            _receive(first)
        assert len(list(Path("/dev/fd").iterdir())) == before
        source.send(b"still-owned")
        assert sentinel.recv(32) == b"still-owned"


def test_acquired_carrier_is_noninheritable_and_capability_is_not_reported(monkeypatch):
    first, second = _pair()
    source, destination = _pair(socket.SOCK_DGRAM)
    helper = NativeHelper()
    monkeypatch.setattr(helper, "connect", lambda: first)
    with second, source, destination:
        second.sendmsg(
            [reply(CAPABILITY + struct.pack("!I", 1))],
            [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [source.fileno()]))],
        )
        lease = helper.acquire(SESSION)
        try:
            assert lease.capability == CAPABILITY
            assert CAPABILITY.decode() not in repr(lease)
            assert not os.get_inheritable(lease.carrier.fileno())
            assert not lease.carrier.getblocking()
            lease.carrier.send(b"exact-carrier")
            assert destination.recv(32) == b"exact-carrier"
            lease.pid = os.getpid() + 1
            with raises(HelperError, match="another or closed process"):
                lease.remove(unit="12345678", generation=4)
            lease.close()  # Forked process releases only its descriptor copies.
            assert lease.closed
            assert lease.capability == b""
            assert first.fileno() == lease.carrier.fileno() == -1
            lease.close()
        finally:
            first.close()
            lease.carrier.close()


@mark.parametrize(
    ("version", "socket_type", "error"),
    [
        (2, socket.SOCK_DGRAM, "packet carrier is incompatible"),
        (1, socket.SOCK_STREAM, "invalid packet carrier"),
    ],
)
def test_incompatible_acquisition_does_not_retain_lease_or_carrier(
    monkeypatch, version, socket_type, error
):
    first, second = _pair()
    source, sentinel = _pair(socket_type)
    helper = NativeHelper()
    monkeypatch.setattr(helper, "connect", lambda: first)
    with second, source, sentinel:
        before = len(list(Path("/dev/fd").iterdir()))
        second.sendmsg(
            [reply(CAPABILITY + struct.pack("!I", version))],
            [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [source.fileno()]))],
        )
        with raises(HelperError, match=error):
            helper.acquire(SESSION)
        assert first.fileno() == -1
        assert len(list(Path("/dev/fd").iterdir())) == before - 1
        source.send(b"original-still-owned")
        assert sentinel.recv(32) == b"original-still-owned"


def test_inspection_preserves_distinct_units_and_full_generations(monkeypatch):
    helper = NativeHelper()
    payload = (
        b"STLVHLP1"
        + struct.pack("!III", 1, 1, 5)
        + bytes([2])
        + struct.pack("!IQB", 0x12345678, 0x0102030405060708, 1)
        + struct.pack("!IQB", 0x87654321, 0x1112131415161718, 2)
    )
    first, second = _pair()
    with first, second:
        monkeypatch.setattr(helper, "connect", lambda: first)
        second.sendall(reply(payload))
        inspection = helper.inspect()
        assert inspection.owned
        assert not inspection.uncertain
        assert inspection.mutating
        assert [(unit.unit, unit.generation, unit.status.name) for unit in inspection.units] == [
            ("12345678", 0x0102030405060708, "ACTIVE"),
            ("87654321", 0x1112131415161718, "FAILED"),
        ]


@mark.parametrize(
    ("suffix", "error"),
    [
        (bytes([1]), "invalid unit inventory"),
        (bytes([9]), "invalid unit inventory"),
        (bytes([0]) + b"trailing", "invalid unit inventory"),
        (bytes([1]) + struct.pack("!IQB", 0, 3, 1), "invalid unit identity"),
        (bytes([1]) + struct.pack("!IQB", 0x12345678, 0, 1), "invalid unit identity"),
        (bytes([1]) + struct.pack("!IQB", 0x12345678, 3, 5), "invalid unit status"),
        (bytes([2]) + struct.pack("!IQB", 0x12345678, 3, 1) * 2, "invalid unit identity"),
    ],
)
def test_inconsistent_inspection_does_not_certify_unit_state(monkeypatch, suffix, error):
    helper = NativeHelper()
    monkeypatch.setattr(
        helper, "request", lambda _: b"STLVHLP1" + struct.pack("!III", 1, 1, 1) + suffix
    )
    with raises(HelperError, match=error):
        helper.inspect()


@mark.parametrize(
    ("magic", "abi", "carrier", "flags"),
    [
        (b"OTHERHLP", 1, 1, 0),
        (b"STLVHLP1", 2, 1, 0),
        (b"STLVHLP1", 1, 2, 0),
        (b"STLVHLP1", 1, 1, 8),
    ],
)
def test_inspection_compatibility_is_checked_with_valid_inventory(
    monkeypatch, magic, abi, carrier, flags
):
    helper = NativeHelper()
    monkeypatch.setattr(
        helper, "request", lambda _: magic + struct.pack("!III", abi, carrier, flags) + bytes([0])
    )
    with raises(HelperError, match="incompatible with this package"):
        helper.inspect()
