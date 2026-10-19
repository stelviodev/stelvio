"""Real socket exchanges guard TCP framing, cancellation and VPC boundaries."""

import time
from ipaddress import IPv4Address, IPv4Network
from threading import Event

from pytest import mark, raises

from stelvio.tunnel.socks import SocksConnector, SocksRejectedError, TransportInterruptedError
from tests.tunnel.tcp_fixture import receive, tcp_peer


@mark.parametrize(
    "bound",
    [
        b"\x01\x7f\x00\x00\x01\x00\x01",
        b"\x04" + b"\x00" * 16 + b"\x00\x01",
        b"\x03\x03abc\x00\x01",
    ],
)
def test_tcp_bytes_and_partial_socks_frames_preserve_destination(bound):
    def peer(connection):
        assert receive(connection, 3) == b"\x05\x01\x00"
        connection.sendall(b"\x05")
        connection.sendall(b"\x00")
        assert receive(connection, 10) == b"\x05\x01\x00\x01\x0a\x01\x02\x03\x69\x89"
        for byte in b"\x05\x00\x00" + bound:
            connection.sendall(bytes([byte]))
        assert receive(connection, 7) == b"request"
        connection.sendall(b"response")
        assert connection.recv(1) == b""

    with tcp_peer(peer) as port:
        connector = SocksConnector(port, (IPv4Network("10.1.0.0/16"),), Event())
        with connector.connect(IPv4Address("10.1.2.3"), 27017) as stream:
            deadline = time.monotonic() + 2
            stream.send(b"request", deadline=deadline)
            assert stream.receive(8, deadline=deadline) == b"response"


@mark.parametrize(
    "reply",
    [
        b"\x04\x00\x00\x01",
        b"\x05\x01\x00\x01",
        b"\x05\x00\x01\x01",
        b"\x05\x00\x00\x07",
        b"\x05\x00\x00\x03\x00",
    ],
)
def test_rejected_or_malformed_proxy_reply_closes_connection(reply):
    def peer(connection):
        assert receive(connection, 3) == b"\x05\x01\x00"
        connection.sendall(b"\x05\x00")
        receive(connection, 10)
        connection.sendall(reply)
        assert connection.recv(1) == b""

    with tcp_peer(peer) as port:
        connector = SocksConnector(port, (IPv4Network("10.1.0.0/16"),), Event())
        with raises(SocksRejectedError, match="Local VPC proxy"):
            connector.connect(IPv4Address("10.1.2.3"), 27017)


@mark.parametrize("cancel", [False, True])
def test_silent_proxy_is_bounded_and_cancelled_attempt_closes_socket(cancel):
    stop = Event()

    def peer(connection):
        assert receive(connection, 3) == b"\x05\x01\x00"
        if cancel:
            stop.set()
        assert connection.recv(1) == b""

    with tcp_peer(peer) as port:
        connector = SocksConnector(port, (IPv4Network("10.1.0.0/16"),), stop)
        expected = TransportInterruptedError if cancel else TimeoutError
        message = "cancelled" if cancel else "budget"
        started = time.monotonic()
        with raises(expected, match=message):
            connector.connect(IPv4Address("10.1.2.3"), 27017, timeout=0.2 if not cancel else 5)
        assert time.monotonic() - started < 1


def test_destination_outside_own_vpc_is_refused_before_connect():
    connector = SocksConnector(1, (IPv4Network("10.1.0.0/16"),), Event())
    with raises(ValueError, match="another VPC"):
        connector.connect(IPv4Address("10.2.0.2"), 53)


@mark.parametrize("cancel", [False, True])
def test_post_handshake_partial_body_cancellation_or_eof_never_returns_partial_success(cancel):
    stop = Event()
    partial_sent = Event()
    cancel_now = Event()

    def peer(connection):
        assert receive(connection, 3) == b"\x05\x01\x00"
        connection.sendall(b"\x05\x00")
        receive(connection, 10)
        connection.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
        connection.sendall(b"part")
        partial_sent.set()
        if cancel:
            assert cancel_now.wait(2)
            stop.set()
            assert connection.recv(1) == b""

    with tcp_peer(peer) as port:
        connector = SocksConnector(port, (IPv4Network("10.1.0.0/16"),), stop)
        with connector.connect(IPv4Address("10.1.2.3"), 27017) as stream:
            assert partial_sent.wait(2)
            if cancel:
                assert stream.receive(4, deadline=time.monotonic() + 2) == b"part"
                cancel_now.set()
                assert stop.wait(2)
            expected = TransportInterruptedError if cancel else ConnectionError
            message = "cancelled" if cancel else "ended early"
            with raises(expected, match=message):
                stream.receive(8, deadline=time.monotonic() + 2)
