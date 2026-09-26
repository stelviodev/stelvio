"""Grader for e2-missing-link."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from graders.inspect import (
    find_dynamo_table,
    find_http_api,
    fixture_dir_for,
    handler_config_for_route,
    handler_entrypoint,
    link_target_names,
    read_handler_source,
    route_matches,
)
from graders.model import CheckResult, GradeResult


def grade_e2(project_dir: Path, checks: dict[str, Any]) -> GradeResult:
    functional = checks.get("functional", {})
    semantic = checks.get("semantic", {})
    footprint = checks.get("footprint", {})
    results: list[CheckResult] = []

    route_spec = functional.get("route", {})
    method = route_spec.get("method", "GET")
    path = route_spec.get("path", "/users/{id}")
    table_name = functional.get("table_name", "users")
    api_name = footprint.get("preserve_api", "users-api")
    expected_handler = footprint.get("preserve_handlers", ["functions/users.get"])[0]

    api = find_http_api(api_name) or find_http_api()
    table = find_dynamo_table(table_name)

    route_ok = api is not None and route_matches(api, method, path)
    results.append(CheckResult("route_exists", "functional", route_ok))

    table_ok = table is not None
    results.append(CheckResult("table_exists", "functional", table_ok))

    config = handler_config_for_route(api, method, path) if api else None
    linked_ok = False
    if config is not None and table is not None:
        linked_ok = table_name in link_target_names(list(config.links))
    results.append(
        CheckResult(
            "table_linked",
            "functional",
            linked_ok,
            "table linked" if linked_ok else "table not linked",
        )
    )

    if semantic.get("table_linked", True):
        results.append(
            CheckResult(
                "semantic_table_linked",
                "semantic",
                linked_ok,
                "fix is linking the table" if linked_ok else "missing link",
            )
        )

    handler_unchanged = False
    if config is not None:
        entry_ok = handler_entrypoint(config) == expected_handler
        current = read_handler_source(project_dir, config)
        fixture_root = fixture_dir_for(project_dir)
        baseline = read_handler_source(fixture_root, config) if entry_ok else None
        handler_unchanged = (
            entry_ok and current is not None and baseline is not None and current == baseline
        )
    if semantic.get("handler_unchanged", True):
        results.append(
            CheckResult(
                "handler_unchanged",
                "semantic",
                handler_unchanged,
                "handler matches fixture" if handler_unchanged else "handler changed",
            )
        )

    preserved_ok = (
        api is not None
        and api.name == api_name
        and table is not None
        and table.name == table_name
        and config is not None
        and handler_entrypoint(config) == expected_handler
    )
    if semantic.get("components_preserved", True):
        results.append(
            CheckResult(
                "components_preserved",
                "semantic",
                preserved_ok,
                "api/table/handler names preserved"
                if preserved_ok
                else "component or handler renamed",
            )
        )

    functional_ids = {"route_exists", "table_exists", "table_linked"}
    functional_pass = all(r.passed for r in results if r.check_id in functional_ids)
    semantic_checks = {r.check_id: r.passed for r in results if r.family == "semantic"}

    return GradeResult(
        case_id="e2-missing-link",
        functional_pass=functional_pass,
        semantic_checks=semantic_checks,
        checks=results,
    )
