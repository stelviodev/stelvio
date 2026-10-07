"""Bounded private runtime IPC; only deployment identities cross this channel."""

from __future__ import annotations

import json
import struct
from dataclasses import asdict
from typing import TYPE_CHECKING

from stelvio.tunnel.manifest import SessionDescription, manifest_from_records

if TYPE_CHECKING:
    import socket

MAX_MESSAGE = 1024 * 1024


def send(connection: socket.socket, value: dict) -> None:
    wire = json.dumps(value).encode()
    if len(wire) > MAX_MESSAGE:
        raise ValueError("Network runtime message exceeds its bound")
    connection.sendall(struct.pack("!I", len(wire)) + wire)


def receive(connection: socket.socket) -> dict:
    def exact(length: int) -> bytes:
        data = bytearray()
        while len(data) < length:
            part = connection.recv(length - len(data))
            if not part:
                raise EOFError("Network runtime channel closed")
            data.extend(part)
        return bytes(data)

    size = struct.unpack("!I", exact(4))[0]
    if not 0 < size <= MAX_MESSAGE:
        raise ValueError("Network runtime message exceeds its bound")
    result = json.loads(exact(size))
    if not isinstance(result, dict):
        raise TypeError("Malformed network runtime message")
    return result


def session_to_wire(session: SessionDescription) -> dict:
    records = []
    for network in session.manifest.vpcs:
        data = asdict(network)
        data.update(
            version=1,
            kind="vpc",
            cidrs=[str(cidr) for cidr in network.cidrs],
            policy=network.policy.value,
        )
        data["public_subnets"] = [
            {"id": subnet.subnet_id, "az": subnet.availability_zone, "cidr": str(subnet.cidr)}
            for subnet in network.public_subnets
        ]
        if network.access:
            data["access"]["az"] = data["access"].pop("availability_zone")
        records.append(data)
    for kind, values in (
        ("resource", session.manifest.resources),
        ("endpoint", session.manifest.endpoints),
    ):
        records.extend({"version": 1, "kind": kind, **asdict(value)} for value in values)
    # json roundtrip turns immutable sequences into the established metadata lists.
    return json.loads(
        json.dumps(
            {
                "session_id": session.session_id,
                "app": session.app,
                "environment": session.environment,
                "owner_uid": session.owner_uid,
                "records": records,
            }
        )
    )


def session_from_wire(value: dict) -> SessionDescription:
    if value.keys() != {"session_id", "app", "environment", "owner_uid", "records"}:
        raise ValueError("Malformed network runtime session")
    return SessionDescription(
        value["session_id"],
        value["app"],
        value["environment"],
        value["owner_uid"],
        manifest_from_records(value["records"]),
    )
