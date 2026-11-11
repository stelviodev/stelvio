"""Bounded private runtime IPC; only deployment identities cross this channel."""

from __future__ import annotations

import json
import struct
from dataclasses import asdict
from typing import TYPE_CHECKING

from stelvio.network import VERSION, manifest_from_records
from stelvio.tunnel.helper_client import MAX_CONFIGURE_STAGE
from stelvio.tunnel.session_types import SESSION_VERSION, SessionDescription

if TYPE_CHECKING:
    import socket

MAX_MESSAGE = 1024 * 1024
MAX_STATUS_DETAIL_LENGTH = 1024
HOST_CONFIGURE_FAILURE_CAUSES = frozenset(
    f"host-uncertain-configure-{stage}" for stage in range(1, MAX_CONFIGURE_STAGE + 1)
)
STATUS_DETAIL_CAUSES = HOST_CONFIGURE_FAILURE_CAUSES | {"ssh-host-key", "cleanup-incomplete"}


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
    for component_type, values in (
        ("stelvio:aws:Vpc", session.manifest.vpcs),
        ("stelvio:aws:DocumentDb", session.manifest.resources),
    ):
        for record in values:
            metadata = asdict(record)
            identity = metadata.pop("identity")
            records.append(
                {
                    "type": component_type,
                    "urn": identity,
                    "metadata": {"version": VERSION, **metadata},
                }
            )
    # JSON turns immutable subnet-ID tuples into the deployment record's lists.
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
    if type(value["version"]) is not int or value["version"] != SESSION_VERSION:
        raise ValueError("Unsupported network runtime session version")
    if not isinstance(value["records"], list):
        raise TypeError("Malformed network runtime manifest records")
    records = []
    for item in value["records"]:
        if (
            not isinstance(item, dict)
            or item.keys() != {"type", "urn", "metadata"}
            or not isinstance(item["type"], str)
            or not isinstance(item["urn"], str)
            or not item["urn"]
            or any(char.isspace() or not char.isprintable() for char in item["urn"])
        ):
            raise ValueError("Malformed network runtime record descriptor")
        records.append((item["type"], item["urn"], item["metadata"]))
    return SessionDescription(
        value["session_id"],
        value["app"],
        value["environment"],
        value["owner_uid"],
        manifest_from_records(records),
        value["version"],
    )
