"""Grader for e4-s3-notify-function."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from graders.inspect import (
    bucket_notify_subscriptions,
    find_bucket,
    notify_handler_entrypoint,
    notify_is_function,
    notify_is_queue,
)
from graders.model import CheckResult, GradeResult


def grade_e4(project_dir: Path, checks: dict[str, Any]) -> GradeResult:
    _ = project_dir
    functional = checks.get("functional", {})
    semantic = checks.get("semantic", {})
    results: list[CheckResult] = []

    prefix = functional.get("filter_prefix", "incoming/")
    suffix = functional.get("filter_suffix", ".jpg")
    handler = functional.get("handler", "functions/process_image.handler")

    bucket = find_bucket()
    results.append(CheckResult("bucket_exists", "functional", bucket is not None))

    matched = None
    if bucket is not None:
        for sub in bucket_notify_subscriptions(bucket):
            if (
                notify_is_function(sub)
                and sub._filter_prefix == prefix
                and sub._filter_suffix == suffix
                and notify_handler_entrypoint(sub) == handler
            ):
                matched = sub
                break

    notify_ok = matched is not None
    results.append(
        CheckResult(
            "notify_function_filters",
            "functional",
            notify_ok,
            f"notify_function {prefix!r} {suffix!r} → {handler}"
            if notify_ok
            else "missing matching notify_function",
        )
    )

    uses_notify_function = False
    if bucket is not None:
        uses_notify_function = any(
            notify_is_function(sub) and not notify_is_queue(sub)
            for sub in bucket_notify_subscriptions(bucket)
        )
    if semantic.get("uses_notify_function", True):
        results.append(
            CheckResult(
                "uses_notify_function",
                "semantic",
                uses_notify_function and notify_ok,
                "Bucket.notify_function" if notify_ok else "not using notify_function correctly",
            )
        )

    functional_ids = {"bucket_exists", "notify_function_filters"}
    functional_pass = all(r.passed for r in results if r.check_id in functional_ids)
    semantic_checks = {r.check_id: r.passed for r in results if r.family == "semantic"}

    return GradeResult(
        case_id="e4-s3-notify-function",
        functional_pass=functional_pass,
        semantic_checks=semantic_checks,
        checks=results,
    )
