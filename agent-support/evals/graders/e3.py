"""Grader for e3-async-queue."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from graders.inspect import (
    components_of_type,
    find_http_api,
    find_queue,
    function_source,
    handler_config_for_route,
    link_target_names,
    queue_subscriptions,
    read_handler_source,
    route_matches,
    source_calls_name,
    source_enqueues,
)
from graders.model import CheckResult, GradeResult
from stelvio.aws.queue import Queue


def grade_e3(project_dir: Path, checks: dict[str, Any]) -> GradeResult:
    functional = checks.get("functional", {})
    semantic = checks.get("semantic", {})
    results: list[CheckResult] = []

    route_spec = functional.get("route", {})
    method = route_spec.get("method", "POST")
    path = route_spec.get("path", "/orders")
    queue_name = functional.get("queue_name", "orders")
    process_fn = functional.get("process_function", "process_order")

    api = find_http_api()
    queue = find_queue(queue_name) or (
        components_of_type(Queue)[0] if components_of_type(Queue) else None
    )

    route_ok = api is not None and route_matches(api, method, path)
    results.append(CheckResult("route_exists", "functional", route_ok))

    queue_ok = queue is not None
    results.append(CheckResult("queue_exists", "functional", queue_ok))

    config = handler_config_for_route(api, method, path) if api else None
    accept_source = read_handler_source(project_dir, config) if config else None
    accept_fn = function_source(accept_source, "accept") if accept_source else None

    accept_async = (
        accept_fn is not None
        and not source_calls_name(accept_fn, process_fn)
        and source_enqueues(accept_fn)
    )
    results.append(
        CheckResult(
            "accept_returns_immediately",
            "functional",
            accept_async,
            "accept enqueues without inline process"
            if accept_async
            else "accept still does inline work or does not enqueue",
        )
    )

    subs = queue_subscriptions(queue) if queue is not None else []
    has_consumer = len(subs) >= 1
    results.append(
        CheckResult(
            "queue_has_consumer",
            "functional",
            has_consumer,
            "queue.subscribe present" if has_consumer else "no queue consumer",
        )
    )

    producer_links = False
    if config is not None and queue is not None:
        producer_links = queue.name in link_target_names(list(config.links))
    if semantic.get("producer_links_queue", True):
        results.append(
            CheckResult(
                "producer_links_queue",
                "semantic",
                producer_links,
                "accept links queue" if producer_links else "accept does not link queue",
            )
        )

    uses_subscribe = has_consumer
    if semantic.get("uses_subscribe", True):
        results.append(
            CheckResult(
                "uses_subscribe",
                "semantic",
                uses_subscribe,
                "queue.subscribe used" if uses_subscribe else "missing subscribe",
            )
        )

    functional_ids = {
        "route_exists",
        "queue_exists",
        "accept_returns_immediately",
        "queue_has_consumer",
    }
    functional_pass = all(r.passed for r in results if r.check_id in functional_ids)
    semantic_checks = {r.check_id: r.passed for r in results if r.family == "semantic"}

    return GradeResult(
        case_id="e3-async-queue",
        functional_pass=functional_pass,
        semantic_checks=semantic_checks,
        checks=results,
    )
