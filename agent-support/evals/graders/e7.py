"""Grader for e7-queue-customize-kms."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from graders.inspect import customize_queue_kms, find_queue
from graders.model import CheckResult, GradeResult


def grade_e7(project_dir: Path, checks: dict[str, Any]) -> GradeResult:
    _ = project_dir
    functional = checks.get("functional", {})
    semantic = checks.get("semantic", {})
    footprint = checks.get("footprint", {})
    results: list[CheckResult] = []

    queue_name = footprint.get("preserve_queue", functional.get("queue_name", "orders"))
    kms_key = functional.get("kms_master_key_id", "alias/orders")

    queue = find_queue(queue_name)
    results.append(
        CheckResult(
            "queue_exists",
            "functional",
            queue is not None,
            queue_name if queue is not None else f"missing Queue {queue_name!r}",
        )
    )

    kms_value = customize_queue_kms(queue) if queue is not None else None
    kms_ok = kms_value == kms_key
    results.append(
        CheckResult(
            "kms_master_key_id",
            "functional",
            kms_ok,
            kms_value or "kms_master_key_id not set via customize",
        )
    )

    same_component = queue is not None and queue.name == queue_name
    if semantic.get("same_queue_component", True):
        results.append(CheckResult("same_queue_component", "semantic", same_component))

    uses_customize = queue is not None and bool(queue._customize.get("queue"))
    if semantic.get("uses_customize", True):
        results.append(
            CheckResult(
                "uses_customize",
                "semantic",
                uses_customize and kms_ok,
                "customize.queue.kms_master_key_id" if kms_ok else "missing customize path",
            )
        )

    functional_ids = {"queue_exists", "kms_master_key_id"}
    functional_pass = all(r.passed for r in results if r.check_id in functional_ids)
    semantic_checks = {r.check_id: r.passed for r in results if r.family == "semantic"}

    return GradeResult(
        case_id="e7-queue-customize-kms",
        functional_pass=functional_pass,
        semantic_checks=semantic_checks,
        checks=results,
    )
