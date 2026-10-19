"""Bounded, versioned IPC between the command and isolated runtime."""

import socket
import struct
from ipaddress import IPv4Network

from pytest import raises

from stelvio.tunnel.manifest import (
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
        "vpc-owner",
        "123456789012",
        "us-east-1",
        "vpc-12345678",
        "provider",
        (IPv4Network("10.254.0.0/16"),),
        BastionPolicy.PERSISTENT,
        ("private.example",),
        (SubnetNetwork("subnet-12345678", "us-east-1a", IPv4Network("10.254.1.0/24")),),
        AccessDescriptor(
            "i-12345678", "sg-12345678", "us-east-1a", "identity-document", "vpc-owner"
        ),
    )
    manifest = NetworkManifest(
        (network,), (), (EndpointNetwork("function", "function", ("vpc-owner",)),)
    )
    return SessionDescription("11111111-1111-4111-8111-111111111111", "app", "dev", 501, manifest)


def test_session_description_round_trips_only_validated_network_metadata():
    original = _description()
    wire = session_to_wire(original)
    decoded = session_from_wire(wire)
    assert decoded == original
    assert wire["version"] == 2
    assert wire["records"][0]["access"]["az"] == "us-east-1a"


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
