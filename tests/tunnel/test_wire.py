"""Bounded, versioned IPC between the command and isolated runtime."""

import socket
import struct
from dataclasses import replace
from ipaddress import IPv4Network

from pytest import mark, raises

from stelvio.tunnel.manifest import (
    VERSION,
    AccessDescriptor,
    EndpointNetwork,
    NetworkManifest,
    SessionDescription,
    SubnetNetwork,
    VpcNetwork,
)
from stelvio.tunnel.policy import BastionPolicy
from stelvio.tunnel.wire import MAX_MESSAGE, receive, send, session_from_wire, session_to_wire


def _description():
    network = VpcNetwork(
        identity="vpc-owner",
        account="123456789012",
        region="us-east-1",
        vpc_id="vpc-12345678",
        provider="provider",
        cidrs=(IPv4Network("10.254.0.0/16"),),
        policy=BastionPolicy.PERSISTENT,
        dns_domains=("private.example",),
        public_subnets=(
            SubnetNetwork("subnet-12345678", "us-east-1a", IPv4Network("10.254.1.0/24")),
        ),
        private_subnets=(
            SubnetNetwork("subnet-private", "us-east-1a", IPv4Network("10.254.2.0/24")),
        ),
        isolated_subnets=(
            SubnetNetwork("subnet-isolated", "us-east-1a", IPv4Network("10.254.3.0/24")),
        ),
        access=AccessDescriptor("i-12345678", "sg-12345678", "us-east-1a", "vpc-owner"),
    )
    manifest = NetworkManifest(
        (network,), (), (EndpointNetwork("function", "function", ("vpc-owner",)),)
    )
    return SessionDescription("11111111-1111-4111-8111-111111111111", "app", "dev", 501, manifest)


@mark.parametrize("empty_private_tiers", [False, True])
def test_session_description_round_trips_only_validated_network_metadata(empty_private_tiers):
    original = _description()
    if empty_private_tiers:
        network = replace(original.manifest.vpcs[0], private_subnets=(), isolated_subnets=())
        original = replace(original, manifest=replace(original.manifest, vpcs=(network,)))
    wire = session_to_wire(original)
    assert wire["version"] == VERSION
    assert wire["records"][0]["access"]["az"] == "us-east-1a"
    network = original.manifest.vpcs[0]
    for tier in ("public_subnets", "private_subnets", "isolated_subnets"):
        assert wire["records"][0][tier] == [
            {"id": subnet.subnet_id, "az": subnet.availability_zone, "cidr": str(subnet.cidr)}
            for subnet in getattr(network, tier)
        ]
    left, right = socket.socketpair()
    left.settimeout(1)
    right.settimeout(1)
    try:
        send(left, wire)
        decoded = session_from_wire(receive(right))
    finally:
        left.close()
        right.close()
    assert decoded == original
    assert decoded.manifest.vpcs[0].route_cidrs == network.route_cidrs


@mark.parametrize("version", [True, 2, VERSION + 1])
def test_session_wire_rejects_unsupported_metadata_versions(version):
    value = session_to_wire(_description())
    value["version"] = version
    with raises(ValueError, match="version"):
        session_from_wire(value)


@mark.parametrize("tier", ["public_subnets", "private_subnets", "isolated_subnets"])
def test_session_wire_validates_subnet_records(tier):
    value = session_to_wire(_description())
    value["records"][0][tier][0]["cidr"] = "192.168.1.0/24"
    with raises(ValueError, match="outside"):
        session_from_wire(value)


def test_session_wire_rejects_unknown_or_unsupported_fields():
    value = session_to_wire(_description())
    value["unexpected"] = "handler data"
    with raises(ValueError, match="Malformed network runtime session"):
        session_from_wire(value)

    value = session_to_wire(_description())
    value["version"] = True
    with raises(ValueError, match="version"):
        session_from_wire(value)


def test_framed_ipc_round_trip_and_short_read():
    left, right = socket.socketpair()
    left.settimeout(1)
    right.settimeout(1)
    try:
        send(left, {"operation": "status", "items": [1, "two"]})
        assert receive(right) == {"operation": "status", "items": [1, "two"]}
    finally:
        left.close()
        right.close()

    left, right = socket.socketpair()
    try:
        left.sendall(struct.pack("!I", 4) + b"{}")
        left.close()
        with raises(EOFError, match="closed"):
            receive(right)
    finally:
        right.close()


def test_framed_ipc_rejects_oversize_invalid_json_and_duplicate_keys():
    left, right = socket.socketpair()
    try:
        left.sendall(struct.pack("!I", MAX_MESSAGE + 1))
        with raises(ValueError, match="bound"):
            receive(right)
    finally:
        left.close()
        right.close()

    for wire in (b"not-json", b"[]", b'{"operation":"status","operation":"stop"}'):
        left, right = socket.socketpair()
        try:
            left.sendall(struct.pack("!I", len(wire)) + wire)
            with raises((ValueError, TypeError)):
                receive(right)
        finally:
            left.close()
            right.close()


def test_ipc_send_rejects_oversize_or_non_json_values():
    left, right = socket.socketpair()
    try:
        with raises(ValueError, match="bound"):
            send(left, {"payload": "x" * MAX_MESSAGE})
        with raises(ValueError, match="valid JSON"):
            send(left, {"payload": float("nan")})
    finally:
        left.close()
        right.close()
