"""The Python control client rejects uncertain framing without reusing the channel."""

import socket
import subprocess
import sys
from contextlib import closing
from ipaddress import IPv4Network
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from pytest import fixture, mark, raises, skip

from stelvio.tunnel.forwarding import Forwarding


@fixture
def control_peer(monkeypatch):
    processes = []
    popen = subprocess.Popen

    def make(fault):
        script = """import json,sys,time
fault=sys.argv[1]
hello={"version":2,"status":"ready"}
if fault=="hello": hello["version"]=1
print(json.dumps(hello),flush=True)
for line in sys.stdin:
    request=json.loads(line)
    reply={"version":2,"id":request["id"],"status":"ok","port":32124}
    if fault=="id": reply["id"]+=1
    if fault=="boolean-id": reply["id"]=True
    if fault=="port": reply["port"]=53
    if fault=="status": reply["status"]="unknown"
    if fault=="json": print("not-json",flush=True)
    elif fault=="oversize": print("x"*32769,flush=True)
    elif fault=="eof": sys.stdout.write("{");sys.stdout.flush();sys.exit(0)
    else: print(json.dumps(reply),flush=True)
"""

        def launch(arguments, **options):
            process = popen([sys.executable, "-I", "-u", "-c", script, fault], **options)
            processes.append(process)
            return process

        monkeypatch.setattr("stelvio.tunnel.forwarding.subprocess.Popen", launch)
        carrier, peer = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
        with carrier, peer:
            return Forwarding(Path("unused-fixture-image"), carrier)

    yield make
    for process in processes:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


@mark.parametrize("fault", ["hello", "id", "boolean-id", "status", "json", "oversize", "eof"])
def test_invalid_control_protocol_fails_and_reaps_or_poisons_channel(control_peer, fault):
    if fault == "hello":
        with raises(RuntimeError, match="failed to start"):
            control_peer(fault)
        return
    with closing(control_peer(fault)) as forwarding:
        with raises(ConnectionError):
            forwarding.remove("00000001", 1, 1)
        forwarding.process.wait(timeout=5)
        assert not forwarding.healthy()


def test_control_timeout_poisons_channel(control_peer, monkeypatch):
    with closing(control_peer("ok")) as forwarding:
        monkeypatch.setattr(
            "stelvio.tunnel.forwarding.select.select",
            lambda read, write, errors, timeout: ([], write, []),
        )
        with raises(TimeoutError, match="acknowledgement timed out"):
            forwarding.remove("00000001", 1, 1)
        forwarding.process.wait(timeout=5)
        assert not forwarding.healthy()


def test_invalid_dns_listener_port_is_not_published(control_peer):
    from ipaddress import IPv4Network
    from types import SimpleNamespace

    with closing(control_peer("port")) as forwarding:
        network = SimpleNamespace(
            cidrs=(IPv4Network("10.254.0.0/16"),),
            route_cidrs=(IPv4Network("10.254.1.0/24"),),
            dns_domains=(),
        )
        with raises(ConnectionError, match="listener port"):
            forwarding.configure_dns("00000001", 1, 1, 1, network, ())


def test_controls_preserve_explicit_generation_attempt_and_revision():
    from ipaddress import IPv4Network

    forwarding = object.__new__(Forwarding)
    forwarding._command = Mock(return_value={"port": 53053})
    network = SimpleNamespace(
        cidrs=(IPv4Network("10.254.0.0/16"),),
        route_cidrs=(IPv4Network("10.254.1.0/24"),),
        dns_domains=(),
    )

    forwarding.add("00000001", 7, 12, network, 12000)
    forwarding.configure_dns("00000001", 7, 12, 19, network, ())
    forwarding.dns("probe", "00000001", 7, 12, 19, name="db.internal", port=27017)
    forwarding.remove("00000001", 7, 12)

    requests = [call.args[0] for call in forwarding._command.call_args_list]
    assert [(request["generation"], request["attempt"]) for request in requests] == [
        (7, 12),
        (7, 12),
        (7, 12),
        (7, 12),
    ]
    assert requests[0]["cidrs"] == ["10.254.1.0/24"]
    assert requests[1]["resolver"] == "10.254.0.2"
    assert "cidrs" not in requests[1]
    assert requests[1]["revision"] == requests[2]["revision"] == 19
    assert requests[0]["revision"] == requests[3]["revision"] == 0


@mark.parametrize(
    ("unit", "generation", "attempt"),
    [
        ("00000001", 0, 1),
        ("00000001", True, 1),
        ("00000001", 1, 0),
        ("00000000", 1, 1),
        ("0000000A", 1, 1),
        ("1", 1, 1),
    ],
)
def test_controls_refuse_invalid_identity_before_writing(unit, generation, attempt):
    forwarding = object.__new__(Forwarding)
    forwarding._command = Mock()
    with raises(ValueError, match="Invalid forwarder"):
        forwarding.remove(unit, generation, attempt)
    forwarding._command.assert_not_called()


@mark.skipif(sys.platform != "darwin", reason="pinned Traforo releases target macOS")
def test_verified_nonroot_forwarder_exchanges_private_dns_when_opted_in():
    import os

    if os.environ.get("STLV_TEST_TRAFORO_FORWARDER") != "1":
        skip("set STLV_TEST_TRAFORO_FORWARDER=1 to run the verified native forwarder")

    from stelvio.tunnel.forwarding import packaged_forwarder
    from tests.integration.tunnel_dns_fixture import SocksDnsFixture

    network = SimpleNamespace(
        cidrs=(IPv4Network("10.254.0.0/16"),),
        route_cidrs=(IPv4Network("10.254.9.0/24"),),
        dns_domains=("forwarder.test",),
    )
    socks = SocksDnsFixture()
    carrier, peer = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
    forwarding = None
    try:
        with packaged_forwarder() as binary:
            forwarding = Forwarding(binary, carrier)
            forwarding.add("00000001", 7, 12, network, socks.port)
            forwarding.configure_dns("00000001", 7, 12, 19, network, ())
            result = forwarding.dns(
                "probe",
                "00000001",
                7,
                12,
                19,
                name="alias.forwarder.test",
                port=27017,
                direct=False,
            )
            assert result["status"] == "ok"
            assert socks.server.queries == ["alias.forwarder.test"]
    finally:
        if forwarding is not None:
            forwarding.close()
        carrier.close()
        peer.close()
        socks.close()


@mark.skipif(sys.platform != "darwin", reason="pinned Traforo releases target macOS")
def test_verified_nonroot_forwarder_dns_reject_servfail_and_reconfigure_when_opted_in():
    import os

    if os.environ.get("STLV_TEST_TRAFORO_FORWARDER") != "1":
        skip("set STLV_TEST_TRAFORO_FORWARDER=1 to run the verified native forwarder")

    from stelvio.tunnel.forwarding import packaged_forwarder
    from tests.integration.tunnel_dns_fixture import (
        SocksDnsFixture,
        dns_exchange,
        dns_query_wire,
        dns_response,
    )

    network = SimpleNamespace(
        cidrs=(IPv4Network("10.254.0.0/16"),),
        route_cidrs=(IPv4Network("10.254.9.0/24"),),
        dns_domains=("forwarder.test",),
    )
    socks = SocksDnsFixture()
    carrier, peer = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
    forwarding = None
    try:
        with packaged_forwarder() as binary:
            forwarding = Forwarding(binary, carrier)
            forwarding.add("00000001", 7, 12, network, socks.port)
            port = forwarding.configure_dns("00000001", 7, 12, 19, network, ())

            for transport in ("udp", "tcp"):
                wire = dns_query_wire(f"before-{transport}.forwarder.test", 0x4A40)
                response = dns_exchange("127.0.0.1", port, wire, transport)
                assert dns_response(wire, response)[0] == 0

            forwarding.dns("reject", "00000001", 7, 12, 19)
            upstream_before = tuple(socks.server.queries)
            for index, transport in enumerate(("udp", "tcp"), start=1):
                wire = dns_query_wire(f"outage-{transport}.forwarder.test", 0x4A40 + index)
                response = dns_exchange("127.0.0.1", port, wire, transport)
                assert dns_response(wire, response) == (2, 0)
            assert tuple(socks.server.queries) == upstream_before

            recovered_port = forwarding.configure_dns("00000001", 7, 12, 20, network, ())
            assert recovered_port == port
            for index, transport in enumerate(("udp", "tcp"), start=1):
                name = f"recovered-{transport}.forwarder.test"
                wire = dns_query_wire(name, 0x4A50 + index)
                response = dns_exchange("127.0.0.1", port, wire, transport)
                assert dns_response(wire, response) == (0, 1)
                assert name in socks.server.queries
    finally:
        if forwarding is not None:
            forwarding.close()
        carrier.close()
        peer.close()
        socks.close()


def test_empty_destination_set_never_writes_forwarder_control():
    forwarding = object.__new__(Forwarding)
    forwarding._command = Mock()
    with raises(ValueError, match="no private or isolated"):
        forwarding.add("00000001", 1, 1, SimpleNamespace(route_cidrs=()), 12000)
    forwarding._command.assert_not_called()
