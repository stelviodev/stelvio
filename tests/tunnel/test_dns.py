"""Actual Python-Traforo DNS interoperability; DNS algorithms are tested in Go."""

import os
import socket
import struct
from contextlib import closing
from ipaddress import IPv4Network
from pathlib import Path
from threading import Thread
from types import SimpleNamespace

from pytest import fixture, mark, raises, skip

from stelvio.tunnel.forwarding import Forwarding

# Independent DNS vectors: A db.private.test, and a matching private A answer.
QUERY = bytes.fromhex("123401000001000000000000026462077072697661746504746573740000010001")
UNCOMPRESSED_ANSWER = bytes.fromhex(
    "123481800001000100000000026462077072697661746504746573740000010001"
    "0264620770726976617465047465737400000100010000000c00040afe0103"
)
ANSWER = bytes.fromhex(
    "123481800001000100000000026462077072697661746504746573740000010001"
    "c00c000100010000000c00040afe0103"
)


def exact(connection, length):
    result = bytearray()
    while len(result) < length:
        part = connection.recv(length - len(result))
        if not part:
            raise ConnectionError("Fixture stream closed")
        result.extend(part)
    return bytes(result)


@fixture
def native_forwarder():
    image = os.environ.get("STLV_TEST_TRAFORO_IMAGE")
    if not image:
        skip("Set STLV_TEST_TRAFORO_IMAGE to a DNS-capable build; pytest never builds Go")
    carrier, peer = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
    with carrier, peer, closing(Forwarding(Path(image), carrier)) as forwarder:
        yield forwarder


@mark.parametrize("udp", [True, False])
def test_actual_dns_control_carrier_process_and_retained_listener(native_forwarder, udp):  # noqa: PLR0915 - actual protocol interoperability
    errors = []
    with socket.socket() as proxy:
        proxy.bind(("127.0.0.1", 0))
        proxy.listen()
        proxy.settimeout(5)

        def serve():
            try:
                with proxy.accept()[0] as connection:
                    connection.settimeout(5)
                    assert exact(connection, 3) == b"\x05\x01\x00"
                    connection.sendall(b"\x05\x00")
                    assert exact(connection, 10) == bytes.fromhex("050100010afe00020035")
                    connection.sendall(bytes.fromhex("050000017f0000010000"))
                    length = struct.unpack("!H", exact(connection, 2))[0]
                    assert exact(connection, length) == QUERY
                    connection.sendall(struct.pack("!H", len(ANSWER)) + ANSWER)
            except Exception as error:
                errors.append(error)

        thread = Thread(target=serve)
        thread.start()
        forwarding = native_forwarder
        network = SimpleNamespace(
            cidrs=(IPv4Network("10.254.0.0/16"),), dns_domains=("private.test",)
        )
        forwarding.add("1", 1, 1, network, proxy.getsockname()[1])
        port = forwarding.configure_dns("1", 1, 1, network, ())

        def exchange():
            with socket.socket(type=socket.SOCK_DGRAM if udp else socket.SOCK_STREAM) as client:
                client.settimeout(5)
                client.connect(("127.0.0.1", port))
                if udp:
                    client.send(QUERY)
                    return client.recv(4096)
                client.sendall(struct.pack("!H", len(QUERY)) + QUERY)
                size = struct.unpack("!H", exact(client, 2))[0]
                return exact(client, size)

        try:
            forwarding.dns("arm", "1", 1, 1, name="db.private.test")
            assert exchange() in (ANSWER, UNCOMPRESSED_ANSWER)
            assert forwarding.dns("check", "1", 1, 1, name="db.private.test")["observed"]
            forwarding.dns("release", "1", 1, 1, name="db.private.test")
            forwarding.dns("reject", "1", 1, 1)
            forwarding.remove("1", 1, 1)
            assert exchange()[3] & 15 == 2  # SERVFAIL survives packet-unit retirement.
            forwarding.add("1", 1, 2, network, proxy.getsockname()[1])
            assert forwarding.configure_dns("1", 2, 2, network, ()) == port
            with raises(ConnectionError, match="stale"):
                forwarding.dns("reject", "1", 1, 1)
            forwarding.dns("reject", "1", 2, 2)
            forwarding.dns("retire", "1", 2, 2)
        finally:
            thread.join(6)
        assert not thread.is_alive()
        assert not errors
