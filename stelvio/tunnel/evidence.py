"""Private durable ownership receipts for SSM transport startup and cleanup."""

from __future__ import annotations

import json
import os
import re
import stat
from contextlib import suppress
from typing import TYPE_CHECKING
from uuid import uuid4

from stelvio.pulumi import get_bin_path
from stelvio.tunnel.assets import NativeAssetError, _no_acl, _validate_cache_directory

if TYPE_CHECKING:
    from pathlib import Path

_NAME = re.compile(r"transport-(start|observed|stop)-[0-9a-f]{32}\.json\Z")
_MAX_RECORD = 4096
_MAX_VALUE = 1024
_PRIVATE_DIRECTORY_MODE = 0o700
_PRIVATE_FILE_MODE = 0o600


class TransportEvidenceError(RuntimeError):
    """The private transport ownership journal cannot safely be updated."""


def _directory() -> Path:
    root = get_bin_path()
    try:
        _validate_cache_directory(root)
    except NativeAssetError:
        raise TransportEvidenceError(
            "Private transport evidence directory is unavailable"
        ) from None
    directory = root / "tunnel-evidence"
    try:
        directory.mkdir(mode=0o700)
    except FileExistsError:
        pass
    except OSError:
        raise TransportEvidenceError(
            "Private transport evidence directory is unavailable"
        ) from None
    try:
        info = directory.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != _PRIVATE_DIRECTORY_MODE
        ):
            raise TransportEvidenceError("Private transport evidence directory is unsafe")
        _no_acl(directory, "Transport evidence directory")
    except (OSError, NativeAssetError):
        raise TransportEvidenceError("Private transport evidence directory is unsafe") from None
    return directory


def _validate_record(name: str, value: dict) -> bytes:
    match = _NAME.fullmatch(name) if isinstance(name, str) else None
    if match is None or not isinstance(value, dict):
        raise TransportEvidenceError("Invalid transport ownership receipt")
    phase = match.group(1)
    fields = {
        "start": {"instance", "reason"},
        "observed": {"session_id"},
        "stop": {"complete"},
    }[phase]
    if value.keys() != fields:
        raise TransportEvidenceError("Invalid transport ownership receipt fields")
    if phase == "stop":
        if value["complete"] is not True:
            raise TransportEvidenceError("Invalid transport completion receipt")
    elif any(
        not isinstance(item, str)
        or not item
        or len(item) > _MAX_VALUE
        or any(not char.isprintable() for char in item)
        for item in value.values()
    ):
        raise TransportEvidenceError("Invalid transport ownership receipt value")
    try:
        wire = json.dumps(value, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise TransportEvidenceError("Invalid transport ownership receipt value") from None
    if len(wire) > _MAX_RECORD:
        raise TransportEvidenceError("Transport ownership receipt exceeds its bound")
    return wire


def record_transport_event(name: str, value: dict) -> None:
    """Atomically publish one immutable, bounded receipt without AWS credentials."""
    directory = _directory()
    wire = _validate_record(name, value)
    temporary = directory / f".receipt-{uuid4().hex}.tmp"
    target = directory / name
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(temporary, flags, _PRIVATE_FILE_MODE)
        _write_all(descriptor, wire)
        os.fsync(descriptor)
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != _PRIVATE_FILE_MODE
        ):
            raise TransportEvidenceError("Transport receipt staging file is unsafe")
        os.close(descriptor)
        descriptor = -1
        # A hard link publishes without replacing any existing evidence.
        os.link(temporary, target, follow_symlinks=False)
        temporary.unlink()
        folder = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(folder)
        finally:
            os.close(folder)
    except (OSError, NativeAssetError):
        raise TransportEvidenceError(
            "Could not preserve transport cleanup ownership evidence"
        ) from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        with suppress(FileNotFoundError):
            temporary.unlink()


def _write_all(descriptor: int, wire: bytes) -> None:
    while wire:
        written = os.write(descriptor, wire)
        if written <= 0:
            raise OSError("short transport receipt write")
        wire = wire[written:]
