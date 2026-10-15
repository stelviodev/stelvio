"""Unit tests for the AWS resource creation audit CLI."""

import gzip
import hashlib
import json
import threading
import weakref
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, date, datetime
from io import StringIO
from itertools import pairwise
from types import SimpleNamespace

from boto3.session import Session as BotoSession
from botocore.awsrequest import AWSResponse
from botocore.exceptions import ClientError
from botocore.hooks import HierarchicalEmitter
from pytest import approx, fixture, mark, raises

from tests.integration import audit_resources as audit


class AuditSession:
    """AWS boundary fake recording requests, pagination, and live resource changes."""

    def __init__(self):
        self.account_id = "123456789012"
        self.history = {"us-east-1": []}
        self.live = {}
        self.requests = []
        self.page_size = 50
        self.lookup_failure = None
        self.lock = threading.Lock()

    def get_partition_for_region(self, region):
        return "aws-cn" if region.startswith("cn-") else "aws"

    def lookup(self, region, kwargs):
        if self.lookup_failure is not None:
            self.lookup_failure(kwargs)
        summaries = []
        for event in self.history.get(region, []):
            summary = (
                event
                if "EventName" in event or "CloudTrailEvent" in event
                else {
                    "EventId": event["eventId"],
                    "EventName": event["eventName"],
                    "EventSource": event["eventSource"],
                    "EventTime": event["eventTime"],
                    "CloudTrailEvent": json.dumps(event),
                }
            )
            timestamp = audit.parse_timestamp(summary.get("EventTime"))
            if timestamp is None or kwargs["StartTime"] <= timestamp <= kwargs["EndTime"]:
                summaries.append(summary)
        start = int(kwargs.get("NextToken", 0))
        page = {"Events": summaries[start : start + self.page_size]}
        if start + self.page_size < len(summaries):
            page["NextToken"] = str(start + self.page_size)
        return page

    def client(self, service, region_name=None, config=None):
        session = self

        class Client:
            meta = SimpleNamespace(events=HierarchicalEmitter())

            def call(self, operation, kwargs):
                with session.lock:
                    session.requests.append((service, region_name, operation, kwargs))
                if service == "sts":
                    return {"Account": session.account_id}
                if service == "cloudtrail":
                    assert config.retries["total_max_attempts"] == 1
                    assert kwargs["LookupAttributes"] == [
                        {"AttributeKey": "ReadOnly", "AttributeValue": "false"}
                    ]
                    return session.lookup(region_name, kwargs)
                response = session.live[(service, operation)]
                if isinstance(response, Exception):
                    raise response
                return response(kwargs) if callable(response) else response

            def __getattr__(self, operation):
                return lambda **kwargs: self.call(operation, kwargs)

            def get_paginator(self, operation):
                client = self

                class Paginator:
                    def paginate(self, **kwargs):
                        response = client.call(operation, kwargs)
                        for page in response if isinstance(response, list) else [response]:
                            if isinstance(page, Exception):
                                raise page
                            yield page

                return Paginator()

        return Client()


@fixture
def audit_session(monkeypatch):
    session = AuditSession()
    monkeypatch.setattr(audit.boto3, "Session", lambda **_kwargs: session)
    monkeypatch.setattr(audit, "utc_now", lambda: datetime(2026, 10, 13, 12, tzinfo=UTC))
    monkeypatch.setattr(audit.time, "sleep", lambda _seconds: None)
    return session


def _audit_args(*extra):
    return [
        "--start-date=2026-10-13",
        "--end-date=2026-10-13",
        "--regions=us-east-1",
        "--output=jsonl",
        *extra,
    ]


def test_cli_streams_creation_pages_and_retains_unknowns(audit_session, monkeypatch, capsys):
    audit_session.page_size = 1
    audit_session.history["us-east-1"] = [
        {"EventName": "DescribeLogGroups", "CloudTrailEvent": "must not decode"},
        _event(
            "CreateLogGroup",
            event_id="new",
            event_time="2026-10-13T11:00:00Z",
            request={"logGroupName": "/audit"},
        ),
        _event(
            "CreateWidget",
            event_id="unknown",
            event_time="2026-10-13T10:30:00Z",
            source="widgets.amazonaws.com",
        ),
        _event(
            "CreateLogGroup",
            event_id="old",
            event_time="2026-10-13T10:00:00Z",
            request={"logGroupName": "/audit"},
        ),
    ]
    audit_session.live[("logs", "describe_log_groups")] = {
        "logGroups": [{"logGroupName": "/audit"}]
    }
    loads = json.loads

    def decode(value, **kwargs):
        assert value != "must not decode"
        return loads(value, **kwargs)

    monkeypatch.setattr(audit.json, "loads", decode)
    assert audit.main(_audit_args("--only-active")) == 0
    records = [loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [(row["resource_type"], row["event_id"], row["status"]) for row in records] == [
        ("AWS::Logs::LogGroup", "old", "active"),
        ("AWS::Unknown::widgets.amazonaws.com:CreateWidget", "unknown", "unknown"),
    ]
    requests = [request[3] for request in audit_session.requests if request[0] == "cloudtrail"]
    assert len(requests) == 4
    assert all(
        request["EndTime"] == datetime(2026, 10, 13, 12, tzinfo=UTC) for request in requests
    )


@mark.parametrize("cache", [False, True])
def test_cli_releases_previous_page_payloads_during_discovery(
    audit_session,
    tmp_path,
    monkeypatch,
    capsys,
    cache,
):
    class RequestPayload(dict):
        pass

    audit_session.page_size = 2
    audit_session.history["us-east-1"] = [
        _event(
            "CreateLogGroup",
            event_id=f"group-{index}",
            event_time="2026-10-13T10:00:00Z",
            request={"logGroupName": f"/{index}"},
        )
        for index in range(6)
    ]
    audit_session.live[("logs", "describe_log_groups")] = {"logGroups": []}
    payloads = []
    loads = json.loads

    def decode(value, **kwargs):
        parsed = loads(value, **kwargs)
        if "requestParameters" in parsed:
            parsed["requestParameters"] = RequestPayload(parsed["requestParameters"])
            payloads.append(weakref.ref(parsed["requestParameters"]))
        return parsed

    def check_previous_page_released(kwargs):
        if kwargs.get("NextToken") == "4":
            assert len(payloads) == 4
            assert all(reference() is None for reference in payloads[:2])

    monkeypatch.setattr(audit.json, "loads", decode)
    audit_session.lookup_failure = check_previous_page_released
    args = _audit_args(f"--cache-dir={tmp_path}") if cache else _audit_args()
    assert audit.main(args) == 0
    assert [loads(line)["resource_id"] for line in capsys.readouterr().out.splitlines()] == [
        f"/{index}" for index in range(6)
    ]


@mark.parametrize("name", ["CreateLogGroup", "CreateWidget", "DescribeLogGroups"])
def test_cli_normalizes_missing_summary_names_before_filtering(audit_session, capsys, name):
    audit_session.history["us-east-1"] = [
        {
            "CloudTrailEvent": json.dumps(
                _event(
                    name,
                    event_id="embedded-name",
                    event_time="2026-10-13T10:00:00Z",
                    request={"logGroupName": "/app"},
                )
            ),
        },
    ]
    audit_session.live[("logs", "describe_log_groups")] = {
        "logGroups": [{"logGroupName": "/app"}],
    }
    assert audit.main(_audit_args()) == 0
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [(row["event_name"], row["status"]) for row in rows] == (
        []
        if name == "DescribeLogGroups"
        else [(name, "active" if name == "CreateLogGroup" else "unknown")]
    )


@mark.parametrize("include_parent", [False, True])
@mark.parametrize(
    ("parent_response", "parent_status", "child_status", "child_calls"),
    [
        ({"logGroups": []}, "deleted", "deleted", 0),
        ({"logGroups": [{"logGroupName": "/app"}]}, "active", "active", 1),
        (
            ClientError(
                {"Error": {"Code": "AccessDeniedException", "Message": "denied"}},
                "DescribeLogGroups",
            ),
            "unknown",
            "active",
            1,
        ),
        (
            ClientError(
                {"Error": {"Code": "ValidationException", "Message": "invalid"}},
                "DescribeLogGroups",
            ),
            "deleted",
            "active",
            1,
        ),
        (
            [
                {"logGroups": []},
                ClientError(
                    {"Error": {"Code": "ThrottlingException", "Message": "rate"}},
                    "DescribeLogGroups",
                ),
            ],
            "unknown",
            "active",
            1,
        ),
    ],
)
def test_cli_checks_parent_before_child(  # noqa: PLR0913 - fixtures plus independent parameters
    audit_session,
    capsys,
    include_parent,
    parent_response,
    parent_status,
    child_status,
    child_calls,
):
    events = [
        _event(
            "CreateLogStream",
            event_id="stream",
            event_time="2026-10-13T10:00:00Z",
            request={"logGroupName": "/app", "logStreamName": "one"},
        )
    ]
    if include_parent:
        events.append(
            _event(
                "CreateLogGroup",
                event_id="group",
                event_time="2026-10-13T09:00:00Z",
                request={"logGroupName": "/app"},
            )
        )
    audit_session.history["us-east-1"] = events
    audit_session.live[("logs", "describe_log_groups")] = parent_response
    audit_session.live[("logs", "describe_log_streams")] = {
        "logStreams": [{"logStreamName": "one"}],
    }
    assert audit.main(_audit_args("--check-workers=1")) == 0
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [(record["resource_type"], record["status"]) for record in records] == (
        [("AWS::Logs::LogGroup", parent_status)] if include_parent else []
    ) + [("AWS::Logs::LogStream", child_status)]
    calls = [request[2] for request in audit_session.requests if request[0] == "logs"]
    assert calls == ["describe_log_groups"] + ["describe_log_streams"] * child_calls


def test_cli_cache_replays_history_and_checks_live_state(
    audit_session, monkeypatch, tmp_path, capsys
):
    audit_session.history["us-east-1"] = [
        _event(
            "CreateLogGroup",
            event_id="old",
            event_time="2026-10-13T09:00:00Z",
            request={"logGroupName": "/old"},
        ),
        _event(
            "CreateLogGroup",
            event_id="overlap",
            event_time="2026-10-13T11:20:00Z",
            request={"logGroupName": "/old"},
        ),
    ]
    audit_session.live[("logs", "describe_log_groups")] = {"logGroups": [{"logGroupName": "/old"}]}
    args = _audit_args(f"--cache-dir={tmp_path}")
    assert audit.main(args) == 0
    first = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [(row["resource_id"], row["created_at"], row["status"]) for row in first] == [
        ("/old", "2026-10-13T09:00:00Z", "active"),
    ]
    audit_session.history["us-east-1"].append(
        _event(
            "CreateLogGroup",
            event_id="late",
            event_time="2026-10-13T11:58:00Z",
            request={"logGroupName": "/late"},
        )
    )
    audit_session.live[("logs", "describe_log_groups")] = {
        "logGroups": [{"logGroupName": "/late"}]
    }
    monkeypatch.setattr(audit, "utc_now", lambda: datetime(2026, 10, 13, 12, 5, tzinfo=UTC))
    assert audit.main(args) == 0
    second = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [(row["resource_id"], row["created_at"], row["status"]) for row in second] == [
        ("/old", "2026-10-13T09:00:00Z", "deleted"),
        ("/late", "2026-10-13T11:58:00Z", "active"),
    ]
    lookups = [request[3] for request in audit_session.requests if request[0] == "cloudtrail"]
    assert [request["StartTime"] for request in lookups] == [
        datetime(2026, 10, 13, tzinfo=UTC),
        datetime(2026, 10, 13, 11, tzinfo=UTC),
    ]
    entries = list(tmp_path.rglob("*.jsonl.gz"))
    assert len(entries) == 1
    with gzip.open(entries[0], "rt") as stream:
        cached = [json.loads(line) for line in stream]
    assert cached[0]["last_full_refresh"] == "2026-10-13T12:00:00Z"
    assert [record["event"]["eventId"] for record in cached[1:-1]] == ["old", "overlap", "late"]
    assert cached[-1] == {"complete": True, "events": 3}
    assert all("status" not in record["event"] for record in cached[1:-1])


@mark.parametrize("force_refresh", [False, True])
def test_cli_cache_full_refresh_finds_older_delayed_events(
    audit_session,
    monkeypatch,
    tmp_path,
    capsys,
    force_refresh,
):
    audit_session.history["us-east-1"] = [
        _event(
            "CreateLogGroup",
            event_id="old",
            event_time="2026-10-13T09:00:00Z",
            request={"logGroupName": "/old"},
        ),
    ]
    audit_session.live[("logs", "describe_log_groups")] = {"logGroups": []}
    args = _audit_args(f"--cache-dir={tmp_path}")
    assert audit.main(args) == 0
    capsys.readouterr()
    monkeypatch.setattr(audit, "utc_now", lambda: datetime(2026, 10, 13, 12, 5, tzinfo=UTC))
    assert audit.main(args) == 0
    capsys.readouterr()
    audit_session.history["us-east-1"].append(
        _event(
            "CreateLogGroup",
            event_id="very-late",
            event_time="2026-10-13T09:30:00Z",
            request={"logGroupName": "/very-late"},
        )
    )
    minute = 6 if force_refresh else 15
    monkeypatch.setattr(audit, "utc_now", lambda: datetime(2026, 10, 13, 12, minute, tzinfo=UTC))
    assert audit.main([*args, *(["--refresh-cache"] if force_refresh else [])]) == 0
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [row["resource_id"] for row in records] == ["/old", "/very-late"]
    lookups = [request[3] for request in audit_session.requests if request[0] == "cloudtrail"]
    assert [request["StartTime"] for request in lookups] == [
        datetime(2026, 10, 13, tzinfo=UTC),
        datetime(2026, 10, 13, 11, tzinfo=UTC),
        datetime(2026, 10, 13, tzinfo=UTC),
    ]


@mark.parametrize("dimension", ["account", "profile", "region", "partition", "day"])
def test_cli_history_cache_is_isolated(audit_session, monkeypatch, tmp_path, capsys, dimension):
    audit_session.history["us-east-1"] = [
        _event(
            "CreateLogGroup",
            event_id="old",
            event_time="2026-10-13T09:00:00Z",
            request={"logGroupName": "/old"},
        ),
    ]
    audit_session.live[("logs", "describe_log_groups")] = {"logGroups": []}
    assert audit.main(_audit_args(f"--cache-dir={tmp_path}")) == 0
    capsys.readouterr()
    region, day, extra = "us-east-1", "2026-10-13", []
    if dimension == "account":
        audit_session.account_id = "999999999999"
    elif dimension == "profile":
        extra = ["--profile=another"]
    elif dimension == "region":
        region = "eu-west-1"
    elif dimension == "partition":
        monkeypatch.setattr(audit_session, "get_partition_for_region", lambda _region: "aws-cn")
    else:
        day = "2026-10-12"
    audit_session.history[region] = [
        _event(
            "CreateLogGroup",
            event_id="new",
            event_time=f"{day}T09:00:00Z",
            region=region,
            request={"logGroupName": "/new"},
        ),
    ]
    assert (
        audit.main(
            [
                f"--start-date={day}",
                f"--end-date={day}",
                f"--regions={region}",
                "--output=jsonl",
                f"--cache-dir={tmp_path}",
                *extra,
            ]
        )
        == 0
    )
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [(row["account_id"], row["region"], row["resource_id"]) for row in records] == [
        (audit_session.account_id, region, "/new"),
    ]
    assert len(list(tmp_path.rglob("*.jsonl.gz"))) == 2


@mark.parametrize(
    "damage",
    [
        "gzip",
        "identity",
        "missing-footer",
        "event",
        "resources",
        "region",
        "timestamp",
        "event-id",
        "trailing-data",
    ],
)
def test_cli_replaces_invalid_cache_without_replaying_partial_rows(
    audit_session,
    tmp_path,
    capsys,
    damage,
):
    audit_session.history["us-east-1"] = [
        _event(
            "CreateLogGroup",
            event_id="old",
            event_time="2026-10-13T09:00:00Z",
            request={"logGroupName": "/old"},
        ),
    ]
    audit_session.live[("logs", "describe_log_groups")] = {"logGroups": []}
    args = _audit_args(f"--cache-dir={tmp_path}")
    assert audit.main(args) == 0
    capsys.readouterr()
    (entry,) = tmp_path.rglob("*.jsonl.gz")
    with gzip.open(entry, "rt") as stream:
        records = [json.loads(line) for line in stream]
    if damage == "gzip":
        entry.write_bytes(b"broken gzip")
    else:
        if damage == "identity":
            records[0]["version"] = -1
        elif damage == "missing-footer":
            records.pop()
        elif damage == "event":
            records[1]["event"] = []
        elif damage in {"resources", "region", "timestamp", "event-id"}:
            key, value = {
                "resources": ("resources", None),
                "region": ("awsRegion", []),
                "timestamp": ("eventTime", "invalid"),
                "event-id": ("eventId", ""),
            }[damage]
            records[1]["event"][key] = value
        else:
            records.append({"event": records[1]["event"]})
        with gzip.open(entry, "wt") as stream:
            stream.writelines(json.dumps(record) + "\n" for record in records)
    audit_session.history["us-east-1"] = [
        _event(
            "CreateLogGroup",
            event_id="new",
            event_time="2026-10-13T09:30:00Z",
            request={"logGroupName": "/new"},
        ),
    ]
    assert audit.main(args) == 0
    output = capsys.readouterr()
    assert [json.loads(line)["resource_id"] for line in output.out.splitlines()] == ["/new"]
    assert "ignoring unusable history cache" in output.err
    assert len(list(tmp_path.rglob("*.jsonl.gz"))) == 1


@mark.parametrize("warm", [False, True])
def test_cli_failed_pagination_does_not_publish_cache_or_partial_output(
    audit_session,
    tmp_path,
    capsys,
    warm,
):
    audit_session.page_size = 1
    audit_session.history["us-east-1"] = [
        _event(
            "CreateLogGroup",
            event_id=f"group-{minute}",
            event_time=f"2026-10-13T11:{minute}:00Z",
            request={"logGroupName": f"/{minute}"},
        )
        for minute in ["30", "45"]
    ]
    audit_session.live[("logs", "describe_log_groups")] = {"logGroups": []}
    args = _audit_args(f"--cache-dir={tmp_path}")
    original = None
    if warm:
        assert audit.main(args) == 0
        capsys.readouterr()
        (entry,) = tmp_path.rglob("*.jsonl.gz")
        original = entry.read_bytes()

    def fail_second_page(kwargs):
        if "NextToken" in kwargs:
            raise ClientError(
                {"Error": {"Code": "AccessDeniedException", "Message": "denied"}}, "LookupEvents"
            )

    audit_session.lookup_failure = fail_second_page
    assert audit.main(args) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert "CloudTrail lookup failed in us-east-1" in output.err
    entries = list(tmp_path.rglob("*.jsonl.gz"))
    assert [entry.read_bytes() for entry in entries] == ([original] if warm else [])
    assert list(tmp_path.rglob(".history-*")) == []


def test_cli_cache_write_failure_warns_and_keeps_audit_usable(audit_session, tmp_path, capsys):
    blocked = tmp_path / "existing-file"
    blocked.write_text("preserve me")
    audit_session.history["us-east-1"] = [
        _event(
            "CreateWidget",
            event_id="widget",
            event_time="2026-10-13T10:00:00Z",
            source="widgets.amazonaws.com",
        ),
    ]
    assert audit.main(_audit_args(f"--cache-dir={blocked}")) == 0
    output = capsys.readouterr()
    assert [json.loads(line)["event_id"] for line in output.out.splitlines()] == ["widget"]
    assert "history cache write failed" in output.err
    assert blocked.read_text() == "preserve me"


@mark.parametrize(
    "code", ["NotFoundException", "TooManyRequestsException", "AccessDeniedException"]
)
def test_live_checks_share_terminal_parent_lookup_failures(audit_session, code):
    audit_session.live[("apigateway", "get_rest_apis")] = {"items": [{"id": "api"}]}
    audit_session.live[("apigateway", "get_resources")] = ClientError(
        {"Error": {"Code": code, "Message": "terminal failure"}},
        "GetResources",
    )
    rows = [
        _resource_row(kind, identity, parent_type="AWS::ApiGateway::RestApi", parent_id="api")
        for kind, identity in [
            ("AWS::ApiGateway::Method", "api|resource|GET"),
            ("AWS::ApiGateway::Integration", "api|resource|GET"),
            ("AWS::ApiGateway::Resource", "api|resource"),
        ]
    ]
    audit.LiveChecker(audit_session, max_workers=3).check(rows)
    expected = "deleted" if code == "NotFoundException" else "unknown"
    assert [(row.status, row.status_detail) for row in rows] == [
        (expected, f"{code}: terminal failure"),
    ] * 3
    assert [(request[2], request[3]) for request in audit_session.requests] == [
        ("get_rest_apis", {}),
        ("get_resources", {"restApiId": "api"}),
    ]


def test_scheduler_limits_one_service_without_starving_another(audit_session):
    barrier = threading.Barrier(5)
    lock = threading.Lock()
    active = {"iam": 0, "logs": 0}
    peaks = {"iam": 0, "total": 0}

    def respond(service, kwargs):
        with lock:
            active[service] += 1
            peaks["iam"] = max(peaks["iam"], active["iam"])
            peaks["total"] = max(peaks["total"], sum(active.values()))
        try:
            if service == "logs" or kwargs["RoleName"] in {
                "role-0",
                "role-16",
                "role-32",
                "role-48",
            }:
                barrier.wait(timeout=3)
            return (
                {"Role": {"Arn": kwargs["RoleName"]}}
                if service == "iam"
                else {
                    "logGroups": [{"logGroupName": "/app"}],
                }
            )
        finally:
            with lock:
                active[service] -= 1

    audit_session.live[("iam", "get_role")] = lambda kwargs: respond("iam", kwargs)
    audit_session.live[("logs", "describe_log_groups")] = lambda kwargs: respond("logs", kwargs)
    rows = [_resource_row("AWS::IAM::Role", f"role-{index}") for index in range(96)]
    rows.append(_resource_row("AWS::Logs::LogGroup", "/app"))
    audit.LiveChecker(audit_session, max_workers=8).check(rows)
    assert [row.status for row in rows] == ["active"] * 97
    assert peaks == {"iam": 4, "total": 5}
    assert len(audit_session.requests) == 97


@mark.parametrize("workers", [1, 2])
def test_scheduler_obeys_worker_bound_for_direct_checks(audit_session, workers):
    barrier = threading.Barrier(workers)
    lock = threading.Lock()
    active = 0
    peak = 0

    def role(kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            barrier.wait(timeout=3)
            return {"Role": {"Arn": kwargs["RoleName"]}}
        finally:
            with lock:
                active -= 1

    audit_session.live[("iam", "get_role")] = role
    rows = [_resource_row("AWS::IAM::Role", f"role-{index}") for index in range(32)]
    audit.LiveChecker(audit_session, max_workers=workers).check(rows)
    assert [row.status for row in rows] == ["active"] * 32
    assert peak == workers


def test_gateway_paces_http_attempts_including_retries_across_versions(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(audit.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(audit.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    session = BotoSession(aws_access_key_id="testing", aws_secret_access_key="testing")  # noqa: S106
    checker = audit.LiveChecker(session)
    v1 = checker.client("apigateway", "us-east-1")
    v2 = checker.client("apigatewayv2", "eu-west-1")
    statuses = iter([429, 200, 200, 200])
    attempts = []

    def send(request):
        status = next(statuses)
        attempts.append((now[0], request.url))
        body = json.dumps(
            {"message": "rate exceeded"} if status == 429 else {"items": []}
        ).encode()
        headers = {"Content-Type": "application/json"}
        if status == 429:
            headers["x-amzn-errortype"] = "TooManyRequestsException"
        return AWSResponse(request.url, status, headers, SimpleNamespace(stream=lambda: [body]))

    monkeypatch.setattr(v1._endpoint.http_session, "send", send)
    monkeypatch.setattr(v2._endpoint.http_session, "send", send)
    v1.get_resources(restApiId="api")
    v2.get_apis()
    v1.get_resources(restApiId="api")
    assert len(attempts) == 4
    assert all(later[0] - earlier[0] >= 0.125 for earlier, later in pairwise(attempts))
    resource_times = [
        timestamp for timestamp, url in attempts if url.endswith("/restapis/api/resources")
    ]
    assert len(resource_times) == 3
    assert all(later - earlier >= 0.5 - 1e-9 for earlier, later in pairwise(resource_times))


def test_gateway_coordinates_resource_and_shared_limits_for_concurrent_sends(monkeypatch):
    now = [0.0]
    reserved = threading.Event()
    release = threading.Event()

    def clock():
        timestamp = now[0]
        if threading.current_thread().name == "gateway_0" and not reserved.is_set():
            reserved.set()
            assert release.wait(timeout=3)
        return timestamp

    monkeypatch.setattr(audit.time, "monotonic", clock)
    monkeypatch.setattr(audit.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    session = BotoSession(aws_access_key_id="testing", aws_secret_access_key="testing")  # noqa: S106
    checker = audit.LiveChecker(session)
    client = checker.client("apigateway", "us-east-1")
    v2 = checker.client("apigatewayv2", "eu-west-1")
    attempts = []

    def send(request):
        attempts.append((now[0], request.url))
        return AWSResponse(
            request.url,
            200,
            {"Content-Type": "application/json"},
            SimpleNamespace(stream=lambda: [b'{"items": []}']),
        )

    monkeypatch.setattr(client._endpoint.http_session, "send", send)
    monkeypatch.setattr(v2._endpoint.http_session, "send", send)
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="gateway") as executor:
        first = executor.submit(client.get_resources, restApiId="first")
        assert reserved.wait(timeout=3)
        second = executor.submit(v2.get_apis)
        try:
            # An independently admitted second request exposes the race; a
            # coordinated request waits until the first reservation finishes.
            second.result(timeout=0.1)
        except TimeoutError:
            pass
        finally:
            release.set()
        assert first.result(timeout=3)["ResponseMetadata"]["HTTPStatusCode"] == 200
        assert second.result(timeout=3)["ResponseMetadata"]["HTTPStatusCode"] == 200
    client.get_resources(restApiId="third")
    assert len(attempts) == 3
    assert all(later[0] - earlier[0] >= 0.125 for earlier, later in pairwise(attempts))
    resource_times = [timestamp for timestamp, url in attempts if "/restapis/" in url]
    assert len(resource_times) == 2
    assert resource_times[1] - resource_times[0] >= 0.5


def test_cli_groups_appsync_resolver_arns_by_api_and_type(audit_session, capsys):
    audit_session.history["us-east-1"] = [
        _event(
            "CreateResolver",
            event_id=f"{type_name}-{field}",
            event_time="2026-10-13T10:00:00Z",
            source="appsync.amazonaws.com",
            request={"apiId": "api", "typeName": type_name, "fieldName": field},
            response={
                "resolver": {
                    "resolverArn": (
                        f"arn:aws:appsync:us-east-1:123456789012:apis/api"
                        f"/types/{type_name}/resolvers/{field}"
                    ),
                },
            },
        )
        for type_name, field in [("Query", "one"), ("Query", "two"), ("Mutation", "save")]
    ]
    audit_session.live[("appsync", "list_graphql_apis")] = {"graphqlApis": [{"apiId": "api"}]}
    audit_session.live[("appsync", "list_resolvers")] = lambda kwargs: {
        "resolvers": [
            {"fieldName": field}
            for field in (["one", "two"] if kwargs["typeName"] == "Query" else ["save"])
        ],
    }
    assert audit.main(_audit_args()) == 0
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [(row["name"], row["status"]) for row in records] == [
        ("Mutation.save", "active"),
        ("Query.one", "active"),
        ("Query.two", "active"),
    ]
    requests = [request for request in audit_session.requests if request[2] == "list_resolvers"]
    assert sorted(
        [request[3] for request in requests], key=lambda request: request["typeName"]
    ) == [
        {"apiId": "api", "typeName": "Mutation"},
        {"apiId": "api", "typeName": "Query"},
    ]


@mark.parametrize(
    ("reference", "expected"),
    [
        ("fn", "deleted"),
        ("arn:aws:lambda:us-east-1:123456789012:function:fn", "deleted"),
        ("arn:aws:lambda:us-east-1:123456789012:function:fn:alias", "deleted"),
        ("arn:aws:lambda:us-east-1:999999999999:function:fn", "active"),
        ("arn:aws:lambda:us-west-2:123456789012:function:fn", "active"),
    ],
)
def test_parent_resolution_matches_function_names_and_arns_conservatively(
    audit_session,
    reference,
    expected,
):
    parent = _resource_row(
        "AWS::Lambda::Function", "arn:aws:lambda:us-east-1:123456789012:function:fn"
    )
    child = _resource_row(
        "AWS::Lambda::Url", "url", parent_type="AWS::Lambda::Function", parent_id=reference
    )
    audit_session.live[("lambda", "list_functions")] = {"Functions": []}
    audit_session.live[("lambda", "get_function_url_config")] = {
        "FunctionUrl": "https://example.test"
    }
    audit.LiveChecker(audit_session, max_workers=1).check([parent, child])
    assert [parent.status, child.status] == ["deleted", expected]
    assert [request[2] for request in audit_session.requests] == ["list_functions"] + (
        ["get_function_url_config"] if expected == "active" else []
    )


def test_ambiguous_parent_aliases_do_not_suppress_child_checks(audit_session):
    parents = [
        replace(_resource_row("AWS::Logs::LogGroup", identity), name="/shared")
        for identity in ["/a", "/b"]
    ]
    child = _resource_row(
        "AWS::Logs::LogStream",
        "/shared|one",
        parent_type="AWS::Logs::LogGroup",
        parent_id="/shared",
    )
    audit_session.live[("logs", "describe_log_groups")] = {"logGroups": []}
    audit_session.live[("logs", "describe_log_streams")] = {
        "logStreams": [{"logStreamName": "one"}]
    }
    audit.LiveChecker(audit_session, max_workers=1).check([*parents, child])
    assert [row.status for row in [*parents, child]] == ["deleted", "deleted", "active"]
    assert [request[2] for request in audit_session.requests] == [
        "describe_log_groups",
        "describe_log_streams",
    ]


@mark.parametrize("kind", ["mapping", "redacted", "unsupported"])
def test_parent_shortcuts_preserve_independent_and_unknown_resources(audit_session, kind):
    if kind == "mapping":
        parent = _resource_row("AWS::Lambda::Function", "fn")
        child = _resource_row("AWS::Lambda::EventSourceMapping", "mapping")
        audit_session.live[("lambda", "list_functions")] = {"Functions": []}
        audit_session.live[("lambda", "list_event_source_mappings")] = {
            "EventSourceMappings": [{"UUID": "mapping"}],
        }
        expected, calls = "active", ["list_functions", "list_event_source_mappings"]
    elif kind == "redacted":
        parent = _resource_row("AWS::AppSync::GraphqlApi", "api")
        child = _resource_row("AWS::AppSync::ApiKey", "redacted-key:api")
        audit_session.live[("appsync", "list_graphql_apis")] = {"graphqlApis": []}
        expected, calls = "unknown", ["list_graphql_apis"]
    else:
        parent = _resource_row("AWS::Logs::LogGroup", "/app")
        child = _resource_row("AWS::Widget::Thing", "thing")
        audit_session.live[("logs", "describe_log_groups")] = {"logGroups": []}
        expected, calls = "unknown", ["describe_log_groups"]
    child.parent_type, child.parent_id = parent.resource_type, parent.resource_id
    audit.LiveChecker(audit_session, max_workers=1).check([parent, child])
    assert [parent.status, child.status] == ["deleted", expected]
    assert [request[2] for request in audit_session.requests] == calls


def test_streamed_regions_deduplicate_events_and_global_resources(audit_session):
    old = _event(
        "CreateRole",
        event_id="old",
        event_time="2026-10-13T09:00:00Z",
        source="iam.amazonaws.com",
        request={"roleName": "role"},
    )
    newer = _event(
        "CreateRole",
        event_id="newer",
        event_time="2026-10-13T10:00:00Z",
        source="iam.amazonaws.com",
        request={"roleName": "role"},
    )
    unknown = _event(
        "CreateWidget",
        event_id="unknown",
        event_time="2026-10-13T11:00:00Z",
        source="widgets.amazonaws.com",
    )
    audit_session.history = {"us-east-1": [newer, old, unknown], "eu-west-1": [old, unknown]}
    rows, unmapped, inventory = audit.collect_resources(
        audit_session,
        ["us-east-1", "eu-west-1"],
        datetime(2026, 10, 13, tzinfo=UTC),
        datetime(2026, 10, 14, tzinfo=UTC),
        audit_session.account_id,
    )
    assert [(row.region, row.event_id, row.created_at) for row in rows] == [
        ("global", "old", "2026-10-13T09:00:00Z"),
        ("us-east-1", "unknown", "2026-10-13T11:00:00Z"),
    ]
    assert unmapped == {"widgets.amazonaws.com:CreateWidget": 1}
    assert (inventory.events_seen, inventory.unknown_events) == (3, 1)


@mark.parametrize("flags", [[], ["--cache"], ["--refresh-cache"]])
def test_cli_history_cache_is_opt_in_with_default_location(
    audit_session,
    monkeypatch,
    tmp_path,
    capsys,
    flags,
):
    monkeypatch.setattr(audit, "user_cache_path", lambda _app: tmp_path)
    audit_session.history["us-east-1"] = [
        _event(
            "CreateWidget",
            event_id="widget",
            event_time="2026-10-13T10:00:00Z",
            source="widgets.amazonaws.com",
        ),
    ]
    assert audit.main(_audit_args(*flags)) == 0
    assert [json.loads(line)["event_id"] for line in capsys.readouterr().out.splitlines()] == [
        "widget"
    ]
    assert len(list((tmp_path / "resource-audit").rglob("*.jsonl.gz"))) == (1 if flags else 0)


def _event(  # noqa: PLR0913
    event_name: str,
    *,
    event_id: str = "event-1",
    event_time: str = "2026-10-10T12:00:00Z",
    source: str = "logs.amazonaws.com",
    region: str = "us-east-1",
    request: dict | None = None,
    response: dict | None = None,
    resources: list[dict] | None = None,
) -> dict:
    return {
        "eventId": event_id,
        "eventName": event_name,
        "eventSource": source,
        "eventTime": event_time,
        "awsRegion": region,
        "requestParameters": request or {},
        "responseElements": response or {},
        "resources": resources or [],
    }


def test_resolve_date_range_uses_utc_calendar_days() -> None:
    start, end = audit.resolve_date_range(None, None, today=date(2026, 10, 13))

    assert start == datetime(2026, 10, 6, tzinfo=UTC)
    assert end == datetime(2026, 10, 14, tzinfo=UTC)


def test_parse_regions_trims_and_deduplicates() -> None:
    assert audit.parse_regions(" us-east-1,eu-west-1,us-east-1 ,, eu-west-1 ") == [
        "us-east-1",
        "eu-west-1",
    ]

    with raises(ValueError, match="at least one region"):
        audit.parse_regions(" , ")


def test_discover_regions_excludes_opt_in_disabled_regions() -> None:
    class EC2:
        def describe_regions(self, **kwargs):
            assert kwargs["AllRegions"] is True
            return {
                "Regions": [
                    {"RegionName": "us-east-2", "OptInStatus": "opt-in-not-required"},
                    {"RegionName": "eu-west-1", "OptInStatus": "opted-in"},
                    {"RegionName": "af-south-1", "OptInStatus": "not-opted-in"},
                ]
            }

    class Session:
        def client(self, service, region_name, config=None):
            assert service == "ec2"
            assert region_name == "us-east-1"
            return EC2()

    assert audit.discover_regions(Session()) == ["eu-west-1", "us-east-2"]


def test_lookup_cloudtrail_region_follows_every_next_token() -> None:
    class CloudTrail:
        def __init__(self) -> None:
            self.requests = []

        def lookup_events(self, **kwargs):
            self.requests.append(kwargs)
            if len(self.requests) == 1:
                return {
                    "Events": [
                        {
                            "EventId": "one",
                            "EventName": "CreateLogGroup",
                            "EventSource": "logs.amazonaws.com",
                            "EventTime": "2026-10-10T12:00:00Z",
                            "CloudTrailEvent": json.dumps(
                                _event(
                                    "CreateLogGroup",
                                    event_id="one",
                                    request={"logGroupName": "/one"},
                                )
                            ),
                        }
                    ],
                    "NextToken": "next-page",
                }
            return {
                "Events": [
                    {
                        "EventId": "two",
                        "EventName": "CreateLogGroup",
                        "EventSource": "logs.amazonaws.com",
                        "EventTime": "2026-10-11T12:00:00Z",
                        "CloudTrailEvent": json.dumps(
                            _event(
                                "CreateLogGroup", event_id="two", request={"logGroupName": "/two"}
                            )
                        ),
                    }
                ]
            }

    class Session:
        def __init__(self) -> None:
            self.cloudtrail = CloudTrail()

        def client(self, service, region_name, config=None):
            assert service == "cloudtrail"
            assert region_name == "us-east-1"
            return self.cloudtrail

    session = Session()
    events = list(
        audit.lookup_cloudtrail_region(
            session,
            "us-east-1",
            datetime(2026, 10, 10, tzinfo=UTC),
            datetime(2026, 10, 12, tzinfo=UTC),
            rate_limiter=audit.CloudTrailRateLimiter(min_interval=0, sleeper=lambda _: None),
        )
    )

    assert [event["eventId"] for event in events] == ["one", "two"]
    assert len(session.cloudtrail.requests) == 2
    assert session.cloudtrail.requests[1]["NextToken"] == "next-page"
    assert all(request["MaxResults"] == 50 for request in session.cloudtrail.requests)
    assert all(
        request["LookupAttributes"] == [{"AttributeKey": "ReadOnly", "AttributeValue": "false"}]
        for request in session.cloudtrail.requests
    )


def test_cloudtrail_rate_limiter_spaces_requests_in_one_region() -> None:
    now = [0.0]
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    limiter = audit.CloudTrailRateLimiter(
        min_interval=0.6,
        clock=lambda: now[0],
        sleeper=sleep,
    )

    limiter.wait("us-east-1")
    limiter.wait("us-east-1")
    limiter.wait("us-east-1")

    assert sleeps == approx([0.6, 0.6])


def _throttling_error() -> ClientError:
    return ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}},
        "LookupEvents",
    )


def test_cloudtrail_throttled_request_retries_and_succeeds(monkeypatch) -> None:
    class CloudTrail:
        def __init__(self) -> None:
            self.calls = 0

        def lookup_events(self, **kwargs):
            self.calls += 1
            if self.calls < 3:
                raise _throttling_error()
            return {"Events": []}

    class Session:
        def __init__(self) -> None:
            self.cloudtrail = CloudTrail()

        def client(self, service, region_name, config=None):
            assert service == "cloudtrail"
            assert region_name == "us-east-1"
            assert config.retries["total_max_attempts"] == 1
            return self.cloudtrail

    session = Session()
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(audit.time, "sleep", sleep)
    limiter = audit.CloudTrailRateLimiter(min_interval=0, sleeper=lambda _: None)
    events = list(
        audit.lookup_cloudtrail_region(
            session,
            "us-east-1",
            datetime(2026, 10, 10, tzinfo=UTC),
            datetime(2026, 10, 12, tzinfo=UTC),
            rate_limiter=limiter,
        )
    )

    assert events == []
    assert session.cloudtrail.calls == 3
    assert len(sleeps) == 2


def test_cloudtrail_retry_exhaustion_fails_the_audit(monkeypatch) -> None:
    class CloudTrail:
        def __init__(self) -> None:
            self.calls = 0

        def lookup_events(self, **kwargs):
            self.calls += 1
            raise _throttling_error()

    class Session:
        def __init__(self) -> None:
            self.cloudtrail = CloudTrail()

        def client(self, service, region_name, config=None):
            return self.cloudtrail

    monkeypatch.setattr(audit.time, "sleep", lambda _: None)
    session = Session()

    with raises(audit.AuditError, match="CloudTrail lookup failed in us-east-1"):
        audit.collect_resources(
            session,
            ["us-east-1"],
            datetime(2026, 10, 10, tzinfo=UTC),
            datetime(2026, 10, 12, tzinfo=UTC),
            "123456789012",
            max_workers=1,
        )
    assert session.cloudtrail.calls == audit.CLOUDTRAIL_MAX_ATTEMPTS


def test_cloudtrail_regions_can_run_concurrently() -> None:
    barrier = threading.Barrier(2)

    class CloudTrail:
        def lookup_events(self, **kwargs):
            barrier.wait(timeout=2)
            return {"Events": []}

    class Session:
        def client(self, service, region_name, config=None):
            return CloudTrail()

    rows, unmapped, _inventory = audit.collect_resources(
        Session(),
        ["us-east-1", "eu-west-1"],
        datetime(2026, 10, 10, tzinfo=UTC),
        datetime(2026, 10, 12, tzinfo=UTC),
        "123456789012",
        max_workers=2,
    )
    assert rows == []
    assert unmapped == {}


def test_normalize_cloudtrail_summary_generates_stable_sha256_event_id() -> None:
    summary = {
        "EventName": "CreateWidget",
        "EventSource": "widgets.amazonaws.com",
        "EventTime": "2026-10-10T12:00:00Z",
        "AwsRegion": "us-east-1",
        "Resources": [{"ResourceType": "AWS::Widget::Thing", "ResourceName": "thing-1"}],
    }

    first = audit.normalize_cloudtrail_summary(summary, "us-east-1")
    second = audit.normalize_cloudtrail_summary(summary, "us-east-1")
    expected_event = {
        "eventId": None,
        "eventName": "CreateWidget",
        "eventSource": "widgets.amazonaws.com",
        "eventTime": "2026-10-10T12:00:00Z",
        "awsRegion": "us-east-1",
        "userIdentity": None,
        "resources": [{"resourceType": "AWS::Widget::Thing", "resourceName": "thing-1"}],
        "errorCode": None,
    }
    expected_digest = hashlib.sha256(
        json.dumps(expected_event, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]

    assert first is not None
    assert second is not None
    assert first["eventId"] == second["eventId"] == f"synthetic-{expected_digest}"
    assert len(first["eventId"]) == len("synthetic-") + 16


def test_extract_resources_deduplicates_and_keeps_unsupported_events() -> None:
    events = [
        _event(
            "CreateLogGroup",
            event_id="later",
            event_time="2026-10-10T12:00:00Z",
            request={"logGroupName": "/audit"},
        ),
        _event(
            "CreateLogGroup",
            event_id="earlier",
            event_time="2026-10-09T12:00:00Z",
            request={"logGroupName": "/audit"},
        ),
        _event(
            "CreateWidget",
            event_id="unknown",
            source="widgets.amazonaws.com",
            resources=[{"resourceType": "AWS::Widget::Thing", "resourceName": "thing-1"}],
        ),
    ]

    rows, unmapped, inventory = audit.extract_resources(events, "123456789012")

    assert len(rows) == 2
    log_group = next(row for row in rows if row.resource_type == "AWS::Logs::LogGroup")
    assert log_group.created_at == "2026-10-09T12:00:00Z"
    assert log_group.event_id == "earlier"
    assert rows[1].status == "unknown"
    assert unmapped["widgets.amazonaws.com:CreateWidget"] == 1
    assert inventory.events_seen == 3


def test_live_checker_log_streams_uses_paginated_parent_listing() -> None:
    class Paginator:
        def paginate(self, **kwargs):
            assert kwargs == {"logGroupName": "/app"}
            return [
                {"logStreams": [{"logStreamName": "first"}]},
                {"logStreams": [{"logStreamName": "second"}]},
            ]

    class Logs:
        def get_paginator(self, operation):
            assert operation == "describe_log_streams"
            return Paginator()

    class Session:
        def client(self, service, region_name, config=None):
            assert service == "logs"
            assert region_name == "us-east-1"
            return Logs()

    rows = [
        audit.ResourceRecord(
            account_id="123456789012",
            region="us-east-1",
            event_region="us-east-1",
            resource_type="AWS::Logs::LogStream",
            resource_id="/app|second",
            name="second",
            created_at="2026-10-10T12:00:00Z",
            event_name="CreateLogStream",
            event_source="logs.amazonaws.com",
            event_id="stream-1",
        ),
        audit.ResourceRecord(
            account_id="123456789012",
            region="us-east-1",
            event_region="us-east-1",
            resource_type="AWS::Logs::LogStream",
            resource_id="/app|gone",
            name="gone",
            created_at="2026-10-10T12:00:00Z",
            event_name="CreateLogStream",
            event_source="logs.amazonaws.com",
            event_id="stream-2",
        ),
    ]

    audit.LiveChecker(Session(), max_workers=1).check(rows)

    assert [row.status for row in rows] == ["active", "deleted"]


def test_missing_and_access_denied_statuses_are_distinct() -> None:
    missing = audit._exception_result(
        audit.ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "gone"}},
            "DescribeThing",
        )
    )
    denied = audit._exception_result(
        audit.ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "no access"}},
            "DescribeThing",
        )
    )

    assert missing.status == "deleted"
    assert denied.status == "unknown"


def _row(status: str, resource_id: str) -> audit.ResourceRecord:
    return audit.ResourceRecord(
        account_id="123456789012",
        region="us-east-1",
        event_region="us-east-1",
        resource_type="AWS::Widget::Thing",
        resource_id=resource_id,
        name=resource_id,
        created_at="2026-10-10T12:00:00Z",
        event_name="CreateWidget",
        event_source="widgets.amazonaws.com",
        event_id=resource_id,
        status=status,
        status_detail=f"{status} detail",
    )


def _resource_row(
    resource_type: str,
    resource_id: str,
    *,
    parent_id: str | None = None,
    parent_type: str | None = None,
) -> audit.ResourceRecord:
    return audit.ResourceRecord(
        account_id="123456789012",
        region="us-east-1",
        event_region="us-east-1",
        resource_type=resource_type,
        resource_id=resource_id,
        name=resource_id,
        created_at="2026-10-10T12:00:00Z",
        event_name="CreateThing",
        event_source="example.amazonaws.com",
        event_id=resource_id,
        parent_id=parent_id,
        parent_type=parent_type,
    )


def test_check_workers_can_be_tuned_independently() -> None:
    assert audit.parse_args([]).check_workers == audit.MAX_WORKERS
    assert audit.parse_args(["--check-workers", "3"]).check_workers == 3

    checker = audit.LiveChecker(object(), max_workers=3)
    assert checker.max_workers == 3


def test_live_checker_reuses_clients_and_batches_ec2_ids() -> None:
    paginator_requests: list[dict] = []

    class Paginator:
        def paginate(self, **kwargs):
            paginator_requests.append(kwargs)
            return [{"Vpcs": [{"VpcId": "vpc-1"}]}]

    class EC2:
        def get_paginator(self, operation):
            assert operation == "describe_vpcs"
            return Paginator()

    class Session:
        def __init__(self) -> None:
            self.client_calls = 0

        def client(self, service, region_name, config=None):
            self.client_calls += 1
            return EC2()

    session = Session()
    checker = audit.LiveChecker(session, max_workers=1)
    rows = [_resource_row("AWS::EC2::VPC", "vpc-1"), _resource_row("AWS::EC2::VPC", "vpc-2")]

    results = checker._check_AWS_EC2_VPC("us-east-1", rows)

    assert results[id(rows[0])].status == "active"
    assert results[id(rows[1])].status == "deleted"
    assert paginator_requests == [{"VpcIds": ["vpc-1", "vpc-2"]}]
    assert session.client_calls == 1
    assert checker.client("ec2", "us-east-1") is checker.client("ec2", "us-east-1")


def test_api_gateway_parent_lookup_is_reused_for_methods_and_integrations() -> None:
    paginator_calls = 0

    class Paginator:
        def paginate(self, **kwargs):
            assert kwargs == {"restApiId": "api-1"}
            return [
                {
                    "items": [
                        {
                            "id": "resource-1",
                            "resourceMethods": {"GET": {"methodIntegration": {"type": "MOCK"}}},
                        }
                    ]
                }
            ]

    class ApiGateway:
        meta = SimpleNamespace(events=HierarchicalEmitter())

        def get_paginator(self, operation):
            nonlocal paginator_calls
            assert operation == "get_resources"
            paginator_calls += 1
            return Paginator()

    class Session:
        def client(self, service, region_name, config=None):
            return ApiGateway()

    checker = audit.LiveChecker(Session(), max_workers=1)
    method = _resource_row("AWS::ApiGateway::Method", "api-1|resource-1|GET")
    integration = _resource_row("AWS::ApiGateway::Integration", "api-1|resource-1|GET")

    method_result = checker._check_AWS_ApiGateway_Method("us-east-1", [method])
    integration_result = checker._check_AWS_ApiGateway_Integration("us-east-1", [integration])

    assert method_result[id(method)].status == "active"
    assert integration_result[id(integration)].status == "active"
    assert paginator_calls == 1


def test_appsync_resolver_parent_lookup_is_reused() -> None:
    paginator_calls = 0

    class Paginator:
        def paginate(self, **kwargs):
            assert kwargs == {"apiId": "api-1", "typeName": "Query"}
            return [{"resolvers": [{"fieldName": "getOne"}, {"fieldName": "getTwo"}]}]

    class AppSync:
        def get_paginator(self, operation):
            nonlocal paginator_calls
            assert operation == "list_resolvers"
            paginator_calls += 1
            return Paginator()

    class Session:
        def client(self, service, region_name, config=None):
            return AppSync()

    checker = audit.LiveChecker(Session(), max_workers=1)
    first = _resource_row("AWS::AppSync::Resolver", "api-1|Query|getOne", parent_id="api-1")
    second = _resource_row("AWS::AppSync::Resolver", "api-1|Query|getTwo", parent_id="api-1")

    first_result = checker._check_AWS_AppSync_Resolver("us-east-1", [first])
    second_result = checker._check_AWS_AppSync_Resolver("us-east-1", [second])

    assert first_result[id(first)].status == "active"
    assert second_result[id(second)].status == "active"
    assert paginator_calls == 1


def test_only_active_keeps_unknown_rows_and_structured_stdout_has_rows_only() -> None:
    rows = [_row("active", "active-1"), _row("deleted", "deleted-1"), _row("unknown", "unknown-1")]
    output = StringIO()

    displayed = audit.render(
        rows,
        output="jsonl",
        only_active=True,
        account_id="123456789012",
        profile="default",
        regions=["us-east-1"],
        start=datetime(2026, 10, 10, tzinfo=UTC),
        end_exclusive=datetime(2026, 10, 11, tzinfo=UTC),
        runtime_seconds=0.25,
        stream=output,
    )

    records = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [row.resource_id for row in displayed] == ["active-1", "unknown-1"]
    assert [record["resource_id"] for record in records] == ["active-1", "unknown-1"]
    assert "Summary" not in output.getvalue()


def test_text_output_summary_reports_deleted_rows_even_when_filtered() -> None:
    output = StringIO()

    audit.render(
        [_row("active", "active-1"), _row("deleted", "deleted-1"), _row("unknown", "unknown-1")],
        output="text",
        only_active=True,
        account_id="123456789012",
        profile="default",
        regions=["us-east-1", "eu-west-1"],
        start=datetime(2026, 10, 10, tzinfo=UTC),
        end_exclusive=datetime(2026, 10, 11, tzinfo=UTC),
        runtime_seconds=1.5,
        stream=output,
    )

    text = output.getvalue()
    assert "Resources discovered: 3" in text
    assert "Resources displayed: 2" in text
    assert "Deleted: 1" in text
    assert "Unknown: 1" in text
    assert "Runtime: 1.50s" in text


def test_csv_output_has_stable_header() -> None:
    output = StringIO()

    audit.render(
        [_row("unknown", "unknown-1")],
        output="csv",
        only_active=False,
        account_id="123456789012",
        profile="default",
        regions=["us-east-1"],
        start=datetime(2026, 10, 10, tzinfo=UTC),
        end_exclusive=datetime(2026, 10, 11, tzinfo=UTC),
        runtime_seconds=0.1,
        stream=output,
    )

    assert output.getvalue().splitlines()[0] == ",".join(audit.CSV_FIELDS)
