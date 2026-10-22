"""Read-only certification of owned SSM cleanup for CLI acceptance."""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
import time
import warnings
from datetime import datetime
from pathlib import Path

from stelvio.tunnel.assets import _no_acl, _validate_cache_directory
from stelvio.tunnel.evidence import _MAX_RECORD, _validate_record

_REASON = re.compile(r"stelvio/[0-9a-f]{16}/([0-9a-f]{32})\Z")
_FIELDS = ("SessionId", "Target", "Reason", "Status", "EndDate")


def _private_record(directory: Path, name: str) -> dict:
    _validate_cache_directory(directory.parent)
    info = directory.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise AssertionError("Unsafe transport receipt directory")
    _no_acl(directory, "Transport receipt directory")
    folder = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        opened = os.fstat(folder)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise AssertionError("Transport receipt directory changed")
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder)
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != os.geteuid()
                or stat.S_IMODE(before.st_mode) != 0o600
                or before.st_nlink != 1
                or before.st_size > _MAX_RECORD
            ):
                raise AssertionError("Unsafe transport receipt file")
            _no_acl(directory / name, "Transport receipt file")
            wire = stream.read(_MAX_RECORD + 1)
            after = os.fstat(stream.fileno())
            if len(wire) > _MAX_RECORD or (
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            ) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise AssertionError("Transport receipt changed or exceeded its bound")
    finally:
        os.close(folder)
    value = json.loads(wire, object_pairs_hook=_unique_fields)
    _validate_record(name, value)
    return value


def _unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise AssertionError("Duplicate transport receipt field")
        result[key] = value
    return result


def _captured(receipts):
    result = {}
    if not 1 <= len(receipts) <= 8:
        raise AssertionError("Expected one to eight owned SSM sessions")
    for value in receipts:
        selected = {key: value.get(key) for key in ("SessionId", "Target", "Reason")}
        if any(
            not isinstance(item, str)
            or not item
            or len(item) > 1024
            or any(not char.isprintable() for char in item)
            for item in selected.values()
        ) or not _REASON.fullmatch(selected["Reason"]):
            raise AssertionError("Invalid captured SSM ownership")
        identifier = selected["SessionId"]
        if identifier in result:
            raise AssertionError("Duplicate captured SSM session")
        result[identifier] = selected
    return result


def _completion(directory, expected):
    identifier = _REASON.fullmatch(expected["Reason"]).group(1)
    values = {
        phase: _private_record(directory, f"transport-{phase}-{identifier}.json")
        for phase in ("start", "observed", "stop")
    }
    if (
        values["start"] != {"instance": expected["Target"], "reason": expected["Reason"]}
        or values["observed"] != {"session_id": expected["SessionId"]}
        or values["stop"] != {"complete": True}
    ):
        raise AssertionError("Transport receipt ownership does not match captured session")
    return values


def _sessions(ssm, deadline, **query):
    pages = iter(
        ssm.get_paginator("describe_sessions").paginate(**query, PaginationConfig={"PageSize": 50})
    )
    for _ in range(20):
        if time.monotonic() >= deadline:
            raise AssertionError("SSM cleanup observation deadline exceeded")
        page = next(pages, None)
        if time.monotonic() >= deadline:
            raise AssertionError("SSM cleanup observation deadline exceeded")
        if page is None:
            return
        if not isinstance(page, dict) or not isinstance(page.get("Sessions"), list):
            raise TypeError("Invalid SSM session inventory")
        if len(page["Sessions"]) > 50:
            raise AssertionError("SSM session inventory exceeds its bound")
        yield from page["Sessions"]
        if not page.get("NextToken"):
            return
    raise AssertionError("SSM session pagination exceeds its bound")


def _owned(row, expected):
    if not isinstance(row, dict) or any(row.get(k) != v for k, v in expected.items()):
        raise AssertionError("SSM inventory ownership does not match captured session")
    if row.get("Status") not in {
        "Connected",
        "Connecting",
        "Disconnected",
        "Terminated",
        "Terminating",
        "Failed",
    }:
        raise AssertionError("Invalid SSM status")
    if "EndDate" in row and (
        not isinstance(row["EndDate"], datetime) or row["EndDate"].tzinfo is None
    ):
        raise AssertionError("Invalid SSM end time")
    return {key: row[key] for key in _FIELDS if key in row}


def _history(ssm, deadline, expected):
    history = {}
    for key, value in expected.items():
        rows = list(
            _sessions(ssm, deadline, State="History", Filters=[{"key": "SessionId", "value": key}])
        )
        if len(rows) > 1:
            raise AssertionError("Duplicate SSM history records")
        if not rows:
            continue
        row = _owned(rows[0], value)
        if row["Status"] not in {"Terminated", "Terminating"}:
            raise AssertionError("Unexpected SSM history state")
        if row["Status"] == "Terminating" and "EndDate" not in row:
            raise AssertionError("Terminating history lacks a valid end time")
        history[key] = row
    return history


def _active(ssm, deadline, expected):
    active = []
    for target in sorted({v["Target"] for v in expected.values()}):
        for row in _sessions(
            ssm, deadline, State="Active", Filters=[{"key": "Target", "value": target}]
        ):
            if not isinstance(row, dict):
                raise TypeError("Invalid active SSM session")
            if row.get("SessionId") in expected:
                active.append(_owned(row, expected[row["SessionId"]]))
    return active


def _poll_cleanup(ssm, expected, directory, deadline, evidence):
    absent = 0
    while time.monotonic() < deadline:
        try:
            evidence["completion_receipts"] = {
                key: _completion(directory, value) for key, value in expected.items()
            }
        except FileNotFoundError:
            absent = 0
            evidence["observations"].append({"completion_receipts": "pending"})
            time.sleep(min(1, max(0, deadline - time.monotonic())))
            continue
        active = _active(ssm, deadline, expected)
        history = _history(ssm, deadline, expected)
        absent = absent + 1 if not active else 0
        evidence["observations"].append(
            {"active": active, "history": history, "consecutive_absent": absent}
        )
        if absent >= 3 and len(history) == len(expected):
            if time.monotonic() >= deadline:
                raise AssertionError("SSM cleanup observation deadline exceeded")
            evidence["functional_cleanup"] = True
            evidence["history_finalized"] = all(
                row["Status"] == "Terminated" for row in history.values()
            )
            return
        time.sleep(min(1, max(0, deadline - time.monotonic())))
    raise AssertionError("Owned SSM cleanup was not certified within its observation budget")


def assert_ssm_cleanup(ssm, receipts, directory: Path, output: Path, *, timeout=60):
    """Require completion receipts and stable inactivity; report delayed history."""
    if timeout <= 0:
        raise ValueError("SSM cleanup timeout must be positive")
    evidence = {"functional_cleanup": False, "history_finalized": False, "observations": []}
    descriptor, destination = tempfile.mkstemp(prefix="ssm-cleanup-", suffix=".json", dir=output)
    try:
        expected = _captured(receipts)
        evidence["sessions"] = expected
        _poll_cleanup(ssm, expected, directory, time.monotonic() + timeout, evidence)
        if not evidence["history_finalized"]:
            warnings.warn(
                "SSM history finalization delayed; functional cleanup verified. "
                f"Evidence: {destination}",
                stacklevel=2,
            )
        return Path(destination)
    except Exception as error:
        evidence["error_type"] = type(error).__name__
        error.add_note(f"SSM cleanup evidence: {destination}")
        raise
    finally:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(evidence, stream, indent=2, default=lambda value: value.isoformat())
            stream.flush()
            os.fsync(stream.fileno())
