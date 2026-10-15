#!/usr/bin/env python3
# ruff: noqa: C901, FURB105, N802, PLR0912, PLR0913, PLR0915, TRY300
"""Audit resources created in an AWS account during a date range.

The inventory is intentionally based on CloudTrail management events rather than tags or
CloudFormation state.  That makes this useful for finding resources left behind by failed or
partially completed deployments.  CloudTrail is only the creation-history source; each row is
also checked against the relevant service API before it is rendered.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import os
import random
import sys
import tempfile
import threading
import time
import zlib
from collections import Counter, defaultdict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from contextlib import ExitStack, suppress
from dataclasses import asdict, dataclass, replace
from datetime import UTC, date, datetime, timedelta
from datetime import time as dt_time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError, OperationNotPageableError
from platformdirs import user_cache_path

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator
    from concurrent.futures import Future
    from typing import TextIO


LOOKUP_PAGE_SIZE = 50
MAX_WORKERS = 8
CLOUDTRAIL_MIN_INTERVAL = 0.6
CLOUDTRAIL_MAX_ATTEMPTS = 6
CLOUDTRAIL_BACKOFF_CAP = 8.0
CLOUDTRAIL_THROTTLE_CODES = {
    "RequestLimitExceeded",
    "ThrottlingException",
    "TooManyRequestsException",
}
TARGETED_ID_BATCH_SIZE = 100
DIRECT_CHECK_BATCH_SIZE = 16
SERVICE_TASK_LIMIT = 4
HISTORY_CACHE_VERSION = 1
HISTORY_CACHE_TTL = timedelta(minutes=15)
HISTORY_CACHE_OVERLAP = timedelta(hours=1)
MISSING_ERROR_CODES = {
    "AccessDeniedException",  # handled specially by _classify_client_error
    "ExpiredToken",
    "InvalidAssociationID.NotFound",
    "InvalidGroup.NotFound",
    "InvalidInstanceID.NotFound",
    "InvalidNetworkInterfaceID.NotFound",
    "InvalidParameterValue",
    "InvalidRouteTableID.NotFound",
    "InvalidSubnetID.NotFound",
    "InvalidVpcID.NotFound",
    "NoSuchBucket",
    "NoSuchEntity",
    "NoSuchKey",
    "NotFound",
    "ResourceNotFoundException",
    "ResourceNotFoundFault",
    "ValidationException",
}
SKIP_EVENT_NAMES = {
    "AddPermission20150331v2",
    "ChangeTagsForResource",
    "ConsoleLogin",
    "CreateGrant",
    "CreateTags",
    "PutBucketNotification",
    "PutBucketPolicy",
    "PutBucketPublicAccessBlock",
    "PutBucketVersioning",
    "PutIntegrationResponse",
    "PutMethodResponse",
    "PutResourcePolicy",
    "PutRetentionPolicy",
    "PutSecretValue",
    "PutTargets",
    "RegisterManagedInstance",
    "RotateKey",
    "StartIngestionJob",
}
GLOBAL_SERVICES = {
    "cloudfront.amazonaws.com",
    "iam.amazonaws.com",
    "route53.amazonaws.com",
    "s3.amazonaws.com",
}
CSV_FIELDS = (
    "account_id",
    "region",
    "event_region",
    "resource_type",
    "resource_id",
    "name",
    "created_at",
    "event_name",
    "event_source",
    "event_id",
    "status",
    "status_detail",
    "parent_type",
    "parent_id",
)


class AuditError(RuntimeError):
    """A fatal error that makes the inventory incomplete."""


def utc_now() -> datetime:
    """Return the current timezone-aware UTC timestamp."""
    return datetime.now(UTC)


def parse_date(value: str) -> date:
    """Parse a YYYY-MM-DD command-line date."""
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid date {value!r}; expected YYYY-MM-DD") from exc


def parse_positive_int(value: str) -> int:
    """Parse a positive integer command-line option."""
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid positive integer {value!r}") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be at least 1")
    return parsed


def resolve_date_range(
    start: date | None,
    end: date | None,
    *,
    today: date | None = None,
) -> tuple[datetime, datetime]:
    """Return an inclusive UTC date range as a half-open datetime interval."""
    current = today or utc_now().date()
    start_date = start or (current - timedelta(days=7))
    end_date = end or current
    if start_date > end_date:
        raise ValueError("--start-date must not be after --end-date")
    start_dt = datetime.combine(start_date, dt_time.min, tzinfo=UTC)
    end_exclusive = datetime.combine(end_date + timedelta(days=1), dt_time.min, tzinfo=UTC)
    return start_dt, end_exclusive


def parse_regions(value: str | None) -> list[str] | None:
    """Parse, validate, and deduplicate a comma-separated region list."""
    if value is None:
        return None
    regions = []
    seen = set()
    for item in value.split(","):
        region = item.strip()
        if not region:
            continue
        if region not in seen:
            regions.append(region)
            seen.add(region)
    if not regions:
        raise ValueError("--regions must contain at least one region")
    return regions


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="List AWS resources created during a UTC date range and check their status."
    )
    parser.add_argument("--start-date", type=parse_date)
    parser.add_argument("--end-date", type=parse_date)
    parser.add_argument("--profile", default="default")
    parser.add_argument(
        "--regions",
        help="comma-separated AWS regions; omitted means all enabled regions",
    )
    parser.add_argument("--output", choices=("text", "jsonl", "csv"), default="text")
    parser.add_argument(
        "--only-active",
        action="store_true",
        help="suppress deleted rows; unknown rows are retained",
    )
    parser.add_argument(
        "--check-workers",
        type=parse_positive_int,
        default=MAX_WORKERS,
        help=f"parallel live-state checker workers (default: {MAX_WORKERS})",
    )
    parser.add_argument(
        "--cache",
        action="store_true",
        help="cache creation history locally; live status is always checked afresh. "
        "Each run refreshes a one-hour overlap; full history is refreshed after 15 minutes",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        help="creation-history cache directory (implies --cache; default: Stelvio user cache)",
    )
    parser.add_argument(
        "--refresh-cache",
        action="store_true",
        help="ignore cached history and fully refresh selected days (implies --cache)",
    )
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments and attach the resolved UTC interval."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.start_datetime, args.end_datetime = resolve_date_range(args.start_date, args.end_date)
        args.regions = parse_regions(args.regions)
        args.cache = args.cache or args.cache_dir is not None or args.refresh_cache
    except ValueError as exc:
        parser.error(str(exc))
    return args


def isoformat_utc(value: datetime | str | None) -> str | None:
    """Normalize a timestamp to an ISO-8601 UTC string."""
    if value is None:
        return None
    if isinstance(value, str):
        normalized = value.replace("Z", "+00:00")
        try:
            value = datetime.fromisoformat(normalized)
        except ValueError:
            return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_timestamp(value: datetime | str | None) -> datetime | None:
    """Parse an AWS timestamp as an aware UTC datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _string(value: Any) -> str | None:
    return None if value is None else str(value)


def _first(*values: Any) -> Any:
    return next((value for value in values if value not in (None, "")), None)


def _nested(value: Any, *keys: str) -> Any:
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _resource_values(event: dict[str, Any], resource_type: str) -> list[str]:
    values = []
    for resource in event.get("resources", []):
        if not isinstance(resource, dict):
            continue
        if resource.get("resourceType") == resource_type and resource.get("resourceName"):
            values.append(str(resource["resourceName"]))
    return values


def _resource_value(event: dict[str, Any], resource_type: str, *prefixes: str) -> str | None:
    values = _resource_values(event, resource_type)
    for prefix in prefixes:
        for value in values:
            if value.startswith(prefix):
                return value
    return values[0] if values else None


def _name_from_tags(value: Any) -> str | None:
    if isinstance(value, dict):
        return _first(value.get("Name"), value.get("name"))
    if isinstance(value, list):
        for tag in value:
            if isinstance(tag, dict) and _first(tag.get("Key"), tag.get("key")) == "Name":
                return _first(tag.get("Value"), tag.get("value"))
    return None


def _request_name(request: dict[str, Any]) -> str | None:
    return _first(
        request.get("name"),
        request.get("Name"),
        request.get("functionName"),
        request.get("bucketName"),
        request.get("queueName"),
        request.get("tableName"),
        request.get("domainName"),
        request.get("emailIdentity"),
        request.get("parameterName"),
        request.get("poolName"),
        _name_from_tags(request.get("tags")),
        _name_from_tags(_nested(request, "tagSpecificationSet", "items")),
    )


def _iam_arn(account_id: str, kind: str, value: str | None) -> str | None:
    if not value:
        return None
    return value if value.startswith("arn:") else f"arn:aws:iam::{account_id}:{kind}/{value}"


def _last_arn_part(value: str | None) -> str | None:
    if not value:
        return None
    return value.rsplit("/", 1)[-1].rsplit(":", 1)[-1]


@dataclass
class ResourceRecord:
    """A normalized resource row."""

    account_id: str
    region: str
    event_region: str
    resource_type: str
    resource_id: str
    name: str | None
    created_at: str
    event_name: str
    event_source: str
    event_id: str
    status: str = "unknown"
    status_detail: str = "not checked"
    parent_type: str | None = None
    parent_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class ResourceInventory:
    """Deduplicated resource records extracted from CloudTrail events."""

    def __init__(self, account_id: str):
        self.account_id = account_id
        self.rows: dict[tuple[str, str, str], ResourceRecord] = {}
        self.events_seen = 0
        self.events_skipped = 0
        self.unknown_events = 0

    def add(
        self,
        *,
        event: dict[str, Any],
        resource_type: str,
        resource_id: str | None,
        name: str | None = None,
        region: str | None = None,
        parent: tuple[str, str] | None = None,
    ) -> bool:
        """Add or merge a resource and retain the earliest creation event."""
        if not resource_id or resource_id == "***":
            return False
        event_region = (
            _string(event.get("awsRegion") or event.get("region") or "unknown") or "unknown"
        )
        effective_region = region or (
            "global" if event.get("eventSource") in GLOBAL_SERVICES else event_region
        )
        event_time = isoformat_utc(event.get("eventTime")) or "unknown"
        event_id = _string(event.get("eventId")) or "unknown"
        candidate = ResourceRecord(
            account_id=self.account_id,
            region=effective_region,
            event_region=event_region,
            resource_type=resource_type,
            resource_id=str(resource_id),
            name=_string(name),
            created_at=event_time,
            event_name=_string(event.get("eventName")) or "unknown",
            event_source=_string(event.get("eventSource")) or "unknown",
            event_id=event_id,
            parent_type=parent[0] if parent else None,
            parent_id=parent[1] if parent else None,
        )
        return self.merge(candidate)

    def merge(self, candidate: ResourceRecord) -> bool:
        """Merge an extracted row without losing its original event metadata."""
        key = (candidate.region, candidate.resource_type, candidate.resource_id)
        previous = self.rows.get(key)
        if previous is None or _creation_key(candidate) < _creation_key(previous):
            if previous is not None:
                candidate.name = candidate.name or previous.name
                candidate.parent_type = candidate.parent_type or previous.parent_type
                candidate.parent_id = candidate.parent_id or previous.parent_id
            self.rows[key] = candidate
        elif previous:
            previous.name = previous.name or candidate.name
            previous.parent_type = previous.parent_type or candidate.parent_type
            previous.parent_id = previous.parent_id or candidate.parent_id
        return True


def _creation_key(row: ResourceRecord) -> tuple[int, str]:
    return (0 if row.created_at != "unknown" else 1, row.created_at)


def _is_creation_event(event_name: str) -> bool:
    return event_name.startswith(("Create", "Publish")) or event_name in {
        "AllocateAddress",
        "ChangeResourceRecordSets",
        "CreateApiKey",
        "PutParameter",
        "PutIntegration",
        "PutMethod",
        "PutRolePolicy",
        "PutRule",
        "PutUserPolicy",
        "RequestCertificate",
        "RunInstances",
        "Subscribe",
    }


def normalize_cloudtrail_summary(
    summary: dict[str, Any], query_region: str
) -> dict[str, Any] | None:
    """Normalize a CloudTrail lookup summary and its embedded event."""
    raw_event: dict[str, Any] = {}
    embedded = summary.get("CloudTrailEvent")
    if isinstance(embedded, str):
        try:
            parsed = json.loads(embedded)
            if isinstance(parsed, dict):
                raw_event = parsed
        except json.JSONDecodeError:
            raw_event = {}
    elif isinstance(embedded, dict):
        raw_event = embedded
    event = dict(raw_event)
    event.setdefault("eventId", summary.get("EventId"))
    event.setdefault("eventName", summary.get("EventName"))
    event.setdefault("eventSource", summary.get("EventSource"))
    event.setdefault("eventTime", summary.get("EventTime"))
    event.setdefault("awsRegion", summary.get("AwsRegion") or query_region)
    event.setdefault("userIdentity", summary.get("Username"))
    if "resources" not in event:
        event["resources"] = [
            {
                "resourceType": item.get("ResourceType"),
                "resourceName": item.get("ResourceName"),
            }
            for item in summary.get("Resources", [])
            if isinstance(item, dict)
        ]
    event.setdefault("errorCode", summary.get("ErrorCode"))
    if not event.get("eventName"):
        return None
    if not event.get("eventId"):
        digest = hashlib.sha256(
            json.dumps(event, sort_keys=True, default=str).encode()
        ).hexdigest()[:16]
        event["eventId"] = f"synthetic-{digest}"
    return event


class CloudTrailRateLimiter:
    """Pace CloudTrail requests independently for each AWS Region."""

    def __init__(
        self,
        *,
        min_interval: float = CLOUDTRAIL_MIN_INTERVAL,
        clock: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        self.min_interval = min_interval
        self._clock = clock or time.monotonic
        self._sleeper = sleeper or time.sleep
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._next_allowed: dict[str, float] = {}

    def _region_lock(self, region: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(region, threading.Lock())

    def wait(self, region: str) -> None:
        """Wait until the next request for ``region`` is allowed."""
        with self._region_lock(region):
            now = self._clock()
            next_allowed = self._next_allowed.get(region, now)
            if now < next_allowed:
                self._sleeper(next_allowed - now)
                now = self._clock()
            self._next_allowed[region] = max(now, next_allowed) + self.min_interval


class GatewayRateLimiter:
    """Admit API Gateway HTTP attempts against both limits at the same instant."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next_attempt = 0.0
        self._next_resources = 0.0

    def wait(self, *, resources: bool) -> None:
        with self._lock:
            now = time.monotonic()
            allowed = max(self._next_attempt, self._next_resources if resources else 0.0)
            if now < allowed:
                time.sleep(allowed - now)
                now = max(time.monotonic(), allowed)
            self._next_attempt = now + 1 / 8
            if resources:
                self._next_resources = now + 1 / 2


def _cloudtrail_error_code(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Code", ""))


def _lookup_events_with_retry(
    client: Any,
    request: dict[str, Any],
    region: str,
    rate_limiter: CloudTrailRateLimiter,
) -> dict[str, Any]:
    """Run one CloudTrail request with paced, bounded throttling retries."""
    for attempt in range(CLOUDTRAIL_MAX_ATTEMPTS):
        rate_limiter.wait(region)
        try:
            return client.lookup_events(**request)
        except ClientError as exc:
            if (
                _cloudtrail_error_code(exc) not in CLOUDTRAIL_THROTTLE_CODES
                or attempt == CLOUDTRAIL_MAX_ATTEMPTS - 1
            ):
                raise
            backoff = min(CLOUDTRAIL_BACKOFF_CAP, 0.5 * (2**attempt))
            time.sleep(random.SystemRandom().uniform(0.0, backoff))
    raise AssertionError("unreachable")


def lookup_cloudtrail_region(
    session: boto3.Session,
    region: str,
    start: datetime,
    end_exclusive: datetime,
    *,
    rate_limiter: CloudTrailRateLimiter | None = None,
) -> Iterator[dict[str, Any]]:
    """Retrieve every CloudTrail lookup page for one region."""
    client = session.client(
        "cloudtrail",
        region_name=region,
        config=Config(retries={"mode": "standard", "total_max_attempts": 1}),
    )
    rate_limiter = rate_limiter or CloudTrailRateLimiter()
    next_token: str | None = None
    while True:
        request: dict[str, Any] = {
            "StartTime": start,
            "EndTime": end_exclusive,
            "MaxResults": LOOKUP_PAGE_SIZE,
            "LookupAttributes": [{"AttributeKey": "ReadOnly", "AttributeValue": "false"}],
        }
        if next_token:
            request["NextToken"] = next_token
        page = _lookup_events_with_retry(client, request, region, rate_limiter)
        for summary in page.get("Events", []):
            if isinstance(summary, dict):
                name = summary.get("EventName")
                if (
                    isinstance(name, str)
                    and name
                    and (not _is_creation_event(name) or name in SKIP_EVENT_NAMES)
                ):
                    continue
                event = normalize_cloudtrail_summary(summary, region)
                if event is not None and _is_creation_event(event["eventName"]):
                    event_time = parse_timestamp(event.get("eventTime"))
                    if event_time is None or start <= event_time < end_exclusive:
                        yield event
        next_token = page.get("NextToken")
        if not next_token:
            return


@dataclass
class CachedHistory:
    """An open, fully validated history entry, replayed without loading it into memory."""

    stream: TextIO
    covered_until: datetime
    last_full_refresh: datetime
    files: ExitStack

    def events(self) -> Iterator[dict[str, Any]]:
        self.stream.seek(0)
        next(self.stream)  # metadata
        for line in self.stream:
            record = json.loads(line)
            if "complete" in record:
                return
            yield record["event"]


class HistoryWriter:
    """Best-effort atomic cache writes; an interrupted AWS lookup never publishes."""

    def __init__(self, path: Path, metadata: dict[str, Any]):
        self.path = path
        self.temporary: Path | None = None
        self.stream: TextIO | None = None
        self.count = 0
        self.files = ExitStack()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".history-", dir=path.parent)
            os.close(fd)
            self.temporary = Path(temporary)
            self.stream = self.files.enter_context(
                gzip.open(self.temporary, "wt", encoding="utf-8")  # noqa: SIM115 - ExitStack owns it
            )
            self.stream.write(json.dumps(metadata) + "\n")
        except OSError as exc:
            self._abandon(exc)

    def _abandon(self, exc: OSError) -> None:
        _print_diagnostics(f"history cache write failed: {exc}")
        self.close()

    def write(self, event: dict[str, Any]) -> None:
        if self.stream is None:
            return
        try:
            self.stream.write(json.dumps({"event": event}, default=str) + "\n")
            self.count += 1
        except OSError as exc:
            self._abandon(exc)

    def publish(self) -> None:
        if self.stream is None or self.temporary is None:
            return
        try:
            self.stream.write(json.dumps({"complete": True, "events": self.count}) + "\n")
            self.files.close()
            self.stream = None
            self.temporary.replace(self.path)
        except OSError as exc:
            self._abandon(exc)

    def close(self) -> None:
        with suppress(OSError):
            self.files.close()
        self.stream = None
        if self.temporary is not None:
            try:
                self.temporary.unlink(missing_ok=True)
            except OSError as exc:
                _print_diagnostics(f"history cache temporary-file cleanup failed: {exc}")
            self.temporary = None


class HistoryCache:
    """Versioned, account/profile/region/day-isolated creation-history cache."""

    def __init__(self, root: Path, account_id: str, profile: str, *, refresh: bool = False):
        self.root = root
        self.account_id = account_id
        self.profile = profile
        self.refresh = refresh

    def _identity(self, partition: str, region: str, start: datetime) -> dict[str, Any]:
        return {
            "version": HISTORY_CACHE_VERSION,
            "partition": partition,
            "account_id": self.account_id,
            "profile": self.profile,
            "region": region,
            "start": isoformat_utc(start),
        }

    def _path(self, partition: str, region: str, start: datetime) -> Path:
        profile_key = hashlib.sha256(self.profile.encode()).hexdigest()
        region_key = hashlib.sha256(region.encode()).hexdigest()
        return (
            self.root
            / partition
            / self.account_id
            / profile_key
            / region_key
            / f"{start.date()}.jsonl.gz"
        )

    def read(
        self, partition: str, region: str, start: datetime, end: datetime, now: datetime
    ) -> CachedHistory | None:
        if self.refresh:
            return None
        try:
            with ExitStack() as files:
                stream = files.enter_context(
                    gzip.open(self._path(partition, region, start), "rt", encoding="utf-8")
                )
                coverage = self._validate(stream, partition, region, start, end, now)
                if coverage is not None:
                    return CachedHistory(stream, *coverage, files.pop_all())
        except FileNotFoundError:
            return None
        except (OSError, EOFError, ValueError, StopIteration, TypeError, zlib.error) as exc:
            _print_diagnostics(f"ignoring unusable history cache: {exc}")
        return None

    def _validate(
        self,
        stream: TextIO,
        partition: str,
        region: str,
        start: datetime,
        end: datetime,
        now: datetime,
    ) -> tuple[datetime, datetime] | None:
        header = json.loads(next(stream))
        if not isinstance(header, dict) or any(
            header.get(key) != value
            for key, value in self._identity(partition, region, start).items()
        ):
            raise ValueError("incompatible cache identity")
        covered = parse_timestamp(header.get("covered_until"))
        full_refresh = parse_timestamp(header.get("last_full_refresh"))
        if (
            covered is None
            or full_refresh is None
            or not start < covered <= end
            or not timedelta(0) <= now - full_refresh < HISTORY_CACHE_TTL
        ):
            return None
        count = 0
        complete = False
        for line in stream:
            record = json.loads(line)
            if not isinstance(record, dict) or complete:
                raise ValueError("invalid history record")
            if "complete" in record:
                if record != {"complete": True, "events": count}:
                    raise ValueError("incomplete history entry")
                complete = True
                continue
            event = record.get("event")
            if (
                not isinstance(event, dict)
                or not isinstance(event.get("eventId"), str)
                or not event["eventId"]
                or not isinstance(event.get("eventName"), str)
                or not _is_creation_event(event["eventName"])
                or not isinstance(event.get("resources", []), list)
                or any(
                    event.get(key) is not None and not isinstance(event[key], str)
                    for key in ("awsRegion", "eventSource", "eventTime")
                )
            ):
                raise ValueError("invalid cached event")
            event_time = parse_timestamp(event.get("eventTime"))
            if event.get("eventTime") and event_time is None:
                raise ValueError("invalid cached event timestamp")
            if event_time is not None and not start <= event_time < covered:
                raise ValueError("cached event outside covered interval")
            count += 1
        if not complete:
            raise ValueError("missing history completion marker")
        return covered, full_refresh

    def events(
        self,
        session: boto3.Session,
        region: str,
        start: datetime,
        end: datetime,
        now: datetime,
        rate_limiter: CloudTrailRateLimiter,
    ) -> Iterator[dict[str, Any]]:
        partition = session.get_partition_for_region(region)
        day = start
        while day < end:
            day_end = min(day + timedelta(days=1), end)
            cached = self.read(partition, region, day, day_end, now)
            query_start = max(day, cached.covered_until - HISTORY_CACHE_OVERLAP) if cached else day
            metadata = {
                **self._identity(partition, region, day),
                "covered_until": isoformat_utc(day_end),
                "last_full_refresh": isoformat_utc(cached.last_full_refresh if cached else now),
            }
            writer = HistoryWriter(self._path(partition, region, day), metadata)
            seen: set[str] = set()
            try:
                sources = (
                    cached.events() if cached else (),
                    lookup_cloudtrail_region(
                        session, region, query_start, day_end, rate_limiter=rate_limiter
                    ),
                )
                for events in sources:
                    for event in events:
                        event_id = str(event["eventId"])
                        if event_id in seen:
                            continue
                        seen.add(event_id)
                        writer.write(event)
                        yield event
                writer.publish()
            finally:
                writer.close()
                if cached is not None:
                    cached.files.close()
            day += timedelta(days=1)


def collect_resources(
    session: boto3.Session,
    regions: list[str],
    start: datetime,
    end_exclusive: datetime,
    account_id: str,
    *,
    cache: HistoryCache | None = None,
    now: datetime | None = None,
    max_workers: int = MAX_WORKERS,
) -> tuple[list[ResourceRecord], Counter[str], ResourceInventory]:
    """Reduce regional event streams to inventories before merging them."""
    cutoff = now or utc_now()
    end = min(end_exclusive, cutoff)
    rate_limiter = CloudTrailRateLimiter()

    def collect_region(region: str) -> CloudTrailExtractor:
        extractor = CloudTrailExtractor(account_id)
        if start >= end:
            return extractor
        events = (
            cache.events(session, region, start, end, cutoff, rate_limiter)
            if cache
            else lookup_cloudtrail_region(session, region, start, end, rate_limiter=rate_limiter)
        )
        for event in events:
            extractor.consume(event)
        return extractor

    inventory = ResourceInventory(account_id)
    seen: set[str] = set()
    skipped: set[str] = set()
    unmapped: dict[str, set[str]] = defaultdict(set)
    with ThreadPoolExecutor(max_workers=min(max_workers, max(1, len(regions)))) as executor:
        futures = {executor.submit(collect_region, region): region for region in regions}
        for future in as_completed(futures):
            try:
                extractor = future.result()
            except Exception as exc:
                raise AuditError(f"CloudTrail lookup failed in {futures[future]}: {exc}") from exc
            for key, row in extractor.inventory.rows.items():
                if row.event_id not in seen or key in inventory.rows:
                    inventory.merge(row)
            seen.update(extractor.seen_event_ids)
            skipped.update(extractor.skipped_event_ids)
            for kind, event_ids in extractor.unmapped_event_ids.items():
                unmapped[kind].update(event_ids)
    inventory.events_seen = len(seen)
    inventory.events_skipped = len(skipped)
    inventory.unknown_events = len(set().union(*unmapped.values()))
    counts = Counter({kind: len(ids) for kind, ids in unmapped.items()})
    return _sorted_rows(inventory), counts, inventory


def _response_value(response: dict[str, Any], *paths: tuple[str, ...]) -> Any:
    for path in paths:
        value = _nested(response, *path)
        if value not in (None, ""):
            return value
    return None


class CloudTrailExtractor:
    """Convert creation events into normalized resource rows."""

    def __init__(self, account_id: str):
        self.inventory = ResourceInventory(account_id)
        self.unmapped_events: Counter[str] = Counter()
        self.unmapped_event_ids: dict[str, set[str]] = defaultdict(set)
        self.seen_event_ids: set[str] = set()
        self.skipped_event_ids: set[str] = set()
        self._recorded = False

    def consume(self, event: dict[str, Any]) -> None:
        """Extract one event once, including cache overlaps and regional duplicates."""
        event_id = _string(event.get("eventId"))
        if event_id and event_id in self.seen_event_ids:
            return
        if event_id:
            self.seen_event_ids.add(event_id)
        skipped = self.inventory.events_skipped
        self.handle(event)
        if event_id and self.inventory.events_skipped > skipped:
            self.skipped_event_ids.add(event_id)

    def _add(
        self,
        event: dict[str, Any],
        resource_type: str,
        resource_id: Any,
        *,
        name: Any = None,
        region: str | None = None,
        parent: tuple[str, str] | None = None,
    ) -> None:
        if self.inventory.add(
            event=event,
            resource_type=resource_type,
            resource_id=_string(resource_id),
            name=_string(name),
            region=region,
            parent=parent,
        ):
            self._recorded = True

    def handle(self, event: dict[str, Any]) -> None:
        """Handle one normalized CloudTrail event."""
        self.inventory.events_seen += 1
        event_name = _string(event.get("eventName")) or ""
        if event.get("errorCode") or event_name in SKIP_EVENT_NAMES:
            self.inventory.events_skipped += 1
            return
        self._recorded = False
        source = _string(event.get("eventSource")) or "unknown.amazonaws.com"
        request = event.get("requestParameters") or {}
        response = event.get("responseElements") or {}
        if not isinstance(request, dict):
            request = {}
        if not isinstance(response, dict):
            response = {}
        if event_name == "PutParameter" and response.get("version") not in (None, 1, "1"):
            self.inventory.events_skipped += 1
            return
        region = "global" if source in GLOBAL_SERVICES else _string(event.get("awsRegion"))

        # CloudWatch Logs.
        if event_name == "CreateLogGroup":
            group = _first(
                _resource_value(event, "AWS::Logs::LogGroup", "/"),
                request.get("logGroupName"),
            )
            self._add(event, "AWS::Logs::LogGroup", group, name=group, region=region)
        elif event_name == "CreateLogStream":
            group = request.get("logGroupName")
            stream = request.get("logStreamName")
            if group and stream:
                self._add(
                    event,
                    "AWS::Logs::LogStream",
                    f"{group}|{stream}",
                    name=stream,
                    region=region,
                    parent=("AWS::Logs::LogGroup", str(group)),
                )

        # EC2 and VPC resources.
        elif source == "ec2.amazonaws.com" and event_name == "CreateVpc":
            resource_id = _resource_value(event, "AWS::EC2::VPC", "vpc-")
            self._add(
                event,
                "AWS::EC2::VPC",
                resource_id,
                name=_first(_name_from_tags(request), request.get("cidrBlock")),
                region=region,
            )
        elif source == "ec2.amazonaws.com" and event_name == "CreateSubnet":
            self._add(
                event,
                "AWS::EC2::Subnet",
                _resource_value(event, "AWS::EC2::Subnet", "subnet-"),
                name=_name_from_tags(request),
                region=region,
            )
        elif source == "ec2.amazonaws.com" and event_name == "CreateSecurityGroup":
            self._add(
                event,
                "AWS::EC2::SecurityGroup",
                _resource_value(event, "AWS::EC2::SecurityGroup", "sg-"),
                name=_first(request.get("groupName"), _name_from_tags(request)),
                region=region,
            )
        elif source == "ec2.amazonaws.com" and event_name == "CreateRouteTable":
            self._add(
                event,
                "AWS::EC2::RouteTable",
                _resource_value(event, "AWS::EC2::RouteTable", "rtb-"),
                name=_name_from_tags(request),
                region=region,
            )
        elif source == "ec2.amazonaws.com" and event_name == "CreateRoute":
            route_table = _first(
                request.get("routeTableId"),
                _resource_value(event, "AWS::EC2::RouteTable", "rtb-"),
            )
            destination = _first(
                request.get("destinationCidrBlock"),
                request.get("destinationIpv6CidrBlock"),
                request.get("destinationPrefixListId"),
            )
            if route_table and destination:
                self._add(
                    event,
                    "AWS::EC2::Route",
                    f"{route_table}|{destination}",
                    name=destination,
                    region=region,
                    parent=("AWS::EC2::RouteTable", str(route_table)),
                )
        elif source == "ec2.amazonaws.com" and event_name == "CreateInternetGateway":
            self._add(
                event,
                "AWS::EC2::InternetGateway",
                _resource_value(event, "AWS::EC2::InternetGateway", "igw-"),
                name=_name_from_tags(request),
                region=region,
            )
        elif source == "ec2.amazonaws.com" and event_name == "CreateNatGateway":
            self._add(
                event,
                "AWS::EC2::NatGateway",
                _resource_value(event, "AWS::EC2::NatGateway", "nat-"),
                name=_name_from_tags(request),
                region=region,
            )
        elif source == "ec2.amazonaws.com" and event_name == "AllocateAddress":
            self._add(
                event,
                "AWS::EC2::EIP",
                _first(
                    _resource_value(event, "AWS::EC2::EIP", "eipalloc-"),
                    response.get("allocationId"),
                ),
                name=response.get("publicIp"),
                region=region,
            )
        elif source == "ec2.amazonaws.com" and event_name == "CreateNetworkInterface":
            self._add(
                event,
                "AWS::EC2::NetworkInterface",
                _resource_value(event, "AWS::EC2::NetworkInterface", "eni-"),
                name=request.get("description"),
                region=region,
            )
        elif event_name == "RunInstances":
            for resource_type, name in (
                ("AWS::EC2::Instance", None),
                ("AWS::EC2::NetworkInterface", "instance-eni"),
                ("AWS::EC2::Volume", None),
            ):
                for resource_id in _resource_values(event, resource_type):
                    self._add(
                        event,
                        resource_type,
                        resource_id,
                        name=name,
                        region=region,
                    )

        # IAM is account-global.
        elif source == "iam.amazonaws.com" and event_name in {
            "CreateRole",
            "CreatePolicy",
            "CreateUser",
            "CreateInstanceProfile",
        }:
            kind, resource_type, request_key = {
                "CreateRole": ("role", "AWS::IAM::Role", "roleName"),
                "CreatePolicy": ("policy", "AWS::IAM::Policy", "policyName"),
                "CreateUser": ("user", "AWS::IAM::User", "userName"),
                "CreateInstanceProfile": (
                    "instance-profile",
                    "AWS::IAM::InstanceProfile",
                    "instanceProfileName",
                ),
            }[event_name]
            value = request.get(request_key)
            self._add(
                event,
                resource_type,
                _first(
                    _resource_value(event, resource_type, "arn:"),
                    _iam_arn(self.inventory.account_id, kind, value),
                ),
                name=value,
                region="global",
            )
        elif source == "iam.amazonaws.com" and event_name == "CreateAccessKey":
            user = request.get("userName")
            self._add(
                event,
                "AWS::IAM::AccessKey",
                _first(
                    _resource_value(event, "AWS::IAM::AccessKey", "AKIA", "ASIA"),
                    _nested(response, "accessKey", "accessKeyId"),
                ),
                name=user,
                region="global",
                parent=("AWS::IAM::User", _iam_arn(self.inventory.account_id, "user", user) or ""),
            )
        elif source == "iam.amazonaws.com" and event_name in {"PutRolePolicy", "PutUserPolicy"}:
            principal_key = "roleName" if event_name == "PutRolePolicy" else "userName"
            principal_kind = "role" if event_name == "PutRolePolicy" else "user"
            parent_type = "AWS::IAM::Role" if event_name == "PutRolePolicy" else "AWS::IAM::User"
            principal = request.get(principal_key)
            policy = request.get("policyName")
            if principal and policy:
                self._add(
                    event,
                    "AWS::IAM::RolePolicy" if principal_kind == "role" else "AWS::IAM::UserPolicy",
                    f"{principal}/{policy}",
                    name=policy,
                    region="global",
                    parent=(
                        parent_type,
                        _iam_arn(self.inventory.account_id, principal_kind, principal) or "",
                    ),
                )

        # Lambda.
        elif source == "lambda.amazonaws.com" and event_name == "CreateFunction20150331":
            name = _first(
                _resource_value(event, "AWS::Lambda::Function"),
                request.get("functionName"),
            )
            if name and name.startswith("arn:"):
                name = _last_arn_part(name)
            self._add(event, "AWS::Lambda::Function", name, name=name, region=region)
        elif source == "lambda.amazonaws.com" and event_name == "CreateFunctionUrlConfig":
            function = request.get("functionName")
            self._add(
                event,
                "AWS::Lambda::Url",
                function,
                name=function,
                region=region,
                parent=("AWS::Lambda::Function", str(function)) if function else None,
            )
        elif source == "lambda.amazonaws.com" and event_name == "CreateEventSourceMapping20150331":
            function = request.get("functionName")
            uuid = _first(response.get("uUID"), response.get("UUID"), response.get("uuid"))
            self._add(
                event,
                "AWS::Lambda::EventSourceMapping",
                uuid,
                name=function,
                region=region,
                parent=("AWS::Lambda::Function", str(function)) if function else None,
            )
        elif source == "lambda.amazonaws.com" and event_name == "PublishLayerVersion20181031":
            arn = response.get("layerVersionArn")
            self._add(
                event,
                "AWS::Lambda::LayerVersion",
                arn,
                name=_first(request.get("layerName"), arn),
                region=region,
            )

        # S3, SQS, SNS, DynamoDB, and Secrets Manager.
        elif event_name == "CreateBucket":
            bucket = _first(_resource_value(event, "AWS::S3::Bucket"), request.get("bucketName"))
            self._add(event, "AWS::S3::Bucket", bucket, name=bucket, region="global")
        elif source == "sqs.amazonaws.com" and event_name == "CreateQueue":
            queue_name = request.get("queueName")
            self._add(
                event,
                "AWS::SQS::Queue",
                _first(
                    response.get("queueUrl"),
                    _resource_value(event, "AWS::SQS::Queue", "https://"),
                    queue_name,
                ),
                name=queue_name,
                region=region,
            )
        elif source == "sns.amazonaws.com" and event_name == "CreateTopic":
            self._add(
                event,
                "AWS::SNS::Topic",
                _first(
                    response.get("topicArn"), _resource_value(event, "AWS::SNS::Topic", "arn:")
                ),
                name=request.get("name"),
                region=region,
            )
        elif source == "sns.amazonaws.com" and event_name == "Subscribe":
            topic = request.get("topicArn")
            subscription = _first(
                response.get("subscriptionArn"),
                _resource_value(event, "AWS::SNS::Subscription", "arn:"),
            )
            if subscription and not str(subscription).endswith(":pending"):
                self._add(
                    event,
                    "AWS::SNS::Subscription",
                    subscription,
                    name=topic,
                    region=region,
                    parent=("AWS::SNS::Topic", str(topic)) if topic else None,
                )
        elif source == "dynamodb.amazonaws.com" and event_name == "CreateTable":
            table = _first(
                _resource_value(event, "AWS::DynamoDB::Table"), request.get("tableName")
            )
            self._add(event, "AWS::DynamoDB::Table", table, name=table, region=region)
        elif source == "secretsmanager.amazonaws.com" and event_name == "CreateSecret":
            self._add(
                event,
                "AWS::SecretsManager::Secret",
                _first(response.get("arn"), request.get("name")),
                name=request.get("name"),
                region=region,
            )

        # RDS and DocumentDB use closely related API shapes.
        elif source in {"rds.amazonaws.com", "docdb.amazonaws.com"} and event_name in {
            "CreateDBCluster",
            "CreateDBInstance",
            "CreateDBSubnetGroup",
            "CreateDBClusterParameterGroup",
        }:
            resource_type = {
                "CreateDBCluster": "AWS::RDS::DBCluster",
                "CreateDBInstance": "AWS::RDS::DBInstance",
                "CreateDBSubnetGroup": "AWS::RDS::DBSubnetGroup",
                "CreateDBClusterParameterGroup": "AWS::RDS::DBClusterParameterGroup",
            }[event_name]
            request_key = {
                "CreateDBCluster": "dBClusterIdentifier",
                "CreateDBInstance": "dBInstanceIdentifier",
                "CreateDBSubnetGroup": "dBSubnetGroupName",
                "CreateDBClusterParameterGroup": "dBClusterParameterGroupName",
            }[event_name]
            self._add(
                event,
                resource_type,
                _first(_resource_value(event, resource_type), request.get(request_key)),
                name=request.get(request_key),
                region=region,
            )

        # Route 53.
        elif source == "route53.amazonaws.com" and event_name == "CreateHostedZone":
            hosted_zone = _resource_value(event, "AWS::Route53::HostedZone", "/hostedzone/", "Z")
            hosted_zone = hosted_zone.rsplit("/", 1)[-1] if hosted_zone else None
            self._add(
                event,
                "AWS::Route53::HostedZone",
                hosted_zone,
                name=request.get("name"),
                region="global",
            )
        elif source == "route53.amazonaws.com" and event_name == "ChangeResourceRecordSets":
            hosted_zone = str(request.get("hostedZoneId") or "").rsplit("/", 1)[-1]
            changes = _nested(request, "changeBatch", "changes") or []
            for change in changes:
                if not isinstance(change, dict) or str(change.get("action", "")).upper() not in {
                    "CREATE",
                    "UPSERT",
                }:
                    continue
                record = change.get("resourceRecordSet") or {}
                record_name = record.get("name")
                record_type = record.get("type")
                if hosted_zone and record_name and record_type:
                    self._add(
                        event,
                        "AWS::Route53::RecordSet",
                        f"{hosted_zone}|{record_name}|{record_type}",
                        name=f"{record_name} {record_type}",
                        region="global",
                        parent=("AWS::Route53::HostedZone", hosted_zone),
                    )

        # API Gateway v1 and v2.
        elif source == "apigateway.amazonaws.com":
            self._handle_api_gateway(event, request, response, region)

        # AppSync.
        elif source == "appsync.amazonaws.com":
            self._handle_appsync(event, request, response, region)

        # CloudFront, ACM, EventBridge, SSM, Cognito, and SES.
        elif source == "cloudfront.amazonaws.com":
            self._handle_cloudfront(event, request, response)
        elif source == "acm.amazonaws.com" and event_name == "RequestCertificate":
            self._add(
                event,
                "AWS::ACM::Certificate",
                _first(
                    _resource_value(event, "AWS::ACM::Certificate", "arn:"),
                    response.get("certificateArn"),
                ),
                name=request.get("domainName"),
                region=region,
            )
        elif source == "events.amazonaws.com" and event_name == "PutRule":
            name = request.get("name")
            self._add(
                event,
                "AWS::Events::Rule",
                _first(
                    response.get("ruleArn"),
                    f"{request.get('eventBusName', 'default')}/{name}" if name else None,
                ),
                name=name,
                region=region,
            )
        elif source == "ssm.amazonaws.com" and event_name in {"PutParameter", "CreateDocument"}:
            resource_type = (
                "AWS::SSM::Parameter" if event_name == "PutParameter" else "AWS::SSM::Document"
            )
            name = _first(request.get("name"), _nested(response, "documentDescription", "name"))
            self._add(event, resource_type, name, name=name, region=region)
        elif source in {"cognito-idp.amazonaws.com", "cognito-identity.amazonaws.com"}:
            self._handle_cognito(event, request, response, region)
        elif source == "ses.amazonaws.com" and event_name in {
            "CreateEmailIdentity",
            "CreateConfigurationSet",
        }:
            name = _first(request.get("emailIdentity"), request.get("configurationSetName"))
            resource_type = (
                "AWS::SES::EmailIdentity"
                if event_name == "CreateEmailIdentity"
                else "AWS::SES::ConfigurationSet"
            )
            self._add(event, resource_type, name, name=name, region=region)

        # Bedrock and S3 Vectors.
        elif source == "bedrock.amazonaws.com" and event_name in {
            "CreateKnowledgeBase",
            "CreateDataSource",
        }:
            if event_name == "CreateKnowledgeBase":
                value = _nested(response, "knowledgeBase", "knowledgeBaseId")
                self._add(event, "AWS::Bedrock::KnowledgeBase", value, name=value, region=region)
            else:
                parent_id = request.get("knowledgeBaseId")
                value = _nested(response, "dataSource", "dataSourceId")
                self._add(
                    event,
                    "AWS::Bedrock::DataSource",
                    f"{parent_id}|{value}" if parent_id and value else None,
                    name=value,
                    region=region,
                    parent=("AWS::Bedrock::KnowledgeBase", str(parent_id)) if parent_id else None,
                )
        elif source == "s3vectors.amazonaws.com" and event_name in {
            "CreateVectorBucket",
            "CreateIndex",
        }:
            if event_name == "CreateVectorBucket":
                name = _first(
                    _resource_value(event, "AWS::S3Vectors::VectorBucket"),
                    request.get("vectorBucketName"),
                )
                self._add(event, "AWS::S3Vectors::VectorBucket", name, name=name, region=region)
            else:
                bucket = request.get("vectorBucketName")
                name = request.get("indexName")
                self._add(
                    event,
                    "AWS::S3Vectors::Index",
                    f"{bucket}|{name}" if bucket and name else None,
                    name=name,
                    region=region,
                    parent=("AWS::S3Vectors::VectorBucket", str(bucket)) if bucket else None,
                )

        if not self._recorded and _is_creation_event(event_name):
            self._add_unknown_event(event, region)

    def _add_unknown_event(self, event: dict[str, Any], region: str | None) -> None:
        """Keep an unsupported creation event visible instead of silently dropping it."""
        source = _string(event.get("eventSource")) or "unknown.amazonaws.com"
        name = _string(event.get("eventName")) or "unknown"
        resources = [item for item in event.get("resources", []) if isinstance(item, dict)]
        if resources:
            for index, resource in enumerate(resources):
                resource_id = resource.get("resourceName") or f"{event['eventId']}#{index}"
                resource_type = resource.get("resourceType") or f"AWS::Unknown::{source}:{name}"
                self._add(
                    event,
                    resource_type,
                    resource_id,
                    name=resource_id,
                    region=region,
                )
        else:
            self._add(
                event,
                f"AWS::Unknown::{source}:{name}",
                str(event.get("eventId")),
                name=name,
                region=region,
            )
        self.inventory.unknown_events += 1
        self.unmapped_events[f"{source}:{name}"] += 1
        self.unmapped_event_ids[f"{source}:{name}"].add(str(event["eventId"]))

    def _handle_api_gateway(
        self,
        event: dict[str, Any],
        request: dict[str, Any],
        response: dict[str, Any],
        region: str | None,
    ) -> None:
        name = _string(event.get("eventName")) or ""
        if name == "CreateRestApi":
            self._add(
                event,
                "AWS::ApiGateway::RestApi",
                _first(
                    _nested(response, "restapiUpdate", "restApiId"),
                    response.get("restApiId"),
                    response.get("id"),
                ),
                name=_nested(request, "createRestApiInput", "name") or request.get("name"),
                region=region,
            )
        elif name == "CreateApi":
            self._add(
                event,
                "AWS::ApiGatewayV2::Api",
                response.get("apiId"),
                name=_first(request.get("name"), response.get("name")),
                region=region,
            )
        elif name == "CreateStage":
            api = _first(request.get("restApiId"), request.get("apiId"))
            stage = _first(
                request.get("stageName"),
                _nested(request, "createStageInput", "stageName"),
                response.get("stageName"),
            )
            if api and stage:
                resource_type = (
                    "AWS::ApiGateway::Stage"
                    if request.get("restApiId")
                    else "AWS::ApiGatewayV2::Stage"
                )
                parent_type = (
                    "AWS::ApiGateway::RestApi"
                    if request.get("restApiId")
                    else "AWS::ApiGatewayV2::Api"
                )
                self._add(
                    event,
                    resource_type,
                    f"{api}|{stage}",
                    name=stage,
                    region=region,
                    parent=(parent_type, str(api)),
                )
        elif name in {"CreateRoute", "CreateIntegration", "CreateAuthorizer"}:
            api = _first(request.get("apiId"), request.get("restApiId"))
            id_key = {
                "CreateRoute": "routeId",
                "CreateIntegration": "integrationId",
                "CreateAuthorizer": "authorizerId",
            }[name]
            child_id = response.get(id_key) or response.get("id")
            if api and child_id:
                is_v1 = bool(request.get("restApiId"))
                resource_type = (
                    {
                        "CreateAuthorizer": "AWS::ApiGateway::Authorizer",
                    }.get(name)
                    if is_v1
                    else {
                        "CreateRoute": "AWS::ApiGatewayV2::Route",
                        "CreateIntegration": "AWS::ApiGatewayV2::Integration",
                        "CreateAuthorizer": "AWS::ApiGatewayV2::Authorizer",
                    }[name]
                )
                if resource_type is None:
                    resource_type = "AWS::Unknown::ApiGateway"
                child_name = _first(
                    response.get("routeKey"),
                    response.get("name"),
                    request.get("routeKey"),
                    request.get("name"),
                )
                parent_type = "AWS::ApiGateway::RestApi" if is_v1 else "AWS::ApiGatewayV2::Api"
                self._add(
                    event,
                    resource_type,
                    f"{api}|{child_id}",
                    name=child_name,
                    region=region,
                    parent=(parent_type, str(api)),
                )
        elif name in {"CreateResource", "CreateDeployment", "PutMethod"}:
            api = request.get("restApiId")
            child_id = response.get("id") or request.get("resourceId")
            if api and child_id:
                suffix = request.get("httpMethod") if name == "PutMethod" else child_id
                resource_type = {
                    "CreateResource": "AWS::ApiGateway::Resource",
                    "CreateDeployment": "AWS::ApiGateway::Deployment",
                    "PutMethod": "AWS::ApiGateway::Method",
                }[name]
                self._add(
                    event,
                    resource_type,
                    f"{api}|{child_id}|{suffix}" if name == "PutMethod" else f"{api}|{child_id}",
                    name=response.get("path") or suffix,
                    region=region,
                    parent=("AWS::ApiGateway::RestApi", str(api)),
                )
        elif name == "PutIntegration":
            api = request.get("restApiId")
            resource_id = request.get("resourceId")
            method = request.get("httpMethod")
            if api and resource_id and method:
                self._add(
                    event,
                    "AWS::ApiGateway::Integration",
                    f"{api}|{resource_id}|{method}",
                    name=method,
                    region=region,
                    parent=("AWS::ApiGateway::RestApi", str(api)),
                )
        elif name == "CreateDomainName":
            domain = _first(request.get("domainName"), response.get("domainName"))
            self._add(event, "AWS::ApiGatewayV2::DomainName", domain, name=domain, region=region)
        elif name == "CreateApiMapping":
            domain = request.get("domainName")
            mapping = response.get("apiMappingId")
            if domain and mapping:
                self._add(
                    event,
                    "AWS::ApiGatewayV2::ApiMapping",
                    f"{domain}|{mapping}",
                    name=request.get("apiId"),
                    region=region,
                    parent=("AWS::ApiGatewayV2::DomainName", str(domain)),
                )

    def _handle_appsync(
        self,
        event: dict[str, Any],
        request: dict[str, Any],
        response: dict[str, Any],
        region: str | None,
    ) -> None:
        name = _string(event.get("eventName")) or ""
        api = request.get("apiId")
        if name == "CreateGraphqlApi":
            graphql = response.get("graphqlApi") or {}
            self._add(
                event,
                "AWS::AppSync::GraphqlApi",
                graphql.get("apiId"),
                name=_first(graphql.get("name"), request.get("name")),
                region=region,
            )
        elif name == "CreateDataSource":
            data_source = response.get("dataSource") or {}
            value = _first(data_source.get("name"), request.get("name"))
            if api and value:
                self._add(
                    event,
                    "AWS::AppSync::DataSource",
                    f"{api}|{value}",
                    name=value,
                    region=region,
                    parent=("AWS::AppSync::GraphqlApi", str(api)),
                )
        elif name == "CreateResolver":
            resolver = response.get("resolver") or {}
            value = _first(
                resolver.get("resolverArn"),
                f"{api}|{request.get('typeName')}|{request.get('fieldName')}" if api else None,
            )
            self._add(
                event,
                "AWS::AppSync::Resolver",
                value,
                name=f"{request.get('typeName')}.{request.get('fieldName')}",
                region=region,
                parent=("AWS::AppSync::GraphqlApi", str(api)) if api else None,
            )
        elif name == "CreateFunction":
            function = response.get("functionConfiguration") or {}
            value = function.get("functionId")
            self._add(
                event,
                "AWS::AppSync::Function",
                f"{api}|{value}" if api and value else None,
                name=_first(function.get("name"), request.get("name")),
                region=region,
                parent=("AWS::AppSync::GraphqlApi", str(api)) if api else None,
            )
        elif name == "CreateApiKey" and api:
            self._add(
                event,
                "AWS::AppSync::ApiKey",
                f"redacted-key:{api}",
                name=api,
                region=region,
                parent=("AWS::AppSync::GraphqlApi", str(api)),
            )

    def _handle_cloudfront(
        self,
        event: dict[str, Any],
        request: dict[str, Any],
        response: dict[str, Any],
    ) -> None:
        name = _string(event.get("eventName")) or ""
        if name in {"CreateFunction", "PublishFunction"}:
            value = _first(request.get("name"), _nested(response, "functionSummary", "name"))
            self._add(event, "AWS::CloudFront::Function", value, name=value, region="global")
        elif name in {"CreateDistribution", "CreateDistributionWithTags"}:
            value = _nested(response, "distribution", "id")
            self._add(event, "AWS::CloudFront::Distribution", value, name=value, region="global")
        elif name == "CreateOriginAccessControl":
            oac = response.get("originAccessControl") or {}
            value = _first(
                oac.get("id"), _resource_value(event, "AWS::CloudFront::OriginAccessControl")
            )
            self._add(
                event,
                "AWS::CloudFront::OriginAccessControl",
                value,
                name=_first(
                    _nested(oac, "originAccessControlConfig", "name"),
                    _nested(request, "originAccessControlConfig", "name"),
                ),
                region="global",
            )
        elif name == "CreateCachePolicy":
            policy = response.get("cachePolicy") or {}
            value = _first(
                policy.get("id"), _resource_value(event, "AWS::CloudFront::CachePolicy")
            )
            self._add(
                event,
                "AWS::CloudFront::CachePolicy",
                value,
                name=_first(
                    _nested(policy, "cachePolicyConfig", "name"),
                    _nested(request, "cachePolicyConfig", "name"),
                ),
                region="global",
            )

    def _handle_cognito(
        self,
        event: dict[str, Any],
        request: dict[str, Any],
        response: dict[str, Any],
        region: str | None,
    ) -> None:
        source = event.get("eventSource")
        name = _string(event.get("eventName")) or ""
        if source == "cognito-identity.amazonaws.com" and name == "CreateIdentityPool":
            self._add(
                event,
                "AWS::Cognito::IdentityPool",
                response.get("identityPoolId"),
                name=_first(response.get("identityPoolName"), request.get("identityPoolName")),
                region=region,
            )
            return
        pool = request.get("userPoolId")
        if name == "CreateUserPool":
            pool_data = response.get("userPool") or {}
            self._add(
                event,
                "AWS::Cognito::UserPool",
                pool_data.get("id"),
                name=_first(pool_data.get("name"), request.get("poolName")),
                region=region,
            )
        elif name == "CreateUserPoolClient":
            client = response.get("userPoolClient") or {}
            pool = pool or client.get("userPoolId")
            client_id = client.get("clientId")
            self._add(
                event,
                "AWS::Cognito::UserPoolClient",
                f"{pool}|{client_id}" if pool and client_id else None,
                name=_first(client.get("clientName"), request.get("clientName")),
                region=region,
                parent=("AWS::Cognito::UserPool", str(pool)) if pool else None,
            )
        elif name in {"CreateUserPoolDomain", "CreateIdentityProvider"}:
            value = _first(request.get("domain"), request.get("providerName"))
            resource_type = (
                "AWS::Cognito::UserPoolDomain"
                if name == "CreateUserPoolDomain"
                else "AWS::Cognito::IdentityProvider"
            )
            self._add(
                event,
                resource_type,
                f"{pool}|{value}" if pool and value else None,
                name=value,
                region=region,
                parent=("AWS::Cognito::UserPool", str(pool)) if pool else None,
            )


def extract_resources(
    events: Iterable[dict[str, Any]], account_id: str
) -> tuple[list[ResourceRecord], Counter[str], ResourceInventory]:
    """Extract and sort deduplicated resources from CloudTrail events."""
    extractor = CloudTrailExtractor(account_id)
    for event in events:
        extractor.consume(event)
    return _sorted_rows(extractor.inventory), extractor.unmapped_events, extractor.inventory


def _sorted_rows(inventory: ResourceInventory) -> list[ResourceRecord]:
    return sorted(
        inventory.rows.values(),
        key=lambda row: (row.created_at, row.region, row.resource_type, row.resource_id),
    )


@dataclass(frozen=True)
class CheckResult:
    """The result of checking one resource."""

    status: str
    detail: str
    confirmed_absent: bool = False


class CachedLookupError(RuntimeError):
    """A fresh exception carrying an immutable, already-retried lookup failure."""

    def __init__(self, result: CheckResult):
        super().__init__(result.detail)
        self.result = result


@dataclass
class CheckTask:
    """A schedulable check with dependencies on parent row results."""

    region: str
    resource_type: str
    rows: list[ResourceRecord]
    service_key: tuple[str, str]
    dependencies: set[int]


DIRECT_CHECK_TYPES = {
    "AWS::IAM::Role",
    "AWS::IAM::Policy",
    "AWS::IAM::User",
    "AWS::IAM::InstanceProfile",
    "AWS::Lambda::Url",
    "AWS::Lambda::LayerVersion",
}
PARENT_GROUPED_TYPES = {
    "AWS::EC2::Route",
    "AWS::Logs::LogStream",
    "AWS::IAM::AccessKey",
    "AWS::IAM::RolePolicy",
    "AWS::IAM::UserPolicy",
    "AWS::Route53::RecordSet",
    "AWS::Bedrock::DataSource",
    "AWS::S3Vectors::Index",
}
PARENT_SHORTCUT_EXCLUSIONS = {
    "AWS::AppSync::ApiKey",  # redacted identifiers must remain explicitly unknown
    "AWS::Lambda::EventSourceMapping",  # survives function deletion; not a contained child
}
SERVICE_NAMES = {
    "EC2": "ec2",
    "Logs": "logs",
    "IAM": "iam",
    "Lambda": "lambda",
    "S3": "s3",
    "SQS": "sqs",
    "SNS": "sns",
    "DynamoDB": "dynamodb",
    "SecretsManager": "secretsmanager",
    "RDS": "rds",
    "SSM": "ssm",
    "Events": "events",
    "ACM": "acm",
    "CloudFront": "cloudfront",
    "SES": "sesv2",
    "ApiGateway": "apigateway",
    "ApiGatewayV2": "apigatewayv2",
    "AppSync": "appsync",
    "Route53": "route53",
    "Bedrock": "bedrock-agent",
    "S3Vectors": "s3vectors",
}


def _task_service(row: ResourceRecord) -> tuple[str, str]:
    family = (
        row.resource_type.split("::", 2)[1] if "::" in row.resource_type else row.resource_type
    )
    service = SERVICE_NAMES.get(family, family)
    if family == "Cognito":
        service = (
            "cognito-identity" if row.resource_type.endswith("IdentityPool") else "cognito-idp"
        )
    elif family == "RDS" and row.event_source == "docdb.amazonaws.com":
        service = "docdb"
    if service == "apigatewayv2":
        service = "apigateway"
    return service, "us-east-1" if row.region == "global" else row.region


def _appsync_resolver_identity(row: ResourceRecord) -> tuple[str, str]:
    parts = row.resource_id.rsplit("|", 2)
    if len(parts) == 3:
        return parts[-2], parts[-1]
    # CloudTrail normally returns an ARN rather than the fallback composite ID.
    arn_parts = row.resource_id.split("/types/", 1)
    if len(arn_parts) == 2:
        type_name, separator, field_name = arn_parts[1].partition("/resolvers/")
        if separator and type_name and field_name:
            return type_name, field_name
    return "", row.resource_id


def _client_error_result(exc: ClientError) -> CheckResult:
    code = str(exc.response.get("Error", {}).get("Code", "unknown"))
    message = str(exc.response.get("Error", {}).get("Message", ""))
    if code != "AccessDeniedException" and (
        code in MISSING_ERROR_CODES or "notfound" in code.lower() or "not found" in message.lower()
    ):
        confirmed = (
            code
            in MISSING_ERROR_CODES
            - {
                "AccessDeniedException",
                "ExpiredToken",
                "InvalidParameterValue",
                "ValidationException",
            }
            or "notfound" in code.lower()
        )
        return CheckResult("deleted", f"{code}: {message}".strip(": "), confirmed)
    return CheckResult("unknown", f"{code}: {message}".strip(": "))


def _exception_result(exc: Exception) -> CheckResult:
    if isinstance(exc, CachedLookupError):
        return exc.result
    if isinstance(exc, ClientError):
        return _client_error_result(exc)
    return CheckResult("unknown", f"{type(exc).__name__}: {exc}")


def _aliases(*values: Any) -> set[str]:
    result: set[str] = set()
    for value in values:
        if value in (None, ""):
            continue
        text = str(value)
        result.add(text)
        if text.startswith("arn:"):
            result.add(_last_arn_part(text) or text)
        if text.startswith("/hostedzone/"):
            result.add(text.rsplit("/", 1)[-1])
    return result


def _row_aliases(row: ResourceRecord) -> set[str]:
    return _aliases(row.resource_id, row.name)


def _page_values(page: dict[str, Any], result_key: str) -> list[Any]:
    value: Any = page
    for key in result_key.split("."):
        if not isinstance(value, dict):
            return []
        value = value.get(key, [])
    return value if isinstance(value, list) else []


def iter_paginated(
    client: Any,
    operation: str,
    result_key: str,
    **kwargs: Any,
) -> Iterator[Any]:
    """Yield all items from a boto3 paginator, with a token fallback."""
    try:
        paginator = client.get_paginator(operation)
    except (OperationNotPageableError, AttributeError):
        token: str | None = None
        while True:
            request = dict(kwargs)
            if token:
                request["NextToken"] = token
            page = getattr(client, operation)(**request)
            yield from _page_values(page, result_key)
            token = page.get("NextToken")
            if not token:
                return
    else:
        for page in paginator.paginate(**kwargs):
            yield from _page_values(page, result_key)


class LiveChecker:
    """Perform current-state checks, grouping rows to avoid one API call per child."""

    def __init__(self, session: boto3.Session, *, max_workers: int = MAX_WORKERS):
        self.session = session
        self.max_workers = max_workers
        self.warnings: list[str] = []
        self._client_cache: dict[tuple[str, str], Any] = {}
        self._client_cache_lock = threading.Lock()
        self._parent_cache: dict[tuple[str, ...], Any] = {}
        self._parent_cache_locks: dict[tuple[str, ...], threading.Lock] = {}
        self._parent_cache_locks_guard = threading.Lock()
        self._gateway_limiter = GatewayRateLimiter()

    def client(self, service: str, region: str) -> Any:
        effective_region = "us-east-1" if region == "global" else region
        key = (service, effective_region)
        with self._client_cache_lock:
            if key not in self._client_cache:
                self._client_cache[key] = self.session.client(
                    service,
                    region_name=effective_region,
                    config=Config(retries={"mode": "standard", "max_attempts": 8}),
                )
                if service in {"apigateway", "apigatewayv2"}:
                    self._client_cache[key].meta.events.register(
                        "before-send.*.*", self._pace_gateway_request
                    )
            return self._client_cache[key]

    def _pace_gateway_request(self, *, event_name: str, **_kwargs: Any) -> None:
        self._gateway_limiter.wait(resources=event_name.endswith(".GetResources"))

    def _cached_parent(self, key: tuple[str, ...], loader: Callable[[], Any]) -> Any:
        with self._parent_cache_locks_guard:
            lock = self._parent_cache_locks.setdefault(key, threading.Lock())
        with lock:
            if key not in self._parent_cache:
                try:
                    self._parent_cache[key] = loader()
                except Exception as exc:
                    self._parent_cache[key] = _exception_result(exc)
            value = self._parent_cache[key]
            if isinstance(value, CheckResult):
                raise CachedLookupError(value)
            return value

    def _method(self, resource_type: str) -> Callable[..., dict[int, CheckResult]] | None:
        return getattr(self, f"_check_{resource_type.replace('::', '_').replace('-', '_')}", None)

    def _parent_reference(self, row: ResourceRecord) -> tuple[str, str, str] | None:
        if (
            not row.parent_type
            or not row.parent_id
            or row.resource_type in PARENT_SHORTCUT_EXCLUSIONS
            or self._method(row.resource_type) is None
        ):
            return None
        parent = row.parent_id
        if parent.startswith("arn:"):
            parts = parent.split(":", 5)
            if (
                len(parts) != 6
                or parts[4] != row.account_id
                or (parts[3] and parts[3] != row.region)
            ):
                return None
            # A function version/alias belongs to its base function, not a function
            # whose name happens to equal the qualifier.
            if row.parent_type == "AWS::Lambda::Function" and parts[5].startswith("function:"):
                parts[5] = ":".join(parts[5].split(":")[:2])
                parent = ":".join(parts)
        return row.region, row.parent_type, parent

    def _prepare_tasks(
        self, rows: list[ResourceRecord]
    ) -> tuple[list[CheckTask], dict[int, ResourceRecord]]:
        all_rows = list(rows)
        exact = {(row.region, row.resource_type, row.resource_id): row for row in rows}
        aliases: dict[tuple[str, str, str], dict[int, ResourceRecord]] = defaultdict(dict)

        def index(row: ResourceRecord) -> None:
            for alias in _row_aliases(row):
                aliases[(row.region, row.resource_type, alias)][id(row)] = row

        for row in rows:
            index(row)
        parents = {}
        for row in rows:
            reference = self._parent_reference(row)
            if reference is None:
                continue
            region, kind, parent_id = reference
            parent = exact.get(reference)
            if parent is None:
                matches: dict[int, ResourceRecord] = {}
                for alias in _aliases(parent_id):
                    matches.update(aliases.get((region, kind, alias), {}))
                # Ambiguous or different full ARNs must never prove a parent's absence.
                if len(matches) > 1 or (
                    parent_id.startswith("arn:")
                    and any(
                        candidate.resource_id.startswith("arn:")
                        and candidate.resource_id != parent_id
                        for candidate in matches.values()
                    )
                ):
                    continue
                parent = next(iter(matches.values()), None)
            if parent is None:
                parent = replace(
                    row,
                    resource_type=kind,
                    resource_id=parent_id,
                    name=None,
                    parent_type=None,
                    parent_id=None,
                    status="unknown",
                    status_detail="not checked",
                )
                exact[reference] = parent
                all_rows.append(parent)
                index(parent)
            parents[id(row)] = parent

        groups: dict[tuple[str, str, str], list[ResourceRecord]] = defaultdict(list)
        for row in all_rows:
            kind = row.resource_type
            parent_grouped = kind in PARENT_GROUPED_TYPES or (
                kind.startswith(
                    (
                        "AWS::ApiGateway::",
                        "AWS::ApiGatewayV2::",
                        "AWS::AppSync::",
                        "AWS::Cognito::",
                    )
                )
                and row.parent_id is not None
            )
            group_key = row.parent_id or row.resource_id.split("|", 1)[0] if parent_grouped else ""
            if kind == "AWS::AppSync::Resolver":
                group_key += "|" + _appsync_resolver_identity(row)[0]
            groups[(row.region, kind, group_key)].append(row)
        tasks = []
        for (region, kind, _parent), group in groups.items():
            size = DIRECT_CHECK_BATCH_SIZE if kind in DIRECT_CHECK_TYPES else len(group)
            for start in range(0, len(group), size):
                chunk = group[start : start + size]
                dependencies = {id(parents[id(row)]) for row in chunk if id(row) in parents}
                dependencies.difference_update(id(row) for row in chunk)
                tasks.append(CheckTask(region, kind, chunk, _task_service(chunk[0]), dependencies))
        return tasks, parents

    def _run_task(
        self,
        task: CheckTask,
        parents: dict[int, ResourceRecord],
        completed: dict[int, CheckResult],
    ) -> dict[int, CheckResult]:
        results = {}
        pending = []
        for row in task.rows:
            parent = parents.get(id(row))
            result = completed.get(id(parent)) if parent is not None else None
            if result is not None and result.confirmed_absent:
                results[id(row)] = CheckResult("deleted", "parent resource is deleted", True)
            else:
                pending.append(row)
        if pending:
            results.update(self._check_group(task.region, task.resource_type, pending))
        return results

    def check(self, rows: list[ResourceRecord]) -> list[str]:
        tasks, parents = self._prepare_tasks(rows)
        completed: dict[int, CheckResult] = {}
        active: Counter[tuple[str, str]] = Counter()
        worker_count = min(self.max_workers, max(1, len(tasks)))
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures: dict[Future[dict[int, CheckResult]], CheckTask] = {}
            while tasks or futures:
                for task in list(tasks):
                    if len(futures) >= worker_count:
                        break
                    if (
                        not task.dependencies.issubset(completed)
                        or active[task.service_key] >= SERVICE_TASK_LIMIT
                    ):
                        continue
                    tasks.remove(task)
                    # Workers only read the already-resolved parent results snapshot.
                    futures[executor.submit(self._run_task, task, parents, dict(completed))] = task
                    active[task.service_key] += 1
                if not futures:
                    # Malformed/cyclic relationships must not deadlock an audit.
                    tasks[0].dependencies.clear()
                    continue
                done, _pending = wait(futures, return_when=FIRST_COMPLETED)
                for future in done:
                    task = futures.pop(future)
                    active[task.service_key] -= 1
                    try:
                        results = future.result()
                    except Exception as exc:
                        results = {id(row): _exception_result(exc) for row in task.rows}
                    for row in task.rows:
                        result = results.get(
                            id(row), CheckResult("unknown", "checker returned no result")
                        )
                        completed[id(row)] = result
                        row.status = result.status
                        row.status_detail = result.detail
        return self.warnings

    def _check_group(
        self,
        region: str,
        resource_type: str,
        rows: list[ResourceRecord],
    ) -> dict[int, CheckResult]:
        method = self._method(resource_type)
        if method is None:
            return {id(row): CheckResult("unknown", "unsupported resource type") for row in rows}
        try:
            return method(region, rows)
        except Exception as exc:
            result = _exception_result(exc)
            self.warnings.append(f"{region} {resource_type}: {result.detail}")
            return {id(row): result for row in rows}

    def _scan(
        self,
        region: str,
        rows: list[ResourceRecord],
        service: str,
        operation: str,
        result_key: str,
        identity: Callable[[Any], set[str]],
        **kwargs: Any,
    ) -> dict[int, CheckResult]:
        try:
            found: set[str] = set()
            details: dict[str, str] = {}
            for item in iter_paginated(
                self.client(service, region), operation, result_key, **kwargs
            ):
                keys = identity(item)
                found.update(keys)
                for key in keys:
                    details[key] = "found"
            results = {}
            for row in rows:
                matched = _row_aliases(row) & found
                results[id(row)] = CheckResult(
                    "active" if matched else "deleted",
                    details.get(next(iter(matched), ""), "resource not returned by list API"),
                    not matched,
                )
            return results
        except Exception as exc:
            result = _exception_result(exc)
            self.warnings.append(f"{region} {rows[0].resource_type}: {result.detail}")
            return {id(row): result for row in rows}

    def _scan_ids(
        self,
        region: str,
        rows: list[ResourceRecord],
        service: str,
        operation: str,
        result_key: str,
        request_key: str,
        identity: Callable[[Any], set[str]],
    ) -> dict[int, CheckResult]:
        """Check resources with batched identifier requests where supported."""
        client = self.client(service, region)
        results: dict[int, CheckResult] = {}
        for start in range(0, len(rows), TARGETED_ID_BATCH_SIZE):
            batch = rows[start : start + TARGETED_ID_BATCH_SIZE]
            request_ids = [row.resource_id for row in batch]
            try:
                found: set[str] = set()
                for item in iter_paginated(
                    client,
                    operation,
                    result_key,
                    **{request_key: request_ids},
                ):
                    found.update(identity(item))
                for row in batch:
                    matched = _row_aliases(row) & found
                    results[id(row)] = CheckResult(
                        "active" if matched else "deleted",
                        "resource returned" if matched else "resource not returned",
                        not matched,
                    )
            except Exception as exc:
                batch_result = _exception_result(exc)
                if batch_result.status != "deleted":
                    results.update({id(row): batch_result for row in batch})
                    continue
                for row in batch:
                    try:
                        found = any(
                            _row_aliases(row) & identity(item)
                            for item in iter_paginated(
                                client,
                                operation,
                                result_key,
                                **{request_key: [row.resource_id]},
                            )
                        )
                        results[id(row)] = CheckResult(
                            "active" if found else "deleted",
                            "resource returned" if found else "resource not returned",
                            not found,
                        )
                    except Exception as row_exc:
                        results[id(row)] = _exception_result(row_exc)
        return results

    def _direct(
        self,
        rows: list[ResourceRecord],
        operation: Callable[[ResourceRecord], str],
    ) -> dict[int, CheckResult]:
        results: dict[int, CheckResult] = {}
        for row in rows:
            try:
                results[id(row)] = CheckResult("active", operation(row))
            except Exception as exc:
                results[id(row)] = _exception_result(exc)
        return results

    # EC2.
    def _check_AWS_EC2_VPC(
        self, region: str, rows: list[ResourceRecord]
    ) -> dict[int, CheckResult]:
        return self._scan_ids(
            region,
            rows,
            "ec2",
            "describe_vpcs",
            "Vpcs",
            "VpcIds",
            lambda item: _aliases(item.get("VpcId")),
        )

    def _check_AWS_EC2_Subnet(
        self, region: str, rows: list[ResourceRecord]
    ) -> dict[int, CheckResult]:
        return self._scan_ids(
            region,
            rows,
            "ec2",
            "describe_subnets",
            "Subnets",
            "SubnetIds",
            lambda item: _aliases(item.get("SubnetId")),
        )

    def _check_AWS_EC2_SecurityGroup(
        self, region: str, rows: list[ResourceRecord]
    ) -> dict[int, CheckResult]:
        return self._scan_ids(
            region,
            rows,
            "ec2",
            "describe_security_groups",
            "SecurityGroups",
            "GroupIds",
            lambda item: _aliases(item.get("GroupId"), item.get("GroupName")),
        )

    def _check_AWS_EC2_RouteTable(
        self, region: str, rows: list[ResourceRecord]
    ) -> dict[int, CheckResult]:
        return self._scan_ids(
            region,
            rows,
            "ec2",
            "describe_route_tables",
            "RouteTables",
            "RouteTableIds",
            lambda item: _aliases(item.get("RouteTableId")),
        )

    def _check_AWS_EC2_InternetGateway(
        self, region: str, rows: list[ResourceRecord]
    ) -> dict[int, CheckResult]:
        return self._scan_ids(
            region,
            rows,
            "ec2",
            "describe_internet_gateways",
            "InternetGateways",
            "InternetGatewayIds",
            lambda item: _aliases(item.get("InternetGatewayId")),
        )

    def _check_AWS_EC2_NatGateway(
        self, region: str, rows: list[ResourceRecord]
    ) -> dict[int, CheckResult]:
        return self._scan_ids(
            region,
            rows,
            "ec2",
            "describe_nat_gateways",
            "NatGateways",
            "NatGatewayIds",
            lambda item: _aliases(item.get("NatGatewayId")),
        )

    def _check_AWS_EC2_NetworkInterface(
        self, region: str, rows: list[ResourceRecord]
    ) -> dict[int, CheckResult]:
        return self._scan_ids(
            region,
            rows,
            "ec2",
            "describe_network_interfaces",
            "NetworkInterfaces",
            "NetworkInterfaceIds",
            lambda item: _aliases(item.get("NetworkInterfaceId")),
        )

    def _check_AWS_EC2_Instance(
        self, region: str, rows: list[ResourceRecord]
    ) -> dict[int, CheckResult]:
        return self._scan_ids(
            region,
            rows,
            "ec2",
            "describe_instances",
            "Reservations",
            "InstanceIds",
            lambda item: set().union(
                *(_aliases(instance.get("InstanceId")) for instance in item.get("Instances", []))
            ),
        )

    def _check_AWS_EC2_Volume(
        self, region: str, rows: list[ResourceRecord]
    ) -> dict[int, CheckResult]:
        return self._scan_ids(
            region,
            rows,
            "ec2",
            "describe_volumes",
            "Volumes",
            "VolumeIds",
            lambda item: _aliases(item.get("VolumeId")),
        )

    def _check_AWS_EC2_EIP(
        self, region: str, rows: list[ResourceRecord]
    ) -> dict[int, CheckResult]:
        return self._scan(
            region,
            rows,
            "ec2",
            "describe_addresses",
            "Addresses",
            lambda item: _aliases(item.get("AllocationId"), item.get("PublicIp")),
        )

    def _check_AWS_EC2_Route(
        self, region: str, rows: list[ResourceRecord]
    ) -> dict[int, CheckResult]:
        try:
            client = self.client("ec2", region)
            route_tables: dict[str, list[dict[str, Any]]] = {}
            for row in rows:
                parent, _, _ = row.resource_id.partition("|")
                route_tables.setdefault(parent, [])
            for page in client.get_paginator("describe_route_tables").paginate(
                RouteTableIds=list(route_tables)
            ):
                for table in page.get("RouteTables", []):
                    route_tables[str(table.get("RouteTableId"))] = table.get("Routes", [])
            results = {}
            for row in rows:
                parent, _, destination = row.resource_id.partition("|")
                found = any(
                    _first(
                        route.get("DestinationCidrBlock"),
                        route.get("DestinationIpv6CidrBlock"),
                        route.get("DestinationPrefixListId"),
                    )
                    == destination
                    for route in route_tables.get(parent, [])
                )
                results[id(row)] = CheckResult(
                    "active" if found else "deleted",
                    "route returned" if found else "route not returned",
                )
            return results
        except Exception as exc:
            result = _exception_result(exc)
            return {id(row): result for row in rows}

    # CloudWatch Logs.
    def _check_AWS_Logs_LogGroup(
        self, region: str, rows: list[ResourceRecord]
    ) -> dict[int, CheckResult]:
        return self._scan(
            region,
            rows,
            "logs",
            "describe_log_groups",
            "logGroups",
            lambda item: _aliases(item.get("logGroupName"), item.get("arn")),
        )

    def _check_AWS_Logs_LogStream(
        self, region: str, rows: list[ResourceRecord]
    ) -> dict[int, CheckResult]:
        results: dict[int, CheckResult] = {}
        grouped: dict[str, list[ResourceRecord]] = defaultdict(list)
        for row in rows:
            group, _, _ = row.resource_id.partition("|")
            grouped[group].append(row)
        for group, group_rows in grouped.items():
            try:
                key = ("logs-log-streams", region, group)
                streams = self._cached_parent(
                    key,
                    lambda group=group: list(
                        iter_paginated(
                            self.client("logs", region),
                            "describe_log_streams",
                            "logStreams",
                            logGroupName=group,
                        )
                    ),
                )
                found = {str(item.get("logStreamName")) for item in streams}
                for row in group_rows:
                    _, _, stream = row.resource_id.partition("|")
                    results[id(row)] = CheckResult(
                        "active" if stream in found else "deleted",
                        "stream returned" if stream in found else "stream not returned",
                    )
            except Exception as exc:
                result = _exception_result(exc)
                for row in group_rows:
                    results[id(row)] = result
        return results

    # IAM.
    def _check_AWS_IAM_Role(
        self, region: str, rows: list[ResourceRecord]
    ) -> dict[int, CheckResult]:
        return self._direct(
            rows,
            lambda row: self.client("iam", "global").get_role(
                RoleName=_last_arn_part(row.resource_id)
            )["Role"]["Arn"],
        )

    def _check_AWS_IAM_Policy(
        self, region: str, rows: list[ResourceRecord]
    ) -> dict[int, CheckResult]:
        return self._direct(
            rows,
            lambda row: self.client("iam", "global").get_policy(PolicyArn=row.resource_id)[
                "Policy"
            ]["Arn"],
        )

    def _check_AWS_IAM_User(
        self, region: str, rows: list[ResourceRecord]
    ) -> dict[int, CheckResult]:
        return self._direct(
            rows,
            lambda row: self.client("iam", "global").get_user(
                UserName=_last_arn_part(row.resource_id)
            )["User"]["Arn"],
        )

    def _check_AWS_IAM_InstanceProfile(
        self, region: str, rows: list[ResourceRecord]
    ) -> dict[int, CheckResult]:
        return self._direct(
            rows,
            lambda row: self.client("iam", "global").get_instance_profile(
                InstanceProfileName=_last_arn_part(row.resource_id)
            )["InstanceProfile"]["Arn"],
        )

    def _check_AWS_IAM_AccessKey(
        self, region: str, rows: list[ResourceRecord]
    ) -> dict[int, CheckResult]:
        keys_by_user: dict[str, set[str]] = {}

        def keys_for_user(user: str) -> set[str]:
            key = ("iam-access-keys", user)
            return self._cached_parent(
                key,
                lambda user=user: {
                    str(item.get("AccessKeyId"))
                    for item in iter_paginated(
                        self.client("iam", "global"),
                        "list_access_keys",
                        "AccessKeyMetadata",
                        UserName=user,
                    )
                },
            )

        def check(row: ResourceRecord) -> str:
            if not row.parent_id:
                raise ValueError("access key has no parent user")
            user = _last_arn_part(row.parent_id)
            if user not in keys_by_user:
                keys_by_user[user] = keys_for_user(user)
            if row.resource_id in keys_by_user[user]:
                return "access key returned"
            raise ClientError(
                {"Error": {"Code": "NoSuchEntity", "Message": "access key not returned"}},
                "ListAccessKeys",
            )

        return self._direct(rows, check)

    def _check_AWS_IAM_RolePolicy(
        self, region: str, rows: list[ResourceRecord]
    ) -> dict[int, CheckResult]:
        return self._check_iam_inline_policy(rows, "list_role_policies", "PolicyNames", "roleName")

    def _check_AWS_IAM_UserPolicy(
        self, region: str, rows: list[ResourceRecord]
    ) -> dict[int, CheckResult]:
        return self._check_iam_inline_policy(rows, "list_user_policies", "PolicyNames", "userName")

    def _check_iam_inline_policy(
        self,
        rows: list[ResourceRecord],
        operation: str,
        result_key: str,
        principal_key: str,
    ) -> dict[int, CheckResult]:
        results: dict[int, CheckResult] = {}
        grouped: dict[str, list[ResourceRecord]] = defaultdict(list)
        for row in rows:
            principal, _, _ = row.resource_id.partition("/")
            grouped[principal].append(row)
        for principal, principal_rows in grouped.items():
            try:
                key = ("iam-inline-policies", operation, principal)
                names = self._cached_parent(
                    key,
                    lambda principal=principal: set(
                        iter_paginated(
                            self.client("iam", "global"),
                            operation,
                            result_key,
                            **{principal_key: principal},
                        )
                    ),
                )
                for row in principal_rows:
                    policy_name = row.resource_id.split("/", 1)[-1]
                    results[id(row)] = CheckResult(
                        "active" if policy_name in names else "deleted",
                        "policy returned" if policy_name in names else "policy not returned",
                    )
            except Exception as exc:
                result = _exception_result(exc)
                for row in principal_rows:
                    results[id(row)] = result
        return results

    # Lambda.
    def _check_AWS_Lambda_Function(
        self, region: str, rows: list[ResourceRecord]
    ) -> dict[int, CheckResult]:
        return self._scan(
            region,
            rows,
            "lambda",
            "list_functions",
            "Functions",
            lambda item: _aliases(item.get("FunctionName"), item.get("FunctionArn")),
        )

    def _check_AWS_Lambda_Url(
        self, region: str, rows: list[ResourceRecord]
    ) -> dict[int, CheckResult]:
        return self._direct(
            rows,
            lambda row: self.client("lambda", region)
            .get_function_url_config(FunctionName=row.parent_id or row.resource_id)
            .get("FunctionUrl", "function URL returned"),
        )

    def _check_AWS_Lambda_EventSourceMapping(
        self, region: str, rows: list[ResourceRecord]
    ) -> dict[int, CheckResult]:
        return self._scan(
            region,
            rows,
            "lambda",
            "list_event_source_mappings",
            "EventSourceMappings",
            lambda item: _aliases(item.get("UUID")),
        )

    def _check_AWS_Lambda_LayerVersion(
        self, region: str, rows: list[ResourceRecord]
    ) -> dict[int, CheckResult]:
        def check(row: ResourceRecord) -> str:
            parts = row.resource_id.rsplit(":", 1)
            layer = parts[-2] if len(parts) == 2 else _last_arn_part(row.resource_id)
            version = parts[-1] if len(parts) == 2 else None
            response = self.client("lambda", region).list_layer_versions(LayerName=layer)
            if any(str(item.get("Version")) == version for item in response.get("Versions", [])):
                return "layer version returned"
            raise ClientError(
                {
                    "Error": {
                        "Code": "ResourceNotFoundException",
                        "Message": "layer version not returned",
                    }
                },
                "ListLayerVersions",
            )

        return self._direct(rows, check)

    # S3, SQS, SNS, DynamoDB, Secrets Manager.
    def _check_AWS_S3_Bucket(
        self, region: str, rows: list[ResourceRecord]
    ) -> dict[int, CheckResult]:
        return self._scan(
            "global",
            rows,
            "s3",
            "list_buckets",
            "Buckets",
            lambda item: _aliases(item.get("Name")),
        )

    def _check_AWS_SQS_Queue(
        self, region: str, rows: list[ResourceRecord]
    ) -> dict[int, CheckResult]:
        return self._scan(
            region, rows, "sqs", "list_queues", "QueueUrls", lambda item: _aliases(item)
        )

    def _check_AWS_SNS_Topic(
        self, region: str, rows: list[ResourceRecord]
    ) -> dict[int, CheckResult]:
        return self._scan(
            region,
            rows,
            "sns",
            "list_topics",
            "Topics",
            lambda item: _aliases(item.get("TopicArn")),
        )

    def _check_AWS_SNS_Subscription(
        self, region: str, rows: list[ResourceRecord]
    ) -> dict[int, CheckResult]:
        return self._scan(
            region,
            rows,
            "sns",
            "list_subscriptions",
            "Subscriptions",
            lambda item: _aliases(item.get("SubscriptionArn")),
        )

    def _check_AWS_DynamoDB_Table(
        self, region: str, rows: list[ResourceRecord]
    ) -> dict[int, CheckResult]:
        return self._scan(
            region, rows, "dynamodb", "list_tables", "TableNames", lambda item: _aliases(item)
        )

    def _check_AWS_SecretsManager_Secret(
        self, region: str, rows: list[ResourceRecord]
    ) -> dict[int, CheckResult]:
        return self._scan(
            region,
            rows,
            "secretsmanager",
            "list_secrets",
            "SecretList",
            lambda item: _aliases(item.get("ARN"), item.get("Name")),
            IncludePlannedDeletion=True,
        )

    # RDS and DocumentDB.
    def _check_AWS_RDS_DBCluster(
        self, region: str, rows: list[ResourceRecord]
    ) -> dict[int, CheckResult]:
        return self._scan_rds(
            region, rows, "describe_db_clusters", "DBClusters", "DBClusterIdentifier"
        )

    def _check_AWS_RDS_DBInstance(
        self, region: str, rows: list[ResourceRecord]
    ) -> dict[int, CheckResult]:
        return self._scan_rds(
            region, rows, "describe_db_instances", "DBInstances", "DBInstanceIdentifier"
        )

    def _check_AWS_RDS_DBSubnetGroup(
        self, region: str, rows: list[ResourceRecord]
    ) -> dict[int, CheckResult]:
        return self._scan_rds(
            region, rows, "describe_db_subnet_groups", "DBSubnetGroups", "DBSubnetGroupName"
        )

    def _check_AWS_RDS_DBClusterParameterGroup(
        self, region: str, rows: list[ResourceRecord]
    ) -> dict[int, CheckResult]:
        return self._scan_rds(
            region,
            rows,
            "describe_db_cluster_parameter_groups",
            "DBClusterParameterGroups",
            "DBClusterParameterGroupName",
        )

    def _scan_rds(
        self,
        region: str,
        rows: list[ResourceRecord],
        operation: str,
        result_key: str,
        identity_key: str,
    ) -> dict[int, CheckResult]:
        service = "rds"
        if any(row.event_source == "docdb.amazonaws.com" for row in rows):
            service = "docdb"
        return self._scan(
            region,
            rows,
            service,
            operation,
            result_key,
            lambda item: _aliases(item.get(identity_key)),
        )

    # SSM and EventBridge.
    def _check_AWS_SSM_Parameter(
        self, region: str, rows: list[ResourceRecord]
    ) -> dict[int, CheckResult]:
        return self._scan(
            region,
            rows,
            "ssm",
            "describe_parameters",
            "Parameters",
            lambda item: _aliases(item.get("Name")),
        )

    def _check_AWS_SSM_Document(
        self, region: str, rows: list[ResourceRecord]
    ) -> dict[int, CheckResult]:
        return self._scan(
            region,
            rows,
            "ssm",
            "list_documents",
            "DocumentIdentifiers",
            lambda item: _aliases(item.get("Name")),
            Filters=[{"Key": "Owner", "Values": ["Self"]}],
        )

    def _check_AWS_Events_Rule(
        self, region: str, rows: list[ResourceRecord]
    ) -> dict[int, CheckResult]:
        return self._scan(
            region,
            rows,
            "events",
            "list_rules",
            "Rules",
            lambda item: _aliases(item.get("Name"), item.get("Arn")),
        )

    # ACM and CloudFront are account-global control planes.
    def _check_AWS_ACM_Certificate(
        self, region: str, rows: list[ResourceRecord]
    ) -> dict[int, CheckResult]:
        return self._scan(
            region,
            rows,
            "acm",
            "list_certificates",
            "CertificateSummaryList",
            lambda item: _aliases(item.get("CertificateArn")),
        )

    def _check_AWS_CloudFront_Function(
        self, region: str, rows: list[ResourceRecord]
    ) -> dict[int, CheckResult]:
        return self._scan(
            "global",
            rows,
            "cloudfront",
            "list_functions",
            "FunctionList.Items",
            lambda item: _aliases(item.get("Name")),
        )

    def _check_AWS_CloudFront_Distribution(
        self, region: str, rows: list[ResourceRecord]
    ) -> dict[int, CheckResult]:
        return self._scan(
            "global",
            rows,
            "cloudfront",
            "list_distributions",
            "DistributionList.Items",
            lambda item: _aliases(item.get("Id"), item.get("ARN")),
        )

    def _check_AWS_CloudFront_OriginAccessControl(
        self, region: str, rows: list[ResourceRecord]
    ) -> dict[int, CheckResult]:
        return self._scan(
            "global",
            rows,
            "cloudfront",
            "list_origin_access_controls",
            "OriginAccessControlList.Items",
            lambda item: _aliases(item.get("Id"), item.get("Name")),
        )

    def _check_AWS_CloudFront_CachePolicy(
        self, region: str, rows: list[ResourceRecord]
    ) -> dict[int, CheckResult]:
        return self._scan(
            "global",
            rows,
            "cloudfront",
            "list_cache_policies",
            "CachePolicyList.Items",
            lambda item: _aliases(item.get("Id"), _nested(item, "CachePolicyConfig", "Name")),
        )

    # SES identities and the most useful API Gateway/AppSync parents.
    def _check_AWS_SES_EmailIdentity(
        self, region: str, rows: list[ResourceRecord]
    ) -> dict[int, CheckResult]:
        return self._scan(
            region,
            rows,
            "sesv2",
            "list_email_identities",
            "EmailIdentities",
            lambda item: _aliases(item.get("IdentityName")),
        )

    def _check_AWS_SES_ConfigurationSet(
        self, region: str, rows: list[ResourceRecord]
    ) -> dict[int, CheckResult]:
        return self._scan(
            region,
            rows,
            "sesv2",
            "list_configuration_sets",
            "ConfigurationSets",
            lambda item: _aliases(item.get("ConfigurationSetName")),
        )

    def _check_AWS_ApiGateway_RestApi(
        self, region: str, rows: list[ResourceRecord]
    ) -> dict[int, CheckResult]:
        return self._scan(
            region,
            rows,
            "apigateway",
            "get_rest_apis",
            "items",
            lambda item: _aliases(item.get("id"), item.get("name")),
        )

    def _check_AWS_ApiGatewayV2_Api(
        self, region: str, rows: list[ResourceRecord]
    ) -> dict[int, CheckResult]:
        return self._scan(
            region,
            rows,
            "apigatewayv2",
            "get_apis",
            "Items",
            lambda item: _aliases(item.get("ApiId"), item.get("Name")),
        )

    def _check_AWS_AppSync_GraphqlApi(
        self, region: str, rows: list[ResourceRecord]
    ) -> dict[int, CheckResult]:
        return self._scan(
            region,
            rows,
            "appsync",
            "list_graphql_apis",
            "graphqlApis",
            lambda item: _aliases(item.get("apiId"), item.get("name")),
        )

    # API Gateway v1 children.
    def _check_AWS_ApiGateway_Stage(
        self, region: str, rows: list[ResourceRecord]
    ) -> dict[int, CheckResult]:
        return self._api_gateway_v1_children(region, rows, "get_stages", "item", "stageName")

    def _check_AWS_ApiGateway_Resource(
        self, region: str, rows: list[ResourceRecord]
    ) -> dict[int, CheckResult]:
        return self._api_gateway_v1_children(region, rows, "get_resources", "items", "id")

    def _check_AWS_ApiGateway_Deployment(
        self, region: str, rows: list[ResourceRecord]
    ) -> dict[int, CheckResult]:
        return self._api_gateway_v1_children(region, rows, "get_deployments", "items", "id")

    def _check_AWS_ApiGateway_Authorizer(
        self, region: str, rows: list[ResourceRecord]
    ) -> dict[int, CheckResult]:
        return self._api_gateway_v1_children(region, rows, "get_authorizers", "items", "id")

    def _check_AWS_ApiGateway_Method(
        self, region: str, rows: list[ResourceRecord]
    ) -> dict[int, CheckResult]:
        return self._api_gateway_v1_method_checks(region, rows, integration=False)

    def _check_AWS_ApiGateway_Integration(
        self, region: str, rows: list[ResourceRecord]
    ) -> dict[int, CheckResult]:
        return self._api_gateway_v1_method_checks(region, rows, integration=True)

    def _api_gateway_v1_resource_index(self, region: str, api: str) -> dict[str, dict[str, Any]]:
        return {
            str(item.get("id")): item
            for item in self._api_gateway_v1_items(region, api, "get_resources", "items")
        }

    def _api_gateway_v1_items(
        self,
        region: str,
        api: str,
        operation: str,
        result_key: str,
    ) -> list[Any]:
        key = ("apigateway-v1", region, api, operation)
        return self._cached_parent(
            key,
            lambda api=api: list(
                iter_paginated(
                    self.client("apigateway", region),
                    operation,
                    result_key,
                    restApiId=api,
                )
            ),
        )

    def _api_gateway_v1_method_checks(
        self,
        region: str,
        rows: list[ResourceRecord],
        *,
        integration: bool,
    ) -> dict[int, CheckResult]:
        results: dict[int, CheckResult] = {}
        grouped: dict[str, list[ResourceRecord]] = defaultdict(list)
        for row in rows:
            api, _, _ = row.resource_id.partition("|")
            grouped[api].append(row)
        for api, api_rows in grouped.items():
            try:
                resources = self._api_gateway_v1_resource_index(region, api)
                for row in api_rows:
                    _, _, resource_id_and_method = row.resource_id.partition("|")
                    resource_id, _, method = resource_id_and_method.partition("|")
                    methods = resources.get(resource_id, {}).get("resourceMethods") or {}
                    method_data = methods.get(method) or {}
                    found = method in methods and (
                        "methodIntegration" in method_data if integration else True
                    )
                    label = "integration" if integration else "method"
                    results[id(row)] = CheckResult(
                        "active" if found else "deleted",
                        f"{label} returned" if found else f"{label} not returned",
                    )
            except Exception as exc:
                result = _exception_result(exc)
                for row in api_rows:
                    results[id(row)] = result
        return results

    def _api_gateway_v1_children(
        self,
        region: str,
        rows: list[ResourceRecord],
        operation: str,
        result_key: str,
        identity_key: str,
    ) -> dict[int, CheckResult]:
        results: dict[int, CheckResult] = {}
        grouped: dict[str, list[ResourceRecord]] = defaultdict(list)
        for row in rows:
            api, _, _ = row.resource_id.partition("|")
            grouped[api].append(row)
        for api, api_rows in grouped.items():
            try:
                items = self._api_gateway_v1_items(region, api, operation, result_key)
                found = {_string(item.get(identity_key)) for item in items}
                for row in api_rows:
                    _, _, child = row.resource_id.partition("|")
                    results[id(row)] = CheckResult(
                        "active" if child in found else "deleted",
                        "child returned" if child in found else "child not returned",
                    )
            except Exception as exc:
                result = _exception_result(exc)
                for row in api_rows:
                    results[id(row)] = result
        return results

    # API Gateway v2 children.
    def _check_AWS_ApiGatewayV2_Stage(
        self, region: str, rows: list[ResourceRecord]
    ) -> dict[int, CheckResult]:
        return self._api_gateway_v2_children(region, rows, "get_stages", "Items", "StageName")

    def _check_AWS_ApiGatewayV2_Route(
        self, region: str, rows: list[ResourceRecord]
    ) -> dict[int, CheckResult]:
        return self._api_gateway_v2_children(region, rows, "get_routes", "Items", "RouteId")

    def _check_AWS_ApiGatewayV2_Integration(
        self, region: str, rows: list[ResourceRecord]
    ) -> dict[int, CheckResult]:
        return self._api_gateway_v2_children(
            region, rows, "get_integrations", "Items", "IntegrationId"
        )

    def _check_AWS_ApiGatewayV2_Authorizer(
        self, region: str, rows: list[ResourceRecord]
    ) -> dict[int, CheckResult]:
        return self._api_gateway_v2_children(
            region, rows, "get_authorizers", "Items", "AuthorizerId"
        )

    def _api_gateway_v2_children(
        self,
        region: str,
        rows: list[ResourceRecord],
        operation: str,
        result_key: str,
        identity_key: str,
    ) -> dict[int, CheckResult]:
        results: dict[int, CheckResult] = {}
        grouped: dict[str, list[ResourceRecord]] = defaultdict(list)
        for row in rows:
            api, _, _ = row.resource_id.partition("|")
            grouped[api].append(row)
        for api, api_rows in grouped.items():
            try:
                key = ("apigateway-v2", region, api, operation)
                items = self._cached_parent(
                    key,
                    lambda api=api: list(
                        iter_paginated(
                            self.client("apigatewayv2", region),
                            operation,
                            result_key,
                            ApiId=api,
                        )
                    ),
                )
                found = {_string(item.get(identity_key)) for item in items}
                for row in api_rows:
                    _, _, child = row.resource_id.partition("|")
                    results[id(row)] = CheckResult(
                        "active" if child in found else "deleted",
                        "child returned" if child in found else "child not returned",
                    )
            except Exception as exc:
                result = _exception_result(exc)
                for row in api_rows:
                    results[id(row)] = result
        return results

    def _check_AWS_AppSync_DataSource(
        self, region: str, rows: list[ResourceRecord]
    ) -> dict[int, CheckResult]:
        return self._appsync_children(region, rows, "list_data_sources", "dataSources", "name")

    def _check_AWS_AppSync_Function(
        self, region: str, rows: list[ResourceRecord]
    ) -> dict[int, CheckResult]:
        return self._appsync_children(region, rows, "list_functions", "functions", "functionId")

    def _check_AWS_AppSync_Resolver(
        self, region: str, rows: list[ResourceRecord]
    ) -> dict[int, CheckResult]:
        results: dict[int, CheckResult] = {}
        grouped: dict[tuple[str, str], list[ResourceRecord]] = defaultdict(list)
        for row in rows:
            type_name, _field_name = _appsync_resolver_identity(row)
            grouped[(row.parent_id or "", type_name)].append(row)
        for (api, type_name), api_rows in grouped.items():
            try:
                key = ("appsync-resolvers", region, api, type_name)
                items = self._cached_parent(
                    key,
                    lambda api=api, type_name=type_name: list(
                        iter_paginated(
                            self.client("appsync", region),
                            "list_resolvers",
                            "resolvers",
                            apiId=api,
                            typeName=type_name,
                        )
                    ),
                )
                found = {item.get("fieldName") for item in items}
                for row in api_rows:
                    _type_name, field_name = _appsync_resolver_identity(row)
                    active = field_name in found
                    results[id(row)] = CheckResult(
                        "active" if active else "deleted",
                        "resolver returned" if active else "resolver not returned",
                    )
            except Exception as exc:
                result = _exception_result(exc)
                for row in api_rows:
                    results[id(row)] = result
        return results

    def _appsync_children(
        self,
        region: str,
        rows: list[ResourceRecord],
        operation: str,
        result_key: str,
        identity_key: str,
    ) -> dict[int, CheckResult]:
        results: dict[int, CheckResult] = {}
        grouped: dict[str, list[ResourceRecord]] = defaultdict(list)
        for row in rows:
            grouped[row.parent_id or ""].append(row)
        for api, api_rows in grouped.items():
            try:
                key = ("appsync", region, api, operation)
                items = self._cached_parent(
                    key,
                    lambda api=api: list(
                        iter_paginated(
                            self.client("appsync", region), operation, result_key, apiId=api
                        )
                    ),
                )
                found = {_string(item.get(identity_key)) for item in items}
                for row in api_rows:
                    child = row.resource_id.rsplit("|", 1)[-1]
                    results[id(row)] = CheckResult(
                        "active" if child in found else "deleted",
                        "child returned" if child in found else "child not returned",
                    )
            except Exception as exc:
                result = _exception_result(exc)
                for row in api_rows:
                    results[id(row)] = result
        return results

    def _check_AWS_AppSync_ApiKey(
        self, region: str, rows: list[ResourceRecord]
    ) -> dict[int, CheckResult]:
        # CloudTrail redacts AppSync key IDs.  We cannot safely associate a live key with a
        # particular redacted event, so retain an explicit unknown status instead of guessing.
        return {
            id(row): CheckResult("unknown", "CloudTrail redacted the AppSync API key ID")
            for row in rows
        }

    # Cognito.
    def _check_AWS_Cognito_UserPool(
        self, region: str, rows: list[ResourceRecord]
    ) -> dict[int, CheckResult]:
        return self._scan(
            region,
            rows,
            "cognito-idp",
            "list_user_pools",
            "UserPools",
            lambda item: _aliases(item.get("Id"), item.get("Name")),
            MaxResults=60,
        )

    def _check_AWS_Cognito_IdentityPool(
        self, region: str, rows: list[ResourceRecord]
    ) -> dict[int, CheckResult]:
        return self._scan(
            region,
            rows,
            "cognito-identity",
            "list_identity_pools",
            "IdentityPools",
            lambda item: _aliases(item.get("IdentityPoolId"), item.get("IdentityPoolName")),
            MaxResults=60,
        )

    def _check_AWS_Cognito_UserPoolClient(
        self, region: str, rows: list[ResourceRecord]
    ) -> dict[int, CheckResult]:
        return self._cognito_children(
            region, rows, "list_user_pool_clients", "UserPoolClients", "ClientId"
        )

    def _check_AWS_Cognito_UserPoolDomain(
        self, region: str, rows: list[ResourceRecord]
    ) -> dict[int, CheckResult]:
        return self._cognito_children(region, rows, "list_user_pool_domains", "Domains", "Domain")

    def _check_AWS_Cognito_IdentityProvider(
        self, region: str, rows: list[ResourceRecord]
    ) -> dict[int, CheckResult]:
        return self._cognito_children(
            region, rows, "list_identity_providers", "Providers", "ProviderName"
        )

    def _cognito_children(
        self,
        region: str,
        rows: list[ResourceRecord],
        operation: str,
        result_key: str,
        identity_key: str,
    ) -> dict[int, CheckResult]:
        results: dict[int, CheckResult] = {}
        grouped: dict[str, list[ResourceRecord]] = defaultdict(list)
        for row in rows:
            grouped[row.parent_id or ""].append(row)
        for pool, pool_rows in grouped.items():
            try:
                key = ("cognito", region, pool, operation)
                items = self._cached_parent(
                    key,
                    lambda pool=pool: list(
                        iter_paginated(
                            self.client("cognito-idp", region),
                            operation,
                            result_key,
                            UserPoolId=pool,
                        )
                    ),
                )
                found = {_string(item.get(identity_key)) for item in items}
                for row in pool_rows:
                    child = row.resource_id.rsplit("|", 1)[-1]
                    results[id(row)] = CheckResult(
                        "active" if child in found else "deleted",
                        "child returned" if child in found else "child not returned",
                    )
            except Exception as exc:
                result = _exception_result(exc)
                for row in pool_rows:
                    results[id(row)] = result
        return results

    # Route 53.
    def _check_AWS_Route53_HostedZone(
        self, region: str, rows: list[ResourceRecord]
    ) -> dict[int, CheckResult]:
        return self._scan(
            "global",
            rows,
            "route53",
            "list_hosted_zones",
            "HostedZones",
            lambda item: _aliases(item.get("Id"), item.get("Name")),
        )

    def _check_AWS_Route53_RecordSet(
        self, region: str, rows: list[ResourceRecord]
    ) -> dict[int, CheckResult]:
        results: dict[int, CheckResult] = {}
        grouped: dict[str, list[ResourceRecord]] = defaultdict(list)
        for row in rows:
            zone, _, _ = row.resource_id.partition("|")
            grouped[zone].append(row)
        for zone, zone_rows in grouped.items():
            try:
                key = ("route53-record-sets", zone)
                records = self._cached_parent(
                    key,
                    lambda zone=zone: list(
                        iter_paginated(
                            self.client("route53", "global"),
                            "list_resource_record_sets",
                            "ResourceRecordSets",
                            HostedZoneId=zone,
                        )
                    ),
                )
                found = {
                    f"{zone}|{_normalize_dns_name(item.get('Name'))}|{item.get('Type')}"
                    for item in records
                }
                for row in zone_rows:
                    matches = row.resource_id in found
                    results[id(row)] = CheckResult(
                        "active" if matches else "deleted",
                        "record returned" if matches else "record not returned",
                    )
            except Exception as exc:
                result = _exception_result(exc)
                for row in zone_rows:
                    results[id(row)] = result
        return results

    # Bedrock and S3 Vectors.  These APIs are newer and may not exist in older botocore
    # versions; an unavailable client is intentionally reported as unknown by _check_group.
    def _check_AWS_Bedrock_KnowledgeBase(
        self, region: str, rows: list[ResourceRecord]
    ) -> dict[int, CheckResult]:
        return self._scan(
            region,
            rows,
            "bedrock-agent",
            "list_knowledge_bases",
            "knowledgeBaseSummaries",
            lambda item: _aliases(item.get("knowledgeBaseId"), item.get("name")),
        )

    def _check_AWS_Bedrock_DataSource(
        self, region: str, rows: list[ResourceRecord]
    ) -> dict[int, CheckResult]:
        results: dict[int, CheckResult] = {}
        grouped: dict[str, list[ResourceRecord]] = defaultdict(list)
        for row in rows:
            grouped[row.parent_id or ""].append(row)
        for knowledge_base, kb_rows in grouped.items():
            try:
                key = ("bedrock-data-sources", region, knowledge_base)
                items = self._cached_parent(
                    key,
                    lambda knowledge_base=knowledge_base: list(
                        iter_paginated(
                            self.client("bedrock-agent", region),
                            "list_data_sources",
                            "dataSourceSummaries",
                            knowledgeBaseId=knowledge_base,
                        )
                    ),
                )
                found = {_string(item.get("dataSourceId")) for item in items}
                for row in kb_rows:
                    child = row.resource_id.rsplit("|", 1)[-1]
                    results[id(row)] = CheckResult(
                        "active" if child in found else "deleted",
                        "data source returned" if child in found else "data source not returned",
                    )
            except Exception as exc:
                result = _exception_result(exc)
                for row in kb_rows:
                    results[id(row)] = result
        return results

    def _check_AWS_S3Vectors_VectorBucket(
        self, region: str, rows: list[ResourceRecord]
    ) -> dict[int, CheckResult]:
        return self._scan(
            region,
            rows,
            "s3vectors",
            "list_vector_buckets",
            "vectorBuckets",
            lambda item: _aliases(item.get("vectorBucketName"), item.get("vectorBucketArn")),
        )

    def _check_AWS_S3Vectors_Index(
        self, region: str, rows: list[ResourceRecord]
    ) -> dict[int, CheckResult]:
        results: dict[int, CheckResult] = {}
        for row in rows:
            bucket, _, index = row.resource_id.partition("|")
            try:
                response = self.client("s3vectors", region).list_indexes(vectorBucketName=bucket)
                found = any(item.get("indexName") == index for item in response.get("indexes", []))
                results[id(row)] = CheckResult(
                    "active" if found else "deleted",
                    "index returned" if found else "index not returned",
                )
            except Exception as exc:
                results[id(row)] = _exception_result(exc)
        return results


def _normalize_dns_name(value: Any) -> str:
    return str(value or "").rstrip(".").lower()


def discover_regions(session: boto3.Session) -> list[str]:
    """Return all regions available to the account, excluding opt-in-disabled regions."""
    try:
        client = session.client(
            "ec2",
            region_name="us-east-1",
            config=Config(retries={"mode": "standard", "max_attempts": 8}),
        )
        response = client.describe_regions(AllRegions=True)
    except Exception as exc:
        raise AuditError(f"could not discover AWS regions: {exc}") from exc
    regions = sorted(
        item["RegionName"]
        for item in response.get("Regions", [])
        if item.get("RegionName") and item.get("OptInStatus") != "not-opted-in"
    )
    if not regions:
        raise AuditError("AWS returned no enabled regions")
    return regions


def account_id_for(session: boto3.Session) -> str:
    """Resolve the account associated with the selected credentials."""
    try:
        account_id = (
            session.client("sts", region_name="us-east-1").get_caller_identity().get("Account")
        )
    except Exception as exc:
        raise AuditError(f"could not resolve the AWS account: {exc}") from exc
    if not account_id:
        raise AuditError("STS did not return an account ID")
    return str(account_id)


def _display_rows(rows: list[ResourceRecord], only_active: bool) -> list[ResourceRecord]:
    """Apply the CLI filter; unknown rows are deliberately retained."""
    if not only_active:
        return rows
    return [row for row in rows if row.status != "deleted"]


def _render_jsonl(rows: list[ResourceRecord], stream: Any) -> None:
    for row in rows:
        print(json.dumps(row.as_dict(), sort_keys=True), file=stream)


def _render_csv(rows: list[ResourceRecord], stream: Any) -> None:
    writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(row.as_dict())


def _render_text(rows: list[ResourceRecord], stream: Any) -> None:
    headers = ("STATUS", "REGION", "RESOURCE TYPE", "RESOURCE ID", "NAME", "CREATED AT", "DETAIL")
    print("\t".join(headers), file=stream)
    for row in rows:
        print(
            "\t".join(
                (
                    row.status.upper(),
                    row.region,
                    row.resource_type,
                    row.resource_id,
                    row.name or "",
                    row.created_at,
                    row.status_detail,
                )
            ),
            file=stream,
        )


def render(
    rows: list[ResourceRecord],
    *,
    output: str,
    only_active: bool,
    account_id: str,
    profile: str,
    regions: list[str],
    start: datetime,
    end_exclusive: datetime,
    runtime_seconds: float,
    warnings: Iterable[str] = (),
    stream: Any = sys.stdout,
) -> list[ResourceRecord]:
    """Render rows and, for text, append an audit summary."""
    displayed = _display_rows(rows, only_active)
    if output == "jsonl":
        _render_jsonl(displayed, stream)
    elif output == "csv":
        _render_csv(displayed, stream)
    else:
        _render_text(displayed, stream)
        counts = Counter(row.status for row in rows)
        affected_regions = {row.region for row in rows}
        print("", file=stream)
        print("Summary", file=stream)
        print(f"  Resources discovered: {len(rows)}", file=stream)
        print(f"  Resources displayed: {len(displayed)}", file=stream)
        print(f"  Active: {counts['active']}", file=stream)
        print(f"  Deleted: {counts['deleted']}", file=stream)
        print(f"  Unknown: {counts['unknown']}", file=stream)
        print(f"  Regions affected: {len(affected_regions)}", file=stream)
        print(f"  Regions queried: {len(regions)}", file=stream)
        print(f"  Account: {account_id}", file=stream)
        print(f"  Profile: {profile}", file=stream)
        print(
            f"  Date range: {start.date()} through {(end_exclusive - timedelta(days=1)).date()}",
            file=stream,
        )
        print(f"  Runtime: {runtime_seconds:.2f}s", file=stream)
        warning_list = list(warnings)
        print(f"  Warnings: {len(warning_list)}", file=stream)
        for warning in warning_list:
            print(f"    - {warning}", file=stream)
    return displayed


def _print_diagnostics(message: str) -> None:
    print(message, file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    """Run the audit command."""
    started = time.monotonic()
    audit_time = utc_now()
    try:
        args = parse_args(argv)
        session = boto3.Session(profile_name=args.profile)
        account_id = account_id_for(session)
        regions = args.regions or discover_regions(session)
        cache = (
            HistoryCache(
                args.cache_dir or user_cache_path("stelvio") / "resource-audit",
                account_id,
                args.profile,
                refresh=args.refresh_cache,
            )
            if args.cache
            else None
        )
        rows, unmapped_events, _inventory = collect_resources(
            session,
            regions,
            args.start_datetime,
            args.end_datetime,
            account_id,
            cache=cache,
            now=audit_time,
        )
        checker = LiveChecker(session, max_workers=args.check_workers)
        checker.check(rows)
        warnings = list(checker.warnings)
        if unmapped_events:
            warnings.append(
                "Unsupported creation events retained as unknown: "
                f"{len(unmapped_events)} event kinds"
            )
        render(
            rows,
            output=args.output,
            only_active=args.only_active,
            account_id=account_id,
            profile=args.profile,
            regions=regions,
            start=args.start_datetime,
            end_exclusive=args.end_datetime,
            runtime_seconds=time.monotonic() - started,
            warnings=warnings,
            stream=sys.stdout,
        )
        return 0
    except (AuditError, ValueError, ClientError) as exc:
        _print_diagnostics(f"audit failed: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
