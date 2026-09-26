"""Grader for e1-linked-dynamodb."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from graders.inspect import (
    find_dynamo_table,
    find_http_api,
    handler_config_for_route,
    link_target_names,
    read_handler_source,
    route_matches,
    source_hardcodes_table_name,
    source_imports_resources,
    source_reads_dynamo_item,
    source_uses_resources_attr,
)
from graders.model import CheckResult, GradeResult


def grade_e1(project_dir: Path, checks: dict[str, Any]) -> GradeResult:
    functional = checks.get("functional", {})
    semantic = checks.get("semantic", {})
    results: list[CheckResult] = []
    observations: list[str] = []

    route_spec = functional.get("route", {})
    method = route_spec.get("method", "GET")
    path = route_spec.get("path", "/users/{id}")
    table_name = functional.get("table_name", "users")
    partition_key = functional.get("partition_key", "id")

    api = find_http_api()
    table = find_dynamo_table(table_name)

    route_ok = api is not None and route_matches(api, method, path)
    results.append(
        CheckResult(
            "route_exists",
            "functional",
            route_ok,
            f"{method} {path}" if route_ok else f"missing route {method} {path}",
        )
    )

    table_ok = table is not None
    results.append(
        CheckResult(
            "table_exists",
            "functional",
            table_ok,
            table_name if table_ok else f"missing DynamoTable {table_name!r}",
        )
    )

    pk_ok = table is not None and table.partition_key == partition_key
    results.append(
        CheckResult(
            "partition_key",
            "functional",
            pk_ok,
            partition_key if pk_ok else f"expected partition_key={partition_key!r}",
        )
    )

    handler_source = None
    config = None
    if api is not None:
        config = handler_config_for_route(api, method, path)
        if config is not None:
            handler_source = read_handler_source(project_dir, config)

    reads_ok = handler_source is not None and source_reads_dynamo_item(handler_source)
    results.append(
        CheckResult(
            "handler_reads_item",
            "functional",
            reads_ok,
            "handler calls get_item/GetItem" if reads_ok else "handler does not read an item",
        )
    )

    linked_ok = False
    if api is not None and table is not None and config is not None:
        linked_ok = table_name in link_target_names(list(config.links))
    if semantic.get("table_linked", True):
        results.append(
            CheckResult(
                "table_linked",
                "semantic",
                linked_ok,
                "table in route links" if linked_ok else "table not linked on route",
            )
        )

    resources_ok = False
    if handler_source is not None:
        resources_ok = source_imports_resources(handler_source) and source_uses_resources_attr(
            handler_source, table_name, "table_name"
        )
    if semantic.get("table_name_from_resources", True):
        results.append(
            CheckResult(
                "table_name_from_resources",
                "semantic",
                resources_ok,
                "Resources.<table>.table_name" if resources_ok else "name not from Resources",
            )
        )

    if (
        handler_source is not None
        and source_hardcodes_table_name(handler_source, table_name)
        and not resources_ok
    ):
        observations.append("hard-coded generated names")

    functional_ids = {"route_exists", "table_exists", "partition_key", "handler_reads_item"}
    functional_pass = all(r.passed for r in results if r.check_id in functional_ids)
    semantic_checks = {r.check_id: r.passed for r in results if r.family == "semantic"}

    return GradeResult(
        case_id="e1-linked-dynamodb",
        functional_pass=functional_pass,
        semantic_checks=semantic_checks,
        checks=results,
        observations=observations,
    )
