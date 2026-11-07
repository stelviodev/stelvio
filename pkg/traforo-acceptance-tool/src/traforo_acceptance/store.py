"""Private durable records and deterministic report inputs."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

# Candidate unit tests monkeypatch process-global OS functions. Reporting must
# keep its own original adapters so those tests cannot intercept evidence writes.
_OPEN, _WRITE, _CLOSE = os.open, os.write, os.close
_FLOCK, _FSYNC = fcntl.flock, os.fsync
_DUMPS = json.dumps
_MONOTONIC = time.monotonic

SCHEMA = 1
SECRET = re.compile(
    r"(?i)(\b(?:aws_secret_access_key|aws_session_token|github_token|password|tokenvalue)"
    r"\b[\\\"']*\s*[:=]\s*[\\\"']*)([^\s\\\",}\]]+)"
)


def redact(text: str) -> str:
    # Values from inherited secret variables must not enter command logs or reports.
    for key, value in os.environ.items():
        if len(value) >= 8 and any(s in key.upper() for s in ("TOKEN", "SECRET", "PASSWORD")):
            text = text.replace(value, "[REDACTED]")
    text = SECRET.sub(r"\1[REDACTED]", text)
    return re.sub(r"(mongodb(?:\+srv)?://[^:/\s]+:)[^@\s]+@", r"\1[REDACTED]@", text)


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def sanitize(value):
    """Redact values before JSON encoding; never modify serialized delimiters."""
    if isinstance(value, dict):
        protected = {
            "password",
            "secretstring",
            "aws_secret_access_key",
            "aws_session_token",
            "github_token",
            "tokenvalue",
        }
        return {
            key: "[REDACTED]" if str(key).lower() in protected else sanitize(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [sanitize(item) for item in value]
    return redact(value) if isinstance(value, str) else value


def digest(path: Path) -> str:
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def write_json(path: Path, value) -> None:
    temporary = path.with_name(path.name + ".new")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as file:
        file.write(json.dumps(sanitize(value), indent=2, sort_keys=True, default=str) + "\n")
        file.flush()
        os.fsync(file.fileno())
    temporary.replace(path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def event(root: Path, kind: str, **fields) -> None:
    line = (
        _DUMPS(
            sanitize({"at": now(), "monotonic": _MONOTONIC(), "kind": kind, **fields}), default=str
        )
        + "\n"
    )
    fd = _OPEN(root / "events.jsonl", os.O_APPEND | os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        _FLOCK(fd, fcntl.LOCK_EX)
        _WRITE(fd, line.encode())
        _FSYNC(fd)
    finally:
        _CLOSE(fd)


def events(root: Path) -> list[dict]:
    path = root / "events.jsonl"
    if not path.exists():
        return []
    result = []
    for line in path.read_text().splitlines():
        try:
            result.append(json.loads(line))
        except ValueError:
            # A killed writer must not hide earlier evidence.
            result.append({"kind": "damaged_event", "line": line})
    return result


class Store:
    def __init__(self, root: Path):
        self.root = root.absolute()
        info = self.root.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or self.root.resolve() != self.root
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            raise RuntimeError("Run directory must be private, user-owned, and not a symlink")

    @property
    def data(self):
        value = json.loads((self.root / "run.json").read_text())
        if value["schema"] != SCHEMA:
            raise RuntimeError("Unsupported run manifest schema")
        return value

    def save(self, data):
        write_json(self.root / "run.json", data)

    @contextmanager
    def locked(self):
        fd = os.open(self.root / "run.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        except BlockingIOError as error:
            raise RuntimeError("Another process owns this run") from error
        finally:
            os.close(fd)
