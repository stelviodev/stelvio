"""Boto3 read-back assertions for CloudWatch Logs."""

from __future__ import annotations

from typing import Literal

from .assert_helpers import _boto3_session


def assert_log_group(name: str, *, retention_days: int | Literal["forever"] | None = None) -> None:
    """Assert a log group exists, with the given retention when passed."""
    logs = _boto3_session().client("logs")
    # describe_log_groups matches by prefix, so a sibling like "<name>-2" would also come back
    pages = logs.get_paginator("describe_log_groups").paginate(logGroupNamePrefix=name)
    group = next((g for p in pages for g in p["logGroups"] if g["logGroupName"] == name), None)
    assert group is not None, name
    if retention_days == "forever":
        # never-expire is the absence of a retention policy, not retentionInDays == 0
        assert "retentionInDays" not in group
    elif retention_days is not None:
        assert group.get("retentionInDays") == retention_days
