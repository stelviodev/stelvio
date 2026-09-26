"""Grader for e6-fanout-topic."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from graders.inspect import (
    components_of_type,
    find_http_api,
    find_topic,
    handler_config_for_route,
    link_target_names,
    queue_subscriptions,
    read_handler_source,
    source_publishes_sns,
    topic_consumer_count,
)
from graders.model import CheckResult, GradeResult
from stelvio.aws.function import Function
from stelvio.aws.queue import Queue
from stelvio.aws.topic import Topic


def _producer_link_and_publish(project_dir: Path, topic: Topic | None) -> tuple[bool, bool]:
    linked = False
    publishes = False
    if topic is None:
        return linked, publishes

    api = find_http_api()
    if api is not None:
        for method, path in (("POST", "/orders/complete"), ("POST", "/orders"), ("POST", "/")):
            config = handler_config_for_route(api, method, path)
            if config is None:
                continue
            linked = topic.name in link_target_names(list(config.links))
            source = read_handler_source(project_dir, config)
            if source is not None:
                publishes = source_publishes_sns(source)
            return linked, publishes

    for fn in components_of_type(Function):
        if topic.name in link_target_names(list(fn.config.links)):
            linked = True
            source = read_handler_source(project_dir, fn.config)
            if source is not None:
                publishes = source_publishes_sns(source)
            break
    return linked, publishes


def grade_e6(project_dir: Path, checks: dict[str, Any]) -> GradeResult:
    functional = checks.get("functional", {})
    semantic = checks.get("semantic", {})
    results: list[CheckResult] = []

    min_consumers = functional.get("min_consumers", 3)
    topics = components_of_type(Topic)
    queues = components_of_type(Queue)

    topic = find_topic() if len(topics) == 1 else (topics[0] if topics else None)
    one_topic = len(topics) == 1
    results.append(CheckResult("one_topic", "functional", one_topic))

    consumer_count = topic_consumer_count(topic) if topic is not None else 0
    fanout_ok = one_topic and consumer_count >= min_consumers
    results.append(
        CheckResult(
            "fanout_consumers",
            "functional",
            fanout_ok,
            f"{consumer_count} topic consumers" if fanout_ok else "insufficient topic fanout",
        )
    )

    competing = (
        not topics and len(queues) == 1 and len(queue_subscriptions(queues[0])) >= min_consumers
    )
    results.append(
        CheckResult(
            "not_competing_queue",
            "functional",
            not competing,
            "not a competing-consumer queue" if not competing else "competing queue fan-in",
        )
    )

    producer_linked, publishes = _producer_link_and_publish(project_dir, topic)
    results.append(CheckResult("producer_publishes", "functional", publishes))

    if semantic.get("one_topic", True):
        results.append(CheckResult("semantic_one_topic", "semantic", one_topic))
    if semantic.get("uses_subscribe", True):
        detail = (
            "topic.subscribe / subscribe_queue" if fanout_ok else "missing topic subscriptions"
        )
        results.append(CheckResult("uses_subscribe", "semantic", fanout_ok, detail))
    if semantic.get("producer_links_topic", True):
        results.append(CheckResult("producer_links_topic", "semantic", producer_linked))

    functional_ids = {
        "one_topic",
        "fanout_consumers",
        "not_competing_queue",
        "producer_publishes",
    }
    functional_pass = all(r.passed for r in results if r.check_id in functional_ids)
    semantic_checks = {r.check_id: r.passed for r in results if r.family == "semantic"}

    return GradeResult(
        case_id="e6-fanout-topic",
        functional_pass=functional_pass,
        semantic_checks=semantic_checks,
        checks=results,
    )
