"""Bounded snapshot framing, peer authentication, and observation envelopes."""

from __future__ import annotations

import array
import ctypes
import json
import os
import socket
import struct
import time
from datetime import UTC, datetime
from typing import Any

VERSION = 1
MAX_MESSAGE = 1024 * 1024
SOURCE_LIMIT = 256 * 1024
STALE_SECONDS = 5
MAX_EPOCH_LENGTH = 128


def timestamp() -> str:
    return datetime.now(UTC).isoformat()


def peer_uid(connection: socket.socket) -> int:
    if hasattr(socket, "SO_PEERCRED"):
        return struct.unpack(
            "3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        )[1]
    library = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    library.getpeereid.argtypes = [
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_uint),
        ctypes.POINTER(ctypes.c_uint),
    ]
    library.getpeereid.restype = ctypes.c_int
    uid, gid = ctypes.c_uint(), ctypes.c_uint()
    if library.getpeereid(connection.fileno(), ctypes.byref(uid), ctypes.byref(gid)):
        raise OSError(ctypes.get_errno(), "Cannot authenticate diagnostics peer")
    return uid.value


def _unique(pairs: list[tuple[str, Any]]) -> dict:
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate diagnostics key")
        value[key] = item
    return value


def decode(data: bytes) -> dict:
    def reject_constant(_value: str) -> None:
        raise ValueError("Nonfinite diagnostics number")

    value = json.loads(data, object_pairs_hook=_unique, parse_constant=reject_constant)
    if not isinstance(value, dict):
        raise TypeError("Diagnostics message must be an object")
    return value


def receive(connection: socket.socket, limit: int = MAX_MESSAGE) -> dict:
    deadline = time.monotonic() + (connection.gettimeout() or 2)

    def exact(size: int) -> bytes:
        result = bytearray()
        while len(result) < size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Diagnostics request timed out")
            connection.settimeout(remaining)
            part, controls, flags, _ = connection.recvmsg(size - len(result), 4096)
            for level, kind, data in controls:
                if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                    values = array.array("i")
                    values.frombytes(data[: len(data) - len(data) % values.itemsize])
                    for descriptor in values:
                        os.close(descriptor)
            if controls or flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC):
                raise ValueError("Descriptors are forbidden in diagnostics")
            if not part:
                raise EOFError("Diagnostics source closed")
            result.extend(part)
        return bytes(result)

    size = struct.unpack("!I", exact(4))[0]
    if not 0 < size <= limit:
        raise ValueError("Diagnostics message exceeds its bound")
    return decode(exact(size))


def send(connection: socket.socket, value: dict, limit: int = MAX_MESSAGE) -> None:
    data = json.dumps(value, allow_nan=False, separators=(",", ":")).encode()
    if not 0 < len(data) <= limit:
        raise ValueError("Diagnostics message exceeds its bound")
    connection.sendall(struct.pack("!I", len(data)) + data)


def request(after_sequence: int = 0, *, resource_checks: bool = False) -> dict:
    if type(after_sequence) is not int or after_sequence < 0:
        raise ValueError("Invalid event sequence")
    value = {"version": VERSION, "operation": "snapshot", "after_sequence": after_sequence}
    if resource_checks:
        value["resource_checks"] = True
    return value


def validate_request(value: dict) -> int:
    if set(value) - {"resource_checks"} != {"version", "operation", "after_sequence"}:
        raise ValueError("Invalid diagnostics request fields")
    if "resource_checks" in value and type(value["resource_checks"]) is not bool:
        raise ValueError("Invalid resource checks flag")
    if type(value["version"]) is not int or value["version"] != VERSION:
        raise ValueError("Unsupported diagnostics version")
    if value["operation"] != "snapshot":
        raise ValueError("Diagnostics supports snapshots only")
    request(value["after_sequence"])
    return value["after_sequence"]


def observation(
    data: object = None, *, status: str = "collected", code: str | None = None
) -> dict:
    return {
        "status": status,
        "observed_at": timestamp(),
        "age_seconds": 0.0,
        "stale": False,
        "error": {"code": code} if code else None,
        "data": data,
    }


def validate_snapshot(value: dict) -> dict:
    if type(value.get("version")) is not int or value["version"] != VERSION:
        raise ValueError("Unsupported diagnostics snapshot version")
    if (
        not isinstance(value.get("epoch"), str)
        or not value["epoch"]
        or len(value["epoch"]) > MAX_EPOCH_LENGTH
    ):
        raise ValueError("Missing diagnostics epoch")
    sample = value.get("sample_monotonic_ns")
    if type(sample) is not int or not 0 <= sample < (1 << 64):
        raise ValueError("Invalid diagnostics sample time")
    return value
