"""Ordinary loopback UDP/TCP clients exercise private DNS through real SOCKS IO."""

import struct
from ipaddress import IPv4Address, IPv4Network
from threading import Event

import dns.flags
import dns.message
import dns.query
import dns.rcode
import dns.rrset
from pytest import mark, raises

from stelvio.tunnel.dns import DnsListener, DnsRelay, DnsView
from stelvio.tunnel.socks import SocksConnector
from tests.tunnel.tcp_fixture import receive, tcp_peer


def dns_peer(query, response, on_query=lambda: None, *, connections=1, resolver="10.1.0.2"):
    def handle(connection):
        assert receive(connection, 3) == b"\x05\x01\x00"
        connection.sendall(b"\x05\x00")
        assert receive(connection, 10) == (
            b"\x05\x01\x00\x01" + IPv4Address(resolver).packed + b"\x00\x35"
        )
        connection.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
        length = struct.unpack("!H", receive(connection, 2))[0]
        assert dns.message.from_wire(receive(connection, length)) == query
        on_query()
        wire = response.to_wire()
        connection.sendall(struct.pack("!H", len(wire)) + wire)
        assert connection.recv(1) == b""

    return tcp_peer(handle, connections=connections)


def view(port, *, generation=1):
    return DnsView(
        generation,
        ("private.test",),
        ("member.token.docdb.amazonaws.com",),
        IPv4Address("10.1.0.2"),
        SocksConnector(port, (IPv4Network("10.1.0.0/16"),), Event()),
    )


@mark.parametrize("udp", [True, False])
@mark.parametrize("negative", [True, False])
def test_dns_clients_preserve_alias_addresses_ttl_and_negative_answers(udp, negative):
    query = dns.message.make_query("db.private.test", "A")
    expected = dns.message.make_response(query)
    if negative:
        expected.set_rcode(dns.rcode.NXDOMAIN)
        expected.authority.append(
            dns.rrset.from_text(
                "private.test.", 90, "IN", "SOA", "ns.private.test. host.private.test. 1 2 3 4 5"
            )
        )
    else:
        expected.answer.extend(
            [
                dns.rrset.from_text("db.private.test.", 30, "IN", "CNAME", "member.private.test."),
                dns.rrset.from_text("member.private.test.", 12, "IN", "A", "10.1.2.3"),
            ]
        )
    with dns_peer(query, expected) as port:
        relay = DnsRelay(view(port))
        listener = DnsListener(relay)
        try:
            listener.start()
            client = dns.query.udp if udp else dns.query.tcp
            response = client(query, "127.0.0.1", port=listener.port, timeout=5)
            assert response == expected
        finally:
            listener.close()


def test_large_udp_answer_retries_tcp_and_preserves_complete_result():
    query = dns.message.make_query("db.private.test", "A")
    expected = dns.message.make_response(query)
    expected.answer.append(
        dns.rrset.from_text(
            "db.private.test.", 12, "IN", "A", *(f"10.1.0.{index}" for index in range(1, 61))
        )
    )
    with dns_peer(query, expected, connections=2) as port:
        listener = DnsListener(DnsRelay(view(port)))
        try:
            listener.start()
            response, used_tcp = dns.query.udp_with_fallback(
                query, "127.0.0.1", port=listener.port, timeout=5
            )
            assert used_tcp is True
            assert response == expected
        finally:
            listener.close()


@mark.parametrize("failure", ["wrong-id", "truncated", "outage", "new-generation"])
def test_invalid_or_superseded_upstream_cannot_publish_answer_and_new_view_recovers(failure):
    query = dns.message.make_query("member.token.docdb.amazonaws.com", "A")
    upstream = dns.message.make_response(query)
    upstream.answer.append(
        dns.rrset.from_text("member.token.docdb.amazonaws.com.", 12, "IN", "A", "10.1.2.3")
    )
    if failure == "wrong-id":
        upstream.id ^= 1
    elif failure == "truncated":
        upstream.flags |= dns.flags.TC
    holder = {}

    def during_query():
        if failure == "outage":
            assert holder["relay"].reject(1)
        elif failure == "new-generation":
            holder["relay"].activate(view(holder["port"], generation=2))

    with dns_peer(query, upstream, during_query) as port:
        relay = DnsRelay(view(port))
        holder.update(relay=relay, port=port)
        expected = dns.message.make_response(query)
        expected.set_rcode(dns.rcode.SERVFAIL)
        assert dns.message.from_wire(relay.answer(query.to_wire(), udp=False)) == expected
    recovered = dns.message.make_response(query)
    recovered.answer.append(
        dns.rrset.from_text("member.token.docdb.amazonaws.com.", 5, "IN", "A", "10.1.2.4")
    )
    with dns_peer(query, recovered) as port:
        relay.activate(view(port, generation=3))
        assert relay.reject(1) is False
        with raises(ValueError, match="generation is stale"):
            relay.activate(view(port, generation=2))
        assert dns.message.from_wire(relay.answer(query.to_wire(), udp=False)) == recovered


@mark.parametrize(
    ("name", "rcode"),
    [
        ("private.test", dns.rcode.SERVFAIL),
        ("db.private.test", dns.rcode.SERVFAIL),
        ("member.token.docdb.amazonaws.com", dns.rcode.SERVFAIL),
        ("child.member.token.docdb.amazonaws.com", dns.rcode.REFUSED),
        ("notprivate.test", dns.rcode.REFUSED),
        ("private.test.public.test", dns.rcode.REFUSED),
        ("public.test", dns.rcode.REFUSED),
        (".", dns.rcode.REFUSED),
    ],
)
def test_retained_outage_and_foreign_names_never_open_upstream(monkeypatch, name, rcode):
    def forbidden(*args, **kwargs):
        raise AssertionError("Rejected DNS query reached a TCP upstream")

    monkeypatch.setattr(SocksConnector, "connect", forbidden)
    relay = DnsRelay(view(1))
    assert relay.reject(1) is True
    query = dns.message.make_query(name, "A")
    expected = dns.message.make_response(query)
    expected.set_rcode(rcode)
    assert dns.message.from_wire(relay.answer(query.to_wire(), udp=True)) == expected


def test_two_simultaneous_dns_views_route_independently_and_survive_other_vpc_failure():
    queries = [dns.message.make_query(name, "A") for name in ("db.a.test", "db.b.test")]
    responses = [dns.message.make_response(query) for query in queries]
    for response, name, address in zip(
        responses, ("db.a.test", "db.b.test"), ("10.1.2.3", "10.2.3.4"), strict=True
    ):
        response.answer.append(dns.rrset.from_text(name + ".", 12, "IN", "A", address))
    with (
        dns_peer(queries[0], responses[0]) as port_a,
        dns_peer(queries[1], responses[1], connections=2, resolver="10.2.0.2") as port_b,
    ):
        relays = [
            DnsRelay(
                DnsView(
                    1,
                    (domain,),
                    (),
                    IPv4Address(resolver),
                    SocksConnector(port, (IPv4Network(cidr),), Event()),
                )
            )
            for domain, resolver, port, cidr in (
                ("a.test", "10.1.0.2", port_a, "10.1.0.0/16"),
                ("b.test", "10.2.0.2", port_b, "10.2.0.0/16"),
            )
        ]
        listeners = [DnsListener(relay) for relay in relays]
        try:
            for listener in listeners:
                listener.start()
            for index in (0, 1):
                assert (
                    dns.query.udp(
                        queries[index], "127.0.0.1", port=listeners[index].port, timeout=5
                    )
                    == responses[index]
                )
                foreign = queries[1 - index]
                refused = dns.message.make_response(foreign)
                refused.set_rcode(dns.rcode.REFUSED)
                assert (
                    dns.query.tcp(foreign, "127.0.0.1", port=listeners[index].port, timeout=5)
                    == refused
                )
            assert relays[0].reject(1)
            failed = dns.message.make_response(queries[0])
            failed.set_rcode(dns.rcode.SERVFAIL)
            assert (
                dns.query.udp(queries[0], "127.0.0.1", port=listeners[0].port, timeout=5) == failed
            )
            assert (
                dns.query.tcp(queries[1], "127.0.0.1", port=listeners[1].port, timeout=5)
                == responses[1]
            )
        finally:
            for listener in listeners:
                listener.close()
