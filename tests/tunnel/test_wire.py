"""Bounded, versioned IPC between the command and isolated runtime."""

import socket
import struct
from dataclasses import replace

from pytest import mark, raises

from stelvio.network import NetworkManifest, ResourceNetwork, VpcNetwork
from stelvio.tunnel.session_types import SESSION_VERSION, SessionDescription
from stelvio.tunnel.wire import MAX_MESSAGE, receive, send, session_from_wire, session_to_wire


def _description():
    network = VpcNetwork(
        identity="vpc-owner",
        account="123456789012",
        region="us-east-1",
        vpc_id="vpc-12345678",
        provider="provider",
        route_subnet_ids=("subnet-private", "subnet-isolated"),
        bastion_instance_id="i-12345678",
    )
    manifest = NetworkManifest((network,), (ResourceNetwork("database", "vpc-owner", "cluster"),))
    return SessionDescription("11111111-1111-4111-8111-111111111111", "app", "dev", 501, manifest)


@mark.parametrize("empty_subnets", [False, True])
def test_session_round_trips_minimal_records_with_component_descriptors(empty_subnets):
    original = _description()
    if empty_subnets:
        network = replace(original.manifest.vpcs[0], route_subnet_ids=())
        original = replace(original, manifest=replace(original.manifest, vpcs=(network,)))
    wire = session_to_wire(original)
    assert wire["version"] == SESSION_VERSION
    assert wire["records"] == [
        {
            "type": "stelvio:aws:Vpc",
            "urn": "vpc-owner",
            "metadata": {
                "version": 1,
                "account": "123456789012",
                "region": "us-east-1",
                "vpc_id": "vpc-12345678",
                "provider": "provider",
                "route_subnet_ids": [] if empty_subnets else ["subnet-private", "subnet-isolated"],
                "bastion_instance_id": "i-12345678",
            },
        },
        {
            "type": "stelvio:aws:DocumentDb",
            "urn": "database",
            "metadata": {
                "version": 1,
                "vpc": "vpc-owner",
                "cluster_id": "cluster",
            },
        },
    ]
    left, right = socket.socketpair()
    with left, right:
        left.settimeout(1)
        right.settimeout(1)
        send(left, wire)
        assert session_from_wire(receive(right)) == original


@mark.parametrize("version", [True, 1.0, 2])
def test_session_wire_rejects_unsupported_session_versions(version):
    value = session_to_wire(_description())
    value["version"] = version
    with raises(ValueError, match="session version"):
        session_from_wire(value)


@mark.parametrize(("field", "value"), [("urn", []), ("urn", ""), ("type", None)])
def test_session_wire_validates_component_descriptors(field, value):
    data = session_to_wire(_description())
    data["records"][0][field] = value
    with raises(ValueError, match="record descriptor"):
        session_from_wire(data)


def test_session_wire_rejects_unknown_descriptor_fields():
    value = session_to_wire(_description())
    value["records"][0]["cidrs"] = ["10.0.0.0/16"]
    with raises(ValueError, match="record descriptor"):
        session_from_wire(value)


def test_session_wire_validates_deployment_record_version_independently():
    value = session_to_wire(_description())
    value["records"][0]["metadata"]["version"] = 2
    with raises(ValueError, match="upgrade Stelvio"):
        session_from_wire(value)


def test_session_wire_rejects_unknown_session_fields():
    value = session_to_wire(_description())
    value["unexpected"] = "handler data"
    with raises(ValueError, match="Malformed network runtime session"):
        session_from_wire(value)


@mark.parametrize(
    ("field", "value", "message"),
    [
        ("session_id", "not-a-uuid", "UUID"),
        ("owner_uid", 0, "nonroot"),
        ("owner_uid", True, "nonroot"),
        ("app", "", "app"),
        ("environment", "contains whitespace", "environment"),
    ],
)
def test_session_identity_is_validated(field, value, message):
    with raises(ValueError, match=message):
        replace(_description(), **{field: value})


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
