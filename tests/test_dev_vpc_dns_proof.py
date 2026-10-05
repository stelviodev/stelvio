"""Loopback wire checks for the P0 relay; OS resolver acceptance is a live proof."""

import socketserver
import struct
import threading
from contextlib import contextmanager
from importlib import import_module

import dns.flags
import dns.message
import dns.query
import dns.rcode
import dns.rdatatype
import dns.rrset
from pytest import mark

relay = import_module("spikes.dev-vpc-v1.dns_relay")
OWNER = "12345678-1234-4234-8234-123456789abc"


@contextmanager
def serving(server):
    with server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join(timeout=5)
            assert not thread.is_alive()


class DNSProxy(socketserver.BaseRequestHandler):
    def handle(self):
        connection = self.request
        connection.settimeout(3)
        greeting = relay.receive(connection, 3)
        if greeting != b"\x05\x01\x00":
            return
        connection.sendall(b"\x05\x00")
        destination = relay.receive(connection, 10)
        self.server.destinations.append(destination)
        connection.sendall(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x35")
        length = struct.unpack("!H", relay.receive(connection, 2))[0]
        query = dns.message.from_wire(relay.receive(connection, length))
        response = dns.message.make_response(query)
        question = query.question[0]
        if question.rdtype == dns.rdatatype.TXT:
            records = [f'"{index:02d}{"x" * 200}"' for index in range(8)]
            response.answer.append(dns.rrset.from_text(question.name, 5, "IN", "TXT", *records))
        else:
            response.answer.append(
                dns.rrset.from_text(question.name, 5, "IN", "A", f"10.{self.server.octet}.0.10")
            )
        wire = response.to_wire()
        connection.sendall(struct.pack("!H", len(wire)) + wire)


class ProxyServer(socketserver.ThreadingTCPServer):
    accepted = 0

    def get_request(self):
        request = super().get_request()
        self.accepted += 1
        return request


@mark.parametrize("protocol", ["udp", "tcp"])
def test_dns_clients_select_own_vpc_and_fail_closed_when_transport_is_gone(protocol):
    client = dns.query.udp if protocol == "udp" else dns.query.tcp
    server_type = relay.UDPServer if protocol == "udp" else relay.TCPServer
    handler = relay.UDPHandler if protocol == "udp" else relay.TCPHandler
    for index, octet in enumerate((254, 253)):
        suffix = f"vpc{index}.{OWNER}.stelvio-proof.test"
        proxy = ProxyServer(("127.0.0.1", 0), DNSProxy)
        proxy.octet = octet
        proxy.destinations = []
        server = server_type(("127.0.0.1", 0), handler)
        server.relay = relay.Relay(suffix, octet, proxy.server_address[1])
        server.request_slots = threading.BoundedSemaphore(32)
        with serving(server):
            with serving(proxy):
                query = dns.message.make_query(f"service.{suffix}", "A")
                answer = client(query, "127.0.0.1", port=server.server_address[1], timeout=5)
                assert answer.rcode() == dns.rcode.NOERROR
                assert answer.answer == [
                    dns.rrset.from_text(f"service.{suffix}.", 5, "IN", "A", f"10.{octet}.0.10")
                ]
                assert proxy.destinations == [
                    b"\x05\x01\x00\x01\x0a" + bytes((octet, 0, 2)) + b"\x00\x35"
                ]
                foreign = dns.message.make_query(
                    f"service.vpc{1 - index}.{OWNER}.stelvio-proof.test", "A"
                )
                refused = client(foreign, "127.0.0.1", port=server.server_address[1], timeout=5)
                assert refused.rcode() == dns.rcode.REFUSED
                assert not refused.answer
                assert not refused.authority
                assert not refused.additional
                assert proxy.accepted == 1
                assert len(proxy.destinations) == 1
                large = dns.message.make_query(f"large.{suffix}", "TXT")
                result = client(large, "127.0.0.1", port=server.server_address[1], timeout=5)
                if protocol == "udp":
                    assert result.flags & dns.flags.TC
                    assert len(result.to_wire()) <= 512
                else:
                    assert not result.flags & dns.flags.TC
                    records = [f'"{index:02d}{"x" * 200}"' for index in range(8)]
                    assert result.answer == [
                        dns.rrset.from_text(f"large.{suffix}.", 5, "IN", "TXT", *records)
                    ]
            failed = client(query, "127.0.0.1", port=server.server_address[1], timeout=5)
            assert failed.rcode() == dns.rcode.SERVFAIL
            assert not failed.answer
            assert not failed.authority
            assert not failed.additional
