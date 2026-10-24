"""Pure dashboard projection, fenced counters, and bounded local history."""

from __future__ import annotations

import math
from collections import deque
from copy import deepcopy

from stelvio.tunnel.diagnostics.protocol import STALE_SECONDS

MAX_PORT = 65535

DROP_FIELDS = (
    "stale_generation_drops",
    "destination_rejected_drops",
    "carrier_backpressure_drops",
    "carrier_write_error_drops",
    "device_write_drops",
    "device_error_drops",
)


def number(value: object) -> bool:
    return type(value) in (int, float) and 0 <= value < (1 << 64) and math.isfinite(value)


def identity(snapshot: dict) -> tuple:
    session = snapshot["session"]
    networks = tuple(
        sorted(
            (item["identity"], item["vpc_id"], item["account"], item["region"])
            for item in session.get("records", [])
            if item.get("kind") == "vpc"
        )
    )
    return session.get("app"), session.get("environment"), networks


def _records(session: dict) -> None:
    records = session.get("records", [])
    if not isinstance(records, list) or any(not isinstance(item, dict) for item in records):
        raise TypeError("Invalid deployment records")
    for record in records:
        if record.get("kind") == "vpc":
            if any(
                not isinstance(record.get(key), str)
                for key in ("identity", "vpc_id", "account", "region")
            ):
                raise TypeError("Invalid VPC record")
            if not isinstance(record.get("access", {}), (dict, type(None))):
                raise TypeError("Invalid bastion record")


def _vpc(vpc: dict) -> None:  # noqa: C901, PLR0912 - validate the optional runtime fields at the boundary
    if not isinstance(vpc, dict) or not isinstance(vpc.get("identity"), str):
        raise TypeError("Invalid VPC")
    if "unit" in vpc and not isinstance(vpc["unit"], str):
        raise TypeError("Invalid VPC unit")
    if not isinstance(vpc.get("checks", {}), dict):
        raise TypeError("Invalid checks")
    for field in ("ranges", "resources"):
        if not isinstance(vpc.get(field, []), list):
            raise TypeError("Invalid VPC list")
    if any(not isinstance(item, str) for item in vpc.get("ranges", [])):
        raise TypeError("Invalid ranges")
    for resource in vpc.get("resources", []):
        if not isinstance(resource, dict) or not isinstance(resource.get("hostnames", []), list):
            raise TypeError("Invalid resource")
        if not isinstance(resource.get("identity", ""), str):
            raise TypeError("Invalid resource identity")
        ports = resource.get("ports", [])
        if not isinstance(ports, list) or any(
            type(port) is not int or not 0 < port <= MAX_PORT for port in ports
        ):
            raise TypeError("Invalid resource ports")
        if any(not isinstance(host, str) for host in resource.get("hostnames", [])):
            raise TypeError("Invalid resource host")
    for field in ("generation", "attempt", "dns_revision", "connected_seconds"):
        if vpc.get(field) is not None and not number(vpc[field]):
            raise ValueError("Invalid VPC counter")


def _source(data: dict) -> None:  # noqa: C901 - validate independently versioned source records
    if not isinstance(data, dict):
        raise TypeError("Invalid source")
    if "sample_monotonic_ns" in data and not number(data["sample_monotonic_ns"]):
        raise ValueError("Invalid source time")
    units = data.get("units", [])
    if not isinstance(units, list):
        raise TypeError("Invalid source units")
    seen = set()
    for unit in units:
        if not isinstance(unit, dict) or not isinstance(unit.get("unit"), str):
            raise TypeError("Invalid source unit")
        if unit["unit"] in seen:
            raise ValueError("Duplicate source unit")
        seen.add(unit["unit"])
        for field in ("generation", "attempt", "dns_revision"):
            if field in unit and (type(unit[field]) is not int or not number(unit[field])):
                raise ValueError("Invalid source identity")
        for field in ("counters", "tcp"):
            counters = unit.get(field, {})
            if not isinstance(counters, dict) or any(
                type(value) is not int or not number(value) for value in counters.values()
            ):
                raise ValueError("Invalid source counters")


def _events(value: dict) -> None:
    events = value.get("events", {})
    if not isinstance(events, dict) or not isinstance(events.get("entries", []), list):
        raise TypeError("Invalid events")
    for field in ("oldest_sequence", "newest_sequence"):
        if field in events and (type(events[field]) is not int or not number(events[field])):
            raise ValueError("Invalid event sequence")
    for entry in events.get("entries", []):
        if (
            not isinstance(entry, dict)
            or type(entry.get("sequence")) is not int
            or not number(entry.get("sequence"))
        ):
            raise ValueError("Invalid event")
    checks = value.get("resource_checks", {})
    if not isinstance(checks, dict) or not isinstance(checks.get("endpoints", []), list):
        raise TypeError("Invalid resource checks")
    for item in checks.get("endpoints", []):
        if not isinstance(item, dict) or any(
            not isinstance(item.get(key), str) for key in ("unit", "resource", "host", "result")
        ):
            raise TypeError("Invalid endpoint checks")
        if any(
            type(item.get(key)) is not int or not number(item[key])
            for key in ("generation", "attempt", "dns_revision", "port")
        ):
            raise TypeError("Invalid endpoint identity")


def validate(value: dict) -> dict:
    """Validate fields consumed by the UI, including optional older-runtime data."""
    session = value["session"]
    if not isinstance(value["epoch"], str) or not isinstance(session["session_id"], str):
        raise TypeError("Invalid session")
    _records(session)
    if not isinstance(session["vpcs"], list):
        raise TypeError("Invalid VPC observations")
    units = set()
    for vpc in session["vpcs"]:
        _vpc(vpc)
        if vpc["identity"] in units:
            raise ValueError("Duplicate VPC")
        units.add(vpc["identity"])
    for source in value["sources"].values():
        if source.get("data") is not None:
            _source(source["data"])
    _events(value)
    return value


def fresh(snapshot: dict, name: str) -> dict:
    source = snapshot["sources"].get(name, {})
    age = source.get("age_seconds")
    if (
        source.get("status") != "collected"
        or source.get("stale")
        or not number(age)
        or age > STALE_SECONDS
    ):
        return {}
    return source.get("data") or {}


class Dashboard:
    def __init__(self) -> None:
        self.snapshot = None
        self.rows = []
        self.baselines = {}
        self.history = deque(maxlen=60)
        self.events = deque(maxlen=256)
        self.epoch = None
        self.sequence = 0
        self.last_received = None
        self.last_history = None
        self.disconnected = True

    def update(self, value: dict, now: float) -> None:  # noqa: C901, PLR0912, PLR0915 - one fenced projection per snapshot
        validate(value)
        value = deepcopy(value)
        epoch = value["session"]["session_id"], value["epoch"]
        if epoch != self.epoch:
            self.baselines.clear()
            self.history.clear()
            self.events.clear()
            self.last_history = None
            self.sequence = 0
            self.epoch = epoch
        self.snapshot, self.last_received, self.disconnected = value, now, False
        helper, forwarder = fresh(value, "helper"), fresh(value, "forwarder")
        session = value["session"]
        if helper.get("session_id") != session["session_id"] or not helper.get(
            "details_available"
        ):
            helper = {}
        if forwarder.get("pid") != session.get("forwarder_pid"):
            forwarder = {}
        helper_units = {unit["unit"]: unit for unit in helper.get("units", [])}
        forwarder_units = {unit["unit"]: unit for unit in forwarder.get("units", [])}
        records = {item.get("identity"): item for item in session.get("records", [])}
        rows = []
        for vpc in session["vpcs"]:
            row = dict(vpc, network=records.get(vpc["identity"], {}))
            unit = vpc.get("unit")
            host, transport = helper_units.get(unit, {}), forwarder_units.get(unit, {})
            if not unit or host.get("generation") != vpc.get("generation"):
                host = {}
            if not unit or any(
                transport.get(key) != vpc.get(key)
                for key in (
                    "generation",
                    "attempt",
                    "dns_revision",
                )
            ):
                transport = {}
            row.update(host=host, transport=transport, upload=None, download=None)
            counters = host.get("counters", {})
            upload, download = (
                counters.get("host_delivered_bytes"),
                counters.get("return_delivered_bytes"),
            )
            sample = helper.get("sample_monotonic_ns")
            key = epoch, helper.get("epoch"), unit, vpc.get("generation")
            previous = self.baselines.get(unit)
            if number(sample) and number(upload) and number(download):
                current = key, sample, upload, download
                if previous and previous[0] == key:
                    elapsed = (sample - previous[1]) / 1_000_000_000
                    if (
                        0 < elapsed <= STALE_SECONDS
                        and upload >= previous[2]
                        and download >= previous[3]
                    ):
                        row.update(
                            upload=(upload - previous[2]) / elapsed,
                            download=(download - previous[3]) / elapsed,
                        )
                    elif elapsed == 0:
                        old = next((item for item in self.rows if item.get("unit") == unit), {})
                        row.update(upload=old.get("upload"), download=old.get("download"))
                self.baselines[unit] = current
            else:
                self.baselines.pop(unit, None)
            row["streams"] = transport.get("tcp", {}).get("active")
            row["queue_drops"] = transport.get("tcp", {}).get("queue_drops")
            row["drops"] = (
                sum(counters[key] for key in DROP_FIELDS)
                if all(key in counters for key in DROP_FIELDS)
                else None
            )
            row["dns_healthy"] = bool(
                vpc.get("state") == "ready"
                and host.get("active")
                and transport.get("dns_upstream_enabled")
                and vpc.get("dns_port")
                and transport.get("dns_port") == vpc["dns_port"]
            )
            rows.append(row)
        self.rows = rows
        self._history(now)
        events = value.get("events", {})
        if events.get("gap") and self.sequence < events.get("oldest_sequence", 0) - 1:
            self.events.append(
                {"code": "Event history gap", "timestamp": "", "sequence": self.sequence}
            )
        for event in events.get("entries", []):
            if event["sequence"] > self.sequence:
                self.events.append(event)
                self.sequence = event["sequence"]

    def missing(self, now: float) -> None:
        self.disconnected = True
        self.baselines.clear()
        self._history(now, gap=True)

    def _history(self, now: float, *, gap: bool = False) -> None:
        second = int(now)
        if self.last_history is not None:
            for tick in range(max(self.last_history + 1, second - 59), second):
                self.history.append((tick, None, None))
        sample = (
            second,
            None if gap else self.total("upload"),
            None if gap else self.total("download"),
        )
        if self.last_history == second and self.history:
            self.history[-1] = sample
        else:
            self.history.append(sample)
        self.last_history = second

    def total(self, field: str) -> float | None:
        if not self.rows or any(row.get(field) is None for row in self.rows):
            return None
        return sum(row[field] for row in self.rows)

    @property
    def state(self) -> str:  # noqa: PLR0911 - explicit user-visible states
        if self.disconnected:
            return "Disconnected" if self.snapshot else "Waiting for a tunnel session"
        if self.snapshot:
            helper = fresh(self.snapshot, "helper")
            if helper.get("uncertain") or helper.get("cleanup_retry_scheduled"):
                return "Cleanup incomplete"
            if (
                not helper
                or not fresh(self.snapshot, "forwarder")
                or any(not row["host"] or not row["transport"] for row in self.rows)
            ):
                return "Stale / statistics unavailable"
        states = {row.get("state") for row in self.rows}
        if states == {"ready"}:
            return "READY"
        if "failed" in states:
            return "Blocked"
        if "retrying" in states:
            return "Reconnecting"
        return "Starting"
