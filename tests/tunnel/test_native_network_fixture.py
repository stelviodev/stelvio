"""Native fixtures keep resolver ownership separate from packet destinations."""

from ipaddress import IPv4Address, IPv4Network
from unittest.mock import Mock

from pytest import mark

from stelvio.tunnel.forwarding import Forwarding
from stelvio.tunnel.helper_client import NativeLease
from stelvio.tunnel.helper_protocol import HelperOperation, ResolverEndpoint
from tests.integration.tunnel_dns_fixture import native_network, packet_network


@mark.parametrize("octet", [254, 253])
@mark.parametrize("generation", [1, 4])
def test_native_network_preserves_ownership_and_selects_same_helper_packet_routes(
    octet, generation
):
    domain = f"fixture-{octet}.internal"
    network = native_network(octet, domain)
    expected_route = f"10.{octet}.9.0/24"
    assert network.cidrs == (IPv4Network(f"10.{octet}.0.0/16"),)
    assert network.route_cidrs == (IPv4Network(expected_route),)
    assert network.dns_domains == (domain,)
    assert network.route_cidrs[0].subnet_of(network.cidrs[0])
    assert IPv4Address(f"10.{octet}.9.19") in network.route_cidrs[0]
    assert IPv4Address(f"10.{octet}.0.2") not in network.route_cidrs[0]

    forwarding = object.__new__(Forwarding)
    forwarding._command = Mock(return_value={"port": 65130})
    forwarding.add("00000001", generation, 1, packet_network(network), 65131)
    packet_request = forwarding._command.call_args.args[0]
    assert packet_request["cidrs"] == [expected_route]

    forwarding.configure_dns("00000001", generation, 1, 1, network, ())
    dns_request = forwarding._command.call_args.args[0]
    assert dns_request["resolver"] == f"10.{octet}.0.2"
    assert dns_request["domains"] == [domain]

    helper = Mock()
    helper.request.side_effect = lambda request: request.encode() and b""
    lease = NativeLease(helper, "11111111-1111-4111-8111-111111111111", b"a" * 16, Mock(), Mock())
    lease.configure(
        unit="00000001",
        generation=generation,
        vpc_id="vpc-12345678",
        cidrs=tuple(str(cidr) for cidr in network.route_cidrs),
        resolvers=(ResolverEndpoint(domain, 65130),),
    )
    helper_request = helper.request.call_args.args[0]
    assert helper_request.operation == HelperOperation.CONFIGURE
    assert helper_request.generation == generation
    assert helper_request.cidrs == tuple(packet_request["cidrs"]) == (expected_route,)
