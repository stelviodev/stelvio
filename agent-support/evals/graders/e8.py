"""Grader for e8-minimal-modification."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from graders.inspect import (
    components_of_type,
    find_dynamo_table,
    find_http_api,
    find_topic,
    handler_config_for_route,
    handler_entrypoint,
    link_target_names,
    read_handler_source,
    route_matches,
    source_publishes_sns,
    source_writes_dynamo_item,
)
from graders.model import CheckResult, GradeResult
from stelvio.aws.topic import Topic


def grade_e8(project_dir: Path, checks: dict[str, Any]) -> GradeResult:
    functional = checks.get("functional", {})
    semantic = checks.get("semantic", {})
    footprint = checks.get("footprint", {})
    results: list[CheckResult] = []
    observations: list[str] = []

    route_spec = functional.get("route", {})
    method = route_spec.get("method", "POST")
    path = route_spec.get("path", "/users")
    api_name = footprint.get("preserve_api", "users-api")
    table_name = footprint.get("preserve_table", "users")
    expected_handler = footprint.get("preserve_handlers", ["functions/users.create"])[0]
    topic_name = functional.get("topic_name", "user-created")

    api = find_http_api(api_name) or find_http_api()
    table = find_dynamo_table(table_name)
    topics = components_of_type(Topic)
    topic = find_topic(topic_name) or (topics[0] if topics else None)

    route_ok = api is not None and route_matches(api, method, path)
    results.append(CheckResult("route_exists", "functional", route_ok))
    results.append(CheckResult("table_exists", "functional", table is not None))

    config = handler_config_for_route(api, method, path) if api else None
    source = read_handler_source(project_dir, config) if config else None

    creates_ok = source is not None and source_writes_dynamo_item(source)
    results.append(CheckResult("creates_user", "functional", creates_ok))

    publishes = source is not None and source_publishes_sns(source)
    topic_ok = topic is not None
    results.append(CheckResult("topic_exists", "functional", topic_ok))
    results.append(CheckResult("publishes_event", "functional", publishes))

    names_ok = (
        api is not None
        and api.name == api_name
        and table is not None
        and table.name == table_name
        and config is not None
        and handler_entrypoint(config) == expected_handler
    )
    if semantic.get("names_preserved", True):
        results.append(
            CheckResult(
                "names_preserved",
                "semantic",
                names_ok,
                "api/route/table/handler preserved" if names_ok else "renamed existing pieces",
            )
        )

    linked = False
    if config is not None and topic is not None:
        linked = topic.name in link_target_names(list(config.links))
    # table should still be linked too
    table_linked = False
    if config is not None and table is not None:
        table_linked = table_name in link_target_names(list(config.links))
    if semantic.get("producer_links_topic", True):
        results.append(CheckResult("producer_links_topic", "semantic", linked))
    if semantic.get("table_still_linked", True):
        results.append(CheckResult("table_still_linked", "semantic", table_linked))

    if not names_ok:
        observations.append("unrelated edits")

    functional_ids = {
        "route_exists",
        "table_exists",
        "creates_user",
        "topic_exists",
        "publishes_event",
    }
    functional_pass = all(r.passed for r in results if r.check_id in functional_ids)
    semantic_checks = {r.check_id: r.passed for r in results if r.family == "semantic"}

    return GradeResult(
        case_id="e8-minimal-modification",
        functional_pass=functional_pass,
        semantic_checks=semantic_checks,
        checks=results,
        observations=observations,
    )
