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
from stelvio.tunnel.helper_protocol import HEADER, HelperOperation, HelperRequest

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
        lease.status()
    helper.request.assert_not_called()
    helper.status.assert_not_called()


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


@mark.parametrize("stage", [1, 4, 23])
def test_configure_failure_reports_bounded_public_stage(monkeypatch, stage):
    first, second = _pair()
    helper = NativeHelper()
    monkeypatch.setattr(helper, "connect", lambda: first)
    with second:
        second.sendall(reply(b"CF\x01" + bytes([stage]), status=3))
        with raises(HelperError, match=f"uncertain \\(configure stage {stage}\\)") as caught:
            helper.request(HelperRequest(HelperOperation.STATUS))
        assert caught.value.status == HelperStatus.UNCERTAIN
        assert caught.value.configure_stage == stage


@mark.parametrize("body", [b"CF\x02\x04", b"CF\x01\x00", b"CF\x01\x18", b"CF\x01\x04extra"])
def test_invalid_configure_diagnostic_is_refused(monkeypatch, body):
    first, second = _pair()
    helper = NativeHelper()
    monkeypatch.setattr(helper, "connect", lambda: first)
    with second:
        second.sendall(reply(body, status=3))
        with raises(HelperError, match="incompatible configure diagnostic"):
            helper.request(HelperRequest(HelperOperation.STATUS))


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


def test_status_preserves_distinct_units_and_full_generations(monkeypatch):
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
        state = helper.status()
        request_packet = second.recv(HEADER.size)
        assert request_packet[8] == 1
        assert state.owned
        assert not state.uncertain
        assert state.mutating
        assert [(unit.unit, unit.generation, unit.status.name) for unit in state.units] == [
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
def test_inconsistent_status_does_not_certify_unit_state(monkeypatch, suffix, error):
    helper = NativeHelper()
    monkeypatch.setattr(
        helper, "request", lambda _: b"STLVHLP1" + struct.pack("!III", 1, 1, 1) + suffix
    )
    with raises(HelperError, match=error):
        helper.status()


@mark.parametrize(
    ("magic", "abi", "carrier", "flags"),
    [
        (b"OTHERHLP", 1, 1, 0),
        (b"STLVHLP1", 2, 1, 0),
        (b"STLVHLP1", 1, 2, 0),
        (b"STLVHLP1", 1, 1, 8),
    ],
)
def test_status_compatibility_is_checked_with_valid_inventory(
    monkeypatch, magic, abi, carrier, flags
):
    helper = NativeHelper()
    monkeypatch.setattr(
        helper, "request", lambda _: magic + struct.pack("!III", abi, carrier, flags) + bytes([0])
    )
    with raises(HelperError, match="incompatible with this package"):
        helper.status()


# The same independent 26-byte vector is checked by Go's route diagnostic test.
ROUTE_CONFLICT_WIRE = bytes.fromhex("4346011452430afe1400fffffc000afe1500ffffff0000000007")


def test_route_conflict_reports_both_ranges_and_interface_without_leaking_descriptors():
    first, second = _pair()
    source, sentinel = _pair(socket.SOCK_DGRAM)
    with first, second, source, sentinel:
        before = len(list(Path("/dev/fd").iterdir()))
        second.sendmsg(
            [reply(ROUTE_CONFLICT_WIRE, status=3)],
            [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [source.fileno()]))],
        )
        with raises(HelperError, match="configure stage 20") as caught:
            _receive(first)
        error = caught.value
        assert error.status == HelperStatus.UNCERTAIN
        assert error.configure_stage == 20
        assert error.route_conflict.requested == "10.254.20.0/22"
        assert error.route_conflict.conflicting == "10.254.21.0/24"
        assert error.route_conflict.interface_index == 7
        assert str(error) == (
            "Native helper request failed: uncertain (configure stage 20): "
            "requested route 10.254.20.0/22 conflicts with route 10.254.21.0/24 "
            "on interface index 7"
        )
        assert len(list(Path("/dev/fd").iterdir())) == before
        source.send(b"still-owned")
        assert sentinel.recv(32) == b"still-owned"


@mark.parametrize(
    "payload",
    [ROUTE_CONFLICT_WIRE[:length] for length in range(5, 26)]
    + [
        ROUTE_CONFLICT_WIRE + b"x",
        ROUTE_CONFLICT_WIRE[:4] + b"RX" + ROUTE_CONFLICT_WIRE[6:],
        b"CF\x02" + ROUTE_CONFLICT_WIRE[3:],
        b"CF\x01\x00" + ROUTE_CONFLICT_WIRE[4:],
        b"CF\x01\x18" + ROUTE_CONFLICT_WIRE[4:],
    ],
)
def test_malformed_route_diagnostic_fails_closed_and_closes_descriptors(payload):
    first, second = _pair()
    source, sentinel = _pair(socket.SOCK_DGRAM)
    with first, second, source, sentinel:
        before = len(list(Path("/dev/fd").iterdir()))
        second.sendmsg(
            [reply(payload, status=3)],
            [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [source.fileno()]))],
        )
        with raises(HelperError, match="incompatible configure diagnostic") as caught:
            _receive(first)
        assert caught.value.status == HelperStatus.UNCERTAIN
        assert caught.value.route_conflict is None
        assert len(list(Path("/dev/fd").iterdir())) == before


@mark.parametrize("status", [1, 2, 4])
def test_route_diagnostic_requires_uncertain_status(status):
    first, second = _pair()
    with first, second:
        second.sendall(reply(ROUTE_CONFLICT_WIRE, status=status))
        with raises(HelperError, match="incompatible configure diagnostic"):
            _receive(first)


@mark.parametrize(
    ("network", "mask", "expected"),
    [
        (0x0A000000, 0xFF000000, "10.0.0.0/8"),
        (0x0AFE1501, 0xFFFFFFFF, "10.254.21.1/32"),
        (0x0AFE1501, 0xFFFFFF00, "10.254.21.0/24"),
        (0x0AFE1501, 0xFF00FF00, "10.254.21.1 mask 255.0.255.0"),
        (0x0AFE1501, 0x000000FF, "10.254.21.1 mask 0.0.0.255"),
    ],
)
def test_kernel_route_masks_are_displayed_without_assuming_private_subnet_policy(
    network, mask, expected
):
    first, second = _pair()
    payload = ROUTE_CONFLICT_WIRE[:14] + struct.pack("!III", network, mask, 0xFFFFFFFF)
    with first, second:
        second.sendall(reply(payload, status=3))
        with raises(HelperError) as caught:
            _receive(first)
        assert caught.value.route_conflict.conflicting == expected
        assert caught.value.route_conflict.interface_index == 0xFFFFFFFF
        assert expected in str(caught.value)


@mark.parametrize("failure", [BrokenPipeError, ConnectionResetError])
@mark.parametrize("status", [1, 2, 3, 4])
def test_early_rejection_preserves_authenticated_status(monkeypatch, failure, status):
    helper = NativeHelper(timeout=0.1)
    first, second = _pair()
    connection = Mock(wraps=first)
    connection.__enter__ = Mock(return_value=connection)
    connection.__exit__ = Mock(return_value=False)
    connection.sendall.side_effect = failure()
    monkeypatch.setattr(helper, "connect", lambda: connection)
    with first, second:
        second.sendall(reply(status=status))
        with raises(HelperError) as caught:
            helper.request(HelperRequest(HelperOperation.STATUS))
        assert caught.value.status == HelperStatus(status)
        connection.sendall.assert_called_once()
        assert 0 < connection.settimeout.call_args.args[0] <= 0.1


@mark.parametrize("response", [b"", b"bad", reply(magic=b"INVALID!"), reply()])
def test_failed_send_never_becomes_success_or_invented_busy(monkeypatch, response):
    helper = NativeHelper(timeout=0.1)
    first, second = _pair()
    connection = Mock(wraps=first)
    connection.__enter__ = Mock(return_value=connection)
    connection.__exit__ = Mock(return_value=False)
    connection.sendall.side_effect = BrokenPipeError()
    monkeypatch.setattr(helper, "connect", lambda: connection)
    with first, second:
        second.sendall(response)
        second.shutdown(socket.SHUT_WR)
        with raises(HelperError) as caught:
            helper.request(HelperRequest(HelperOperation.STATUS))
        assert caught.value.status is None
        connection.sendall.assert_called_once()


def test_early_rejection_wait_is_bounded(monkeypatch):
    helper = NativeHelper(timeout=0.01)
    first, second = _pair()
    connection = Mock(wraps=first)
    connection.__enter__ = Mock(return_value=connection)
    connection.__exit__ = Mock(return_value=False)
    connection.sendall.side_effect = BrokenPipeError()
    monkeypatch.setattr(helper, "connect", lambda: connection)
    with first, second, raises(HelperError, match="reply unavailable"):
        helper.request(HelperRequest(HelperOperation.STATUS))


def test_reply_budget_does_not_reset_after_partial_header(monkeypatch):
    connection = Mock()
    connection.recvmsg.return_value = (b"S", [], 0, None)
    monkeypatch.setattr("stelvio.tunnel.helper_client.monotonic", Mock(side_effect=[0, 2]))
    with raises(HelperError, match="timed out"):
        _receive(connection, deadline=1)
    connection.recvmsg.assert_called_once()


def test_early_rejection_closes_unexpected_descriptor(monkeypatch):
    import stelvio.tunnel.helper_client as client

    helper = NativeHelper(timeout=0.1)
    first, second = _pair()
    connection = Mock(wraps=first)
    connection.__enter__ = Mock(return_value=connection)
    connection.__exit__ = Mock(return_value=False)
    connection.sendall.side_effect = BrokenPipeError()
    monkeypatch.setattr(helper, "connect", lambda: connection)
    closed = []
    original = client._close_descriptors

    def close(descriptors):
        closed.extend(descriptors)
        original(descriptors)

    monkeypatch.setattr(client, "_close_descriptors", close)
    with first, second, Path(os.devnull).open("rb") as descriptor:
        second.sendmsg(
            [reply()],
            [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [descriptor.fileno()]))],
        )
        with raises(HelperError, match="Only acquisition"):
            helper.request(HelperRequest(HelperOperation.STATUS))
        assert len(closed) == 1
        with raises(OSError, match=r"[Bb]ad file descriptor"):
            os.fstat(closed[0])
