"""Persistent first-connect SSH trust, separate from session and helper lifetime."""

from __future__ import annotations

import base64
import json
import os
import re
import stat
import sys
import time
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from hashlib import sha256
from typing import TYPE_CHECKING
from uuid import uuid4

from stelvio.pulumi import get_bin_path
from stelvio.tunnel.assets import NativeAssetError, _no_acl, _validate_cache_directory
from stelvio.tunnel.socks import TransportInterruptedError

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path
    from threading import Event

HOST_KEY_PREFIX = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20"
HOST_KEY_BYTES = 51
MAX_RECORD_BYTES = 4096
LOCK_SECONDS = 10
FILE_MODE = 0o600
DIRECTORY_MODE = 0o700


class HostTrustError(RuntimeError):
    """Local SSH trust is unsafe, invalid, or changed."""


def parse_host_key(key: str) -> str:
    try:
        kind, encoded = key.split()
        wire = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as error:
        raise HostTrustError("Malformed Ed25519 SSH host key") from error
    if (
        kind != "ssh-ed25519"
        or len(wire) != HOST_KEY_BYTES
        or not wire.startswith(HOST_KEY_PREFIX)
    ):
        raise HostTrustError("Malformed Ed25519 SSH host key")
    return f"{kind} {base64.b64encode(wire).decode()}"


def fingerprint(key: str) -> str:
    wire = base64.b64decode(parse_host_key(key).split()[1])
    return "SHA256:" + base64.b64encode(sha256(wire).digest()).decode().rstrip("=")


@dataclass(frozen=True)
class HostIdentity:
    partition: str
    account: str
    region: str
    instance_id: str

    def __post_init__(self) -> None:
        if not (
            re.fullmatch(r"aws(?:-[a-z]+)*", self.partition)
            and re.fullmatch(r"[0-9]{12}", self.account)
            and re.fullmatch(r"[a-z][a-z0-9-]{1,62}", self.region)
            and re.fullmatch(r"i-(?:[0-9a-f]{8}|[0-9a-f]{17})", self.instance_id)
        ):
            raise HostTrustError("Require a valid AWS partition, account, region and instance ID")

    @property
    def scope(self) -> tuple[str, ...]:
        return self.partition, self.account, self.region, self.instance_id

    @property
    def alias(self) -> str:
        return "stelvio-" + sha256("/".join(self.scope).encode()).hexdigest()

    @property
    def label(self) -> str:
        return "/".join(self.scope)


def _acl(path: Path) -> None:
    if sys.platform == "darwin":
        _no_acl(path, "SSH host trust")


def _validate_file(fd: int, path: Path) -> None:
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != FILE_MODE
        or info.st_nlink != 1
    ):
        raise HostTrustError("SSH host trust file has unsafe ownership or permissions")
    _acl(path)


def _unique_fields(pairs: list[tuple[str, object]]) -> dict:
    value = {}
    for key, item in pairs:
        if key in value:
            raise HostTrustError("SSH host trust record contains duplicate fields")
        value[key] = item
    return value


class LockedHostTrust:
    def __init__(self, directory: Path, directory_fd: int, identity: HostIdentity) -> None:
        self.directory = directory
        self.directory_fd = directory_fd
        self.identity = identity
        self.name = identity.alias + ".json"
        self.key = self._read()

    def _read(self) -> str | None:
        try:
            fd = os.open(
                self.name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                dir_fd=self.directory_fd,
            )
        except FileNotFoundError:
            return None
        with os.fdopen(fd, "rb") as stream:
            _validate_file(stream.fileno(), self.directory / self.name)
            data = stream.read(MAX_RECORD_BYTES + 1)
        try:
            value = json.loads(data, object_pairs_hook=_unique_fields)
        except ValueError as error:
            raise HostTrustError(
                "SSH host trust record is invalid; retain it for inspection"
            ) from error
        if (
            len(data) > MAX_RECORD_BYTES
            or not isinstance(value, dict)
            or set(value) != {"identity", "key"}
            or value["identity"] != list(self.identity.scope)
            or not isinstance(value["key"], str)
        ):
            raise HostTrustError("SSH host trust record is invalid; retain it for inspection")
        return parse_host_key(value["key"])

    def accept(self, key: str) -> None:
        key = parse_host_key(key)
        previous = self._read()
        if previous != self.key or (previous is not None and previous != key):
            raise HostTrustError(f"SSH host trust changed for {self.identity.label}")
        if previous is not None:
            return
        name = self.name + "." + uuid4().hex
        fd = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=self.directory_fd,
        )
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(json.dumps({"identity": self.identity.scope, "key": key}).encode())
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.name, src_dir_fd=self.directory_fd, dst_dir_fd=self.directory_fd)
            os.fsync(self.directory_fd)
            self.key = key
        except OSError as error:
            raise HostTrustError("SSH host trust publication failed") from error
        finally:
            with suppress(FileNotFoundError):
                os.unlink(name, dir_fd=self.directory_fd)

    def reset(self, expected: str) -> None:
        if self.key is None or fingerprint(self.key) != expected or self._read() != self.key:
            raise HostTrustError("SSH host trust changed before reset; inspect it again")
        os.unlink(self.name, dir_fd=self.directory_fd)
        os.fsync(self.directory_fd)
        self.key = None


def _wait_lock(fd: int, stop: Event) -> None:
    import fcntl  # noqa: PLC0415 - Unix operation; imports remain platform independent

    deadline = time.monotonic() + LOCK_SECONDS
    while True:
        if stop.is_set():
            raise TransportInterruptedError("SSH host trust operation cancelled")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise HostTrustError(
                    "SSH host trust is busy; retry after the other connection"
                ) from None
            stop.wait(0.05)
        else:
            return


@contextmanager
def locked_host_trust(
    identity: HostIdentity, stop: Event, *, directory: Path | None = None
) -> Iterator[LockedHostTrust]:
    """Hold one bounded interprocess lock across SSH authentication and publication."""
    directory = directory or get_bin_path() / "ssh-host-trust"
    directory_fd = lock_fd = None
    try:
        try:
            if not os.geteuid() or os.getuid() != os.geteuid():
                raise HostTrustError("SSH host trust must be managed by the ordinary nonroot user")
            _validate_cache_directory(directory.parent)
            directory.mkdir(mode=0o700, exist_ok=True)
            directory_fd = os.open(
                directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
            )
            info = os.fstat(directory_fd)
            if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != DIRECTORY_MODE:
                raise HostTrustError(
                    "SSH host trust directory has unsafe ownership or permissions"
                )
            _acl(directory)
            name = identity.alias + ".lock"
            lock_fd = os.open(
                name,
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                0o600,
                dir_fd=directory_fd,
            )
            _validate_file(lock_fd, directory / name)
            _wait_lock(lock_fd, stop)
            entry = LockedHostTrust(directory, directory_fd, identity)
        except TransportInterruptedError:
            raise
        except (OSError, NativeAssetError) as error:
            raise HostTrustError("SSH host trust is unavailable or unsafe") from error
        yield entry
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
        if directory_fd is not None:
            os.close(directory_fd)
