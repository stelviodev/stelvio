"""Fixed report schema; cleanup cannot rewrite acceptance history."""

from __future__ import annotations

import json
import shlex
from pathlib import Path

from .stages import STAGES
from .store import Store, events, redact, write_json


def generate(root: Path):
    data = Store(root).data
    records = events(root)
    failures = [
        r
        for r in records
        if r["kind"] in {"failure", "inventory_error", "damaged_event", "command_failure_output"}
        or r["kind"] == "test_result"
        and r["outcome"] == "failed"
    ]
    failures.extend(r for r in records if r["kind"] == "recovery" and r.get("outcome") == "failed")
    failures.extend(
        r for r in records if r["kind"] == "go_result" and r["result"].get("Action") == "fail"
    )
    skips = [r for r in records if r["kind"] == "test_result" and r["outcome"] == "skipped"]
    commands = [
        json.loads(p.read_text()) for p in sorted((root / "commands").glob("*/command.json"))
    ]
    complete = all(data["results"].get(s, {}).get("outcome") == "passed" for s in data["selected"])
    cleanup = data.get("audit", {}).get("outcome", "unknown")
    full = data["selected"] == list(STAGES)
    # Default Python unit collection deliberately skips opt-in tiers. Explicit
    # interop/native/AWS stages must not silently skip their selected test cases.
    implicit_skips = [s for s in skips if s.get("stage") != "python"]
    passed = complete and not failures and not implicit_skips and cleanup == "certified"
    status = ("passed" if full else "partial-passed") if passed else "failed-or-incomplete"
    report = {
        "schema": 1,
        "identity": {
            **data.get("identity", {}),
            "tool_version": data.get("tool_version"),
            "tool_fingerprint": data.get("tool_fingerprint"),
        },
        "tools": data.get("tools", {}),
        "configuration": data["configuration"],
        "artifacts": data.get("artifacts", {}),
        "plan": data["selected"],
        "results": data["results"],
        "commands": commands,
        "failures": failures,
        "test_results": [r for r in records if r["kind"] == "test_result"],
        "skips": skips,
        "recovery": data.get("recovery", []),
        "audit": data.get("audit", {}),
        "audit_history": data.get("audit_history", []),
        "outcome": status,
        "exclusions": ["Traforo release downloads", "downloaded-release upgrade"]
        + [f"stage: {name}" for name in STAGES if name not in data["selected"]],
        "limitations": [
            "No automatic fixes or test retries",
            "Intel networking not inferred",
            "Root causes are limited to captured evidence",
        ],
    }
    write_json(root / "report.json", report)
    lines = [
        "# Traforo acceptance report",
        "",
        f"Outcome: **{status}**",
        "",
        "## 1. Candidate and environment",
        "",
        "```json",
        json.dumps(
            {k: report[k] for k in ("identity", "tools", "configuration", "artifacts")},
            indent=2,
            sort_keys=True,
        ),
        "```",
        "",
        "## 2. Stages and commands",
        "",
        "| Stage | Outcome | Runtime (s) |",
        "|---|---|---|",
    ]
    for name in data["selected"]:
        result = data["results"].get(name, {})
        lines.append(
            f"| {name} | {result.get('outcome', 'not-run')} | {result.get('runtime', '')} |"
        )
    if report["test_results"]:
        lines.extend(
            [
                "",
                "### Test phases",
                "",
                "| Stage | Case | Phase | Outcome | Runtime (s) |",
                "|---|---|---|---|---|",
            ]
        )
        for case in report["test_results"]:
            lines.append(
                f"| {case.get('stage', '')} | {case.get('nodeid', 'unknown')} | "
                f"{case.get('phase', 'unknown')} | "
                f"{case['outcome']} | {case.get('duration', '')} |"
            )
    for command in commands:
        lines.extend(
            [
                "",
                f"### {command['stage']}",
                "",
                f"Directory: `{command['cwd']}`",
                "",
                "```sh",
                shlex.join(command["argv"]),
                "```",
                "",
                f"Environment: `{json.dumps(command['environment'], sort_keys=True)}`",
                f"Exit: {command.get('exit_code', 'incomplete')}; "
                f"runtime: {command.get('runtime_seconds', 'incomplete')}s.",
            ]
        )
    lines.extend(
        [
            "",
            "## 3. Failures and reproduction",
            "",
            "Re-run the recorded command in its recorded directory with the listed environment.",
            "Credentials must be supplied separately. Do not rerun live commands before cleanup.",
            "",
            "```json",
            json.dumps(failures, indent=2, sort_keys=True),
            "```",
            "",
            "## 4. Recovery",
            "",
            "```json",
            json.dumps(report["recovery"], indent=2, sort_keys=True),
            "```",
            "",
            "## 5. Independent audit and limits",
            "",
            "```json",
            json.dumps(report["audit"], indent=2, sort_keys=True),
            "```",
            "",
            "Excluded: " + ", ".join(report["exclusions"]) + ".",
            "A partial run is not full acceptance. Cleanup success does not erase failures.",
            "See events.jsonl, per-command output.log, JUnit XML, and case traces for evidence.",
        ]
    )
    (root / "report.md").write_text(redact("\n".join(lines)) + "\n")
    (root / "report.md").chmod(0o600)
    return report
