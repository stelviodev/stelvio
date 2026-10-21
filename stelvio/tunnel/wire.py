"""Bounded private runtime IPC; only deployment identities cross this channel."""

from __future__ import annotations

import json
import struct
from dataclasses import asdict
from typing import TYPE_CHECKING

from stelvio.tunnel.manifest import VERSION, SessionDescription, manifest_from_records

if TYPE_CHECKING:
    import socket

MAX_MESSAGE = 1024 * 1024
MAX_STATUS_DETAIL_LENGTH = 1024


def send(connection: socket.socket, value: dict) -> None:
    if not isinstance(value, dict):
        raise TypeError("Network runtime messages must be objects")
    try:
        wire = json.dumps(value, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as error:
        raise ValueError("Network runtime message is not valid JSON") from error
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

    def unique_object(pairs: list[tuple[str, object]]) -> dict:
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("Duplicate network runtime message key")
            result[key] = item
        return result

    try:
        result = json.loads(exact(size), object_pairs_hook=unique_object)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as error:
        raise ValueError("Malformed network runtime JSON") from error
    if not isinstance(result, dict):
        raise TypeError("Malformed network runtime message")
    return result


def session_to_wire(session: SessionDescription) -> dict:
    records = []
    for network in session.manifest.vpcs:
        data = asdict(network)
        data.update(
            version=VERSION,
            kind="vpc",
            cidrs=[str(cidr) for cidr in network.cidrs],
            policy=network.policy.value,
        )
        for tier in ("public_subnets", "private_subnets", "isolated_subnets"):
            data[tier] = [
                {"id": subnet.subnet_id, "az": subnet.availability_zone, "cidr": str(subnet.cidr)}
                for subnet in getattr(network, tier)
            ]
        if network.access:
            data["access"]["az"] = data["access"].pop("availability_zone")
        records.append(data)
    for kind, values in (
        ("resource", session.manifest.resources),
        ("endpoint", session.manifest.endpoints),
    ):
        records.extend({"version": VERSION, "kind": kind, **asdict(value)} for value in values)
    # json roundtrip turns immutable sequences into the established metadata lists.
    return json.loads(
        json.dumps(
            {
                "session_id": session.session_id,
                "version": session.version,
                "app": session.app,
                "environment": session.environment,
                "owner_uid": session.owner_uid,
                "records": records,
            }
        )
    )


def session_from_wire(value: dict) -> SessionDescription:
    if not isinstance(value, dict) or value.keys() != {
        "session_id",
        "version",
        "app",
        "environment",
        "owner_uid",
        "records",
    }:
        raise ValueError("Malformed network runtime session")
    if type(value["version"]) is not int or value["version"] != VERSION:
        raise ValueError("Unsupported network runtime session version")
    if not isinstance(value["records"], list):
        raise TypeError("Malformed network runtime manifest records")
    return SessionDescription(
        value["session_id"],
        value["app"],
        value["environment"],
        value["owner_uid"],
        manifest_from_records(value["records"]),
        value["version"],
    )
