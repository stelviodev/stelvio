"""Bounded, read-only session discovery and snapshot acquisition."""

from __future__ import annotations

import contextlib
import time
from collections import deque
from typing import TYPE_CHECKING

from stelvio.tunnel.diagnostics import runtime
from stelvio.tunnel.monitor.model import identity, validate

if TYPE_CHECKING:
    from pathlib import Path


DISCOVERY_SECONDS = 2


class MonitorClient:
    """Called serially by one UI worker; no overlapping polls or network ownership."""

    def __init__(self, pinned: str | None = None) -> None:
        self.pinned = pinned
        self.selected = pinned
        self.match = None
        self.sessions = {}
        self.paths = {}
        self.pending = deque()
        self.last_discovery = float("-inf")
        self.sequences = {}

    def _read(self, path: Path) -> dict:
        session_id = path.stem
        old = self.sessions.get(session_id, {})
        value = validate(
            runtime.snapshot(
                path,
                self.sequences.get(session_id, 0),
                resource_checks=(
                    session_id == self.selected
                    and "resource_checks" in old.get("capabilities", [])
                ),
            )
        )
        if old.get("epoch") != value["epoch"] and self.sequences.get(session_id):
            value = validate(runtime.snapshot(path))
        self.sessions[session_id] = value
        self.paths[session_id] = path
        return value

    def poll(self, requested: str | None = None) -> dict:  # noqa: C901, PLR0912 - bounded session discovery and reconnection
        if (
            requested is not None
            and requested != self.selected
            and self.pinned is None
            and requested in self.sessions
        ):
            self.selected = requested
            self.sequences[requested] = 0
            self.match = identity(self.sessions[requested])
        now = time.monotonic()
        error = None
        if now - self.last_discovery >= DISCOVERY_SECONDS:
            self.last_discovery = now
            try:
                paths = runtime.discover()
                live_paths = set(paths)
                for session_id, path in list(self.paths.items()):
                    if path not in live_paths:
                        self.paths.pop(session_id, None)
                        self.sessions.pop(session_id, None)
                if not self.pending:
                    self.pending.extend(paths)
                # Incremental discovery bounds each UI worker to two exchanges (four seconds).
                for _ in range(min(1, len(self.pending))):
                    path = self.pending.popleft()
                    if (
                        path.stem == self.selected and path.stem in self.paths
                    ) or path not in live_paths:
                        continue
                    try:
                        self._read(path)
                    except (OSError, ValueError, TypeError, KeyError, EOFError):
                        self.sessions.pop(path.stem, None)
                        self.paths.pop(path.stem, None)
            except (OSError, ValueError):
                error = "Session discovery unavailable"
        if self.selected is None and len(self.sessions) == 1 and not self.pending:
            self.selected = next(iter(self.sessions))
        if (
            self.selected not in self.paths
            and self.pinned is None
            and self.match is not None
            and not self.pending
        ):
            matches = [
                key for key, value in self.sessions.items() if identity(value) == self.match
            ]
            if len(matches) == 1:
                self.selected = matches[0]
        value = None
        path = self.paths.get(self.selected)
        # Discover and safety-check pinned UUIDs; never accept an arbitrary socket path.
        if self.pinned and path is None:
            with contextlib.suppress(OSError, ValueError):
                path = next(
                    (item for item in runtime.discover() if item.stem == self.pinned), None
                )
        if path is not None:
            try:
                value = self._read(path)
                self.match = identity(value)
                self.sequences[self.selected] = value.get("events", {}).get("newest_sequence", 0)
            except (OSError, ValueError, TypeError, KeyError, EOFError):
                error = "Session disconnected or snapshot unavailable"
                self.sessions.pop(self.selected, None)
                self.paths.pop(self.selected, None)
        return {"snapshot": value, "sessions": list(self.sessions.values()), "error": error}
