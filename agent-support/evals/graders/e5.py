"""Grader for e5-s3-notify-queue."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from graders.inspect import (
    bucket_notify_subscriptions,
    components_of_type,
    find_bucket,
    notify_is_function,
    notify_is_queue,
    queue_subscriptions,
)
from graders.model import CheckResult, GradeResult
from stelvio.aws.queue import Queue


def grade_e5(project_dir: Path, checks: dict[str, Any]) -> GradeResult:
    _ = project_dir
    semantic = checks.get("semantic", {})
    results: list[CheckResult] = []

    bucket = find_bucket()
    queues = components_of_type(Queue)
    results.append(CheckResult("bucket_exists", "functional", bucket is not None))
    results.append(CheckResult("queue_exists", "functional", len(queues) >= 1))

    has_notify_queue = False
    has_direct_function_only = False
    if bucket is not None:
        subs = bucket_notify_subscriptions(bucket)
        has_notify_queue = any(notify_is_queue(sub) for sub in subs)
        has_direct_function_only = (
            any(notify_is_function(sub) for sub in subs) and not has_notify_queue
        )

    results.append(
        CheckResult(
            "bucket_notifies_queue",
            "functional",
            has_notify_queue,
            "notify_queue present" if has_notify_queue else "no queue notification",
        )
    )

    has_worker = any(len(queue_subscriptions(q)) >= 1 for q in queues)
    results.append(
        CheckResult(
            "queue_has_worker",
            "functional",
            has_worker,
            "queue.subscribe present" if has_worker else "no worker subscription",
        )
    )

    # Direct Bucket → Function (no queue) fails functional.
    if has_direct_function_only:
        results.append(
            CheckResult(
                "not_direct_function",
                "functional",
                False,
                "direct Bucket→Function is insufficient",
            )
        )

    functional_ids = {"bucket_exists", "queue_exists", "bucket_notifies_queue", "queue_has_worker"}
    functional_pass = all(r.passed for r in results if r.check_id in functional_ids)
    if has_direct_function_only:
        functional_pass = False

    if semantic.get("uses_notify_queue", True):
        results.append(CheckResult("uses_notify_queue", "semantic", has_notify_queue))
    if semantic.get("uses_subscribe", True):
        results.append(CheckResult("uses_subscribe", "semantic", has_worker))

    semantic_checks = {r.check_id: r.passed for r in results if r.family == "semantic"}

    return GradeResult(
        case_id="e5-s3-notify-queue",
        functional_pass=functional_pass,
        semantic_checks=semantic_checks,
        checks=results,
    )
