"""User-private session discovery, snapshot service, and demand-driven sampling."""

from __future__ import annotations

import os
import socket
import stat
import sys
import time
from collections import deque
from contextlib import suppress
from copy import deepcopy
from pathlib import Path
from threading import BoundedSemaphore, Event, Lock, Thread
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from stelvio.tunnel.diagnostics.protocol import (
    STALE_SECONDS,
    observation,
    peer_uid,
    receive,
    request,
    send,
    timestamp,
    validate_request,
    validate_snapshot,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from stelvio.tunnel.diagnostics.resource_checks import ResourceChecks


DIRECTORY_MODE = 0o700
SOCKET_MODE = 0o600
MAX_ENDPOINTS = 64
MAX_VPCS = 8
MAX_DIRECTORY_ENTRIES = 4096


def directory() -> Path:
    root = "/tmp" if sys.platform == "linux" else "/private/tmp"  # noqa: S108 - owned private child
    return Path(f"{root}/stlv-diag-{os.geteuid()}")


def _safe_directory(path: Path) -> None:
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != DIRECTORY_MODE
    ):
        raise ValueError("Unsafe diagnostics directory")


def _safe_socket(path: Path) -> None:
    _safe_directory(path.parent)
    info = path.lstat()
    if (
        not stat.S_ISSOCK(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != SOCKET_MODE
    ):
        raise ValueError("Unsafe diagnostics endpoint")


def discover() -> list[Path]:
    path = directory()
    try:
        _safe_directory(path)
    except FileNotFoundError:
        return []
    result = []
    # The runtime uses UUID filenames; never interpret an arbitrary directory entry as a PID.
    with os.scandir(path) as entries:
        for index, entry in enumerate(entries):
            if index >= MAX_DIRECTORY_ENTRIES or len(result) == MAX_ENDPOINTS:
                break
            if entry.name.endswith(".sock"):
                candidate = path / entry.name
                try:
                    _safe_socket(candidate)
                    result.append(candidate)
                except (OSError, ValueError):
                    continue
    return sorted(result)


def snapshot(path: Path, after_sequence: int = 0, *, resource_checks: bool = False) -> dict:
    _safe_socket(path)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(2)
        connection.connect(str(path))
        if peer_uid(connection) != os.geteuid():
            raise ValueError("Diagnostics peer has another owner")
        send(connection, request(after_sequence, resource_checks=resource_checks))
        value = validate_snapshot(receive(connection))
        if not isinstance(value.get("session"), dict) or not isinstance(
            value.get("sources"), dict
        ):
            raise TypeError("Malformed session diagnostics")
        session = value["session"]
        if str(UUID(session.get("session_id", ""))) != path.stem:
            raise ValueError("Diagnostics session identity differs from its endpoint")
        vpcs = session.get("vpcs")
        if (
            not isinstance(vpcs, list)
            or len(vpcs) > MAX_VPCS
            or any(not isinstance(vpc, dict) for vpc in vpcs)
        ):
            raise TypeError("Malformed diagnostics VPC observations")
        for source in value["sources"].values():
            if (
                not isinstance(source, dict)
                or source.get("status") not in {"collected", "unavailable", "skipped", "failed"}
                or not isinstance(source.get("data"), (dict, type(None)))
                or type(source.get("stale")) is not bool
            ):
                raise TypeError("Malformed diagnostics source")
        return value


class Sampler:
    """One bounded poll per source per second; no idle background collector."""

    def __init__(self, sources: dict[str, Callable[[], dict]]) -> None:
        self.sources = sources
        self.lock = Lock()
        self.values = {}
        self.attempts = {}
        self.observed = {}

    def collect(self) -> dict:
        # The only callers are bounded diagnostic workers, never lifecycle workers.
        with self.lock:
            for name, collect in self.sources.items():
                now = time.monotonic()
                if now - self.attempts.get(name, float("-inf")) < 1:
                    continue
                self.attempts[name] = now
                try:
                    self.values[name] = observation(collect())
                    self.observed[name] = time.monotonic()
                except Exception as error:
                    previous = self.values.get(name, observation(status="unavailable"))
                    self.values[name] = dict(
                        previous, status="unavailable", error={"code": type(error).__name__}
                    )
            result = deepcopy(self.values)
            now = time.monotonic()
            for name, value in result.items():
                value["age_seconds"] = now - self.observed[name] if name in self.observed else None
                value["stale"] = name not in self.observed or value["age_seconds"] > STALE_SECONDS
            return result


class Events:
    def __init__(self) -> None:
        self.lock = Lock()
        self.entries = deque(maxlen=256)
        self.sequence = 0

    def append(self, code: str, unit: str | None = None, detail: str | None = None) -> None:
        with self.lock:
            self.sequence += 1
            self.entries.append(
                {
                    "sequence": self.sequence,
                    "timestamp": timestamp(),
                    "unit": unit,
                    "code": code[:64],
                    "detail": detail[:1024] if detail else None,
                }
            )

    def after(self, sequence: int) -> dict:
        with self.lock:
            oldest = self.entries[0]["sequence"] if self.entries else self.sequence + 1
            return {
                "oldest_sequence": oldest,
                "newest_sequence": self.sequence,
                "gap": sequence < oldest - 1,
                "entries": [dict(item) for item in self.entries if item["sequence"] > sequence],
            }


class RuntimeDiagnostics:
    def __init__(
        self,
        session_id: str,
        state: Callable[[], dict],
        sources: dict[str, Callable[[], dict]],
        events: Events,
        resource_checks: ResourceChecks | None = None,
    ) -> None:
        self.path = directory() / f"{session_id}.sock"
        self.state = state
        self.events = events
        self.resource_checks = resource_checks
        self.sampler = Sampler(sources)
        self.epoch = str(uuid4())
        self.stop = Event()
        self.slots = BoundedSemaphore(4)
        self.clients_lock = Lock()
        self.clients = set()
        self.listener = None
        self.inode = None

    def start(self) -> None:
        self.path.parent.mkdir(mode=0o700, exist_ok=True)
        _safe_directory(self.path.parent)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(self.path))
            self.inode = self.path.lstat().st_ino
            self.path.chmod(0o600)
            listener.listen(4)
            listener.settimeout(0.2)
            self.listener = listener
            Thread(target=self._accept, daemon=True, name="stelvio-diagnostics").start()
        except BaseException:
            listener.close()
            self.close()
            raise

    def _accept(self) -> None:
        while not self.stop.is_set():
            try:
                connection, _ = self.listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            if not self.slots.acquire(blocking=False):
                connection.close()
                continue
            with self.clients_lock:
                if self.stop.is_set():
                    connection.close()
                    self.slots.release()
                    return
                self.clients.add(connection)
            Thread(target=self._serve, args=(connection,), daemon=True).start()

    def _serve(self, connection: socket.socket) -> None:
        try:
            connection.settimeout(2)
            if peer_uid(connection) != os.geteuid():
                return
            deadline = time.monotonic() + 2
            query = receive(connection, 4096)
            sequence = validate_request(query)
            value = {
                "version": 1,
                "epoch": self.epoch,
                "sample_monotonic_ns": time.monotonic_ns(),
                "session": self.state(),
                "sources": self.sampler.collect(),
                "events": self.events.after(sequence),
            }
            if self.resource_checks is not None:
                value["capabilities"] = ["resource_checks"]
                value["resource_checks"] = self.resource_checks.collect(
                    value["session"], requested=query.get("resource_checks", False)
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            connection.settimeout(remaining)
            send(connection, value)
        except (OSError, EOFError, ValueError, TypeError, RecursionError):
            pass
        finally:
            with self.clients_lock:
                self.clients.discard(connection)
            connection.close()
            self.slots.release()

    def close(self) -> None:
        self.stop.set()
        if self.listener:
            self.listener.close()
        with self.clients_lock:
            for connection in self.clients:
                with suppress(OSError):
                    connection.shutdown(socket.SHUT_RDWR)
        with suppress(OSError):
            info = self.path.lstat()
            if info.st_ino == self.inode and stat.S_ISSOCK(info.st_mode):
                self.path.unlink()
