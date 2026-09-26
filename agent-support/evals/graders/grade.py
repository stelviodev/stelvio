"""Grade a Stelvio project directory against a case ``checks.toml``."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from graders.e1 import grade_e1
from graders.e2 import grade_e2
from graders.e3 import grade_e3
from graders.e4 import grade_e4
from graders.e5 import grade_e5
from graders.e6 import grade_e6
from graders.e7 import grade_e7
from graders.e8 import grade_e8
from graders.load import load_and_run_app
from graders.model import GradeResult

_GRADERS = {
    "e1-linked-dynamodb": grade_e1,
    "e2-missing-link": grade_e2,
    "e3-async-queue": grade_e3,
    "e4-s3-notify-function": grade_e4,
    "e5-s3-notify-queue": grade_e5,
    "e6-fanout-topic": grade_e6,
    "e7-queue-customize-kms": grade_e7,
    "e8-minimal-modification": grade_e8,
}


def load_checks(checks_path: Path) -> dict[str, Any]:
    with checks_path.open("rb") as f:
        return tomllib.load(f)


def grade_project(project_dir: Path, checks_path: Path | None = None) -> GradeResult:
    project_dir = project_dir.resolve()
    checks_file = checks_path or (project_dir.parent / "checks.toml")
    if not checks_file.is_file():
        # solutions live under cases/<id>/solutions/<name>/ — checks at case root
        checks_file = project_dir.parent.parent / "checks.toml"
    checks = load_checks(checks_file)
    meta = checks.get("meta", {})
    case_id = meta.get("id")
    if not case_id:
        return GradeResult(
            case_id="unknown",
            functional_pass=False,
            error="checks.toml missing meta.id",
        )

    grader = _GRADERS.get(case_id)
    if grader is None:
        return GradeResult(
            case_id=case_id,
            functional_pass=False,
            error=f"No grader registered for case {case_id!r}",
        )

    try:
        load_and_run_app(project_dir)
    except Exception as exc:
        return GradeResult(
            case_id=case_id,
            functional_pass=False,
            error=f"Failed to load app: {exc}",
            observations=["HARNESS"],
        )

    return grader(project_dir, checks)
