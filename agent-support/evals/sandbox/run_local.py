"""Minimal local sandbox loop for Stelvio development evals.

Copies a case fixture into a temp workspace, runs a maintainer-supplied agent
command under a scrubbed environment, grades the result, and writes JSON
outside the workspace. Not a general evaluation framework.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Literal

from graders.grade import grade_project, load_checks

Condition = Literal["baseline", "skill_invoked", "direct_context"]

_EVALS_ROOT = Path(__file__).resolve().parents[1]
_CASES_ROOT = _EVALS_ROOT / "cases"

# Environment allowlist for the agent process. Anything else is dropped.
_ENV_ALLOW_EXACT = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "TERM",
        "TMPDIR",
        "TMP",
        "TEMP",
        "LANG",
        "LANGUAGE",
        "TZ",
        "PWD",
    }
)
_ENV_ALLOW_PREFIXES = ("LC_",)

# Always strip these even if somehow allowlisted later.
_ENV_DENY_PREFIXES = (
    "AWS_",
    "PULUMI_",
    "STLV_",
    "STELVIO_",
)


@dataclass
class Footprint:
    files_changed: int
    lines_added: int
    lines_removed: int
    unrelated_files_changed: int


@dataclass
class RunResult:
    task_id: str
    agent: str | None
    model: str | None
    model_version: str | None
    harness: str | None
    condition: Condition
    attempt: int
    functional_pass: bool
    semantic_checks: dict[str, bool]
    observations: list[str] = field(default_factory=list)
    failure_tags: list[str] = field(default_factory=list)
    skill_loaded: bool | None = None
    references_loaded: bool | None = None
    files_changed: int | None = None
    lines_added: int | None = None
    lines_removed: int | None = None
    unrelated_files_changed: int | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    duration: float | None = None
    cost: float | None = None
    agent_exit_code: int | None = None
    timed_out: bool = False
    error: str | None = None
    result_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def cases_root() -> Path:
    return _CASES_ROOT


def resolve_case_dir(case_id: str, cases_dir: Path | None = None) -> Path:
    root = cases_dir or _CASES_ROOT
    case_dir = root / case_id
    if not case_dir.is_dir():
        raise FileNotFoundError(f"Unknown case {case_id!r}: {case_dir}")
    fixture = case_dir / "fixture"
    if not fixture.is_dir():
        raise FileNotFoundError(f"Case {case_id!r} has no fixture/ at {fixture}")
    return case_dir


def scrubbed_env(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """Build an allowlisted environment with AWS/Pulumi/Stelvio credentials removed."""
    src = dict(source if source is not None else os.environ)
    out = {
        key: value
        for key, value in src.items()
        if key in _ENV_ALLOW_EXACT or key.startswith(_ENV_ALLOW_PREFIXES)
    }
    for key in list(out):
        if key.startswith(_ENV_DENY_PREFIXES):
            del out[key]
    return out


def _text_file(path: Path) -> bool:
    try:
        path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return False
    return True


def _snapshot_files(root: Path) -> dict[str, list[str]]:
    """Relative path → list of lines for text files under root."""
    snap: dict[str, list[str]] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        if _text_file(path):
            snap[rel] = path.read_text(encoding="utf-8").splitlines()
        else:
            snap[rel] = []
    return snap


def change_footprint(
    before: Mapping[str, list[str]],
    after: Mapping[str, list[str]],
    *,
    unrelated_globs: Sequence[str] = (),
) -> Footprint:
    """Compare before/after file snapshots under the workspace."""
    all_keys = set(before) | set(after)
    files_changed = 0
    lines_added = 0
    lines_removed = 0
    unrelated = 0

    for key in all_keys:
        old = before.get(key)
        new = after.get(key)
        if old == new:
            continue
        files_changed += 1
        old_lines = old or []
        new_lines = new or []
        matcher = SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "insert":
                lines_added += j2 - j1
            elif tag == "delete":
                lines_removed += i2 - i1
            elif tag == "replace":
                lines_removed += i2 - i1
                lines_added += j2 - j1

        if _matches_any(key, unrelated_globs):
            unrelated += 1

    return Footprint(
        files_changed=files_changed,
        lines_added=lines_added,
        lines_removed=lines_removed,
        unrelated_files_changed=unrelated,
    )


def _matches_any(rel_path: str, patterns: Sequence[str]) -> bool:
    name = Path(rel_path).name
    return any(fnmatch(rel_path, p) or fnmatch(name, p) for p in patterns)


def _prepare_workspace(
    case_dir: Path,
    work_dir: Path,
    *,
    condition: Condition,
    skill_dir: Path | None,
    direct_context: str | None,
) -> tuple[bool | None, bool | None]:
    """Copy fixture + prompt into work_dir; optionally mount skill / direct context.

    Returns (skill_loaded, references_loaded).
    """
    shutil.copytree(case_dir / "fixture", work_dir, dirs_exist_ok=True)
    prompt_src = case_dir / "prompt.md"
    if prompt_src.is_file():
        shutil.copy2(prompt_src, work_dir / "prompt.md")

    skill_loaded: bool | None = None
    references_loaded: bool | None = None

    if condition == "baseline":
        skill_loaded = False
        references_loaded = False
        return skill_loaded, references_loaded

    if condition == "skill_invoked":
        instruction = (
            "Follow the stelvio-best-practices skill when solving this task.\n"
            "The skill is available under .agents/skills/stelvio-best-practices/.\n"
        )
        (work_dir / "AGENT_INSTRUCTION.txt").write_text(instruction, encoding="utf-8")
        if skill_dir is not None and skill_dir.is_dir():
            dest = work_dir / ".agents" / "skills" / "stelvio-best-practices"
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(skill_dir, dest, dirs_exist_ok=True)
            skill_loaded = True
            references_loaded = (dest / "references").is_dir()
        else:
            skill_loaded = False
            references_loaded = False
        return skill_loaded, references_loaded

    # direct_context
    skill_loaded = False
    references_loaded = False
    guidance = direct_context or (
        "Apply Stelvio best practices: prefer links over manual IAM, "
        "use Resources for linked names, subscribe/notify_* for events, "
        "and customize for escape hatches.\n"
    )
    (work_dir / "DIRECT_CONTEXT.md").write_text(guidance, encoding="utf-8")
    prompt_path = work_dir / "prompt.md"
    if prompt_path.is_file():
        prompt_path.write_text(
            prompt_path.read_text(encoding="utf-8") + "\n\n## Guidance\n\n" + guidance,
            encoding="utf-8",
        )
    return skill_loaded, references_loaded


def _ensure_grade_context() -> None:
    """Set a minimal AppContext so graders can construct components."""
    from stelvio.config import AwsConfig
    from stelvio.context import AppContext, _ContextStore
    from stelvio.provider import ProviderStore

    _ContextStore.clear()
    ProviderStore.reset()
    _ContextStore.set(
        AppContext(
            name="eval",
            env="eval",
            aws=AwsConfig(profile="default", region="us-east-1"),
            home="aws",
            customize={},
        )
    )


def run_case(
    case_id: str,
    agent_command: Sequence[str],
    *,
    condition: Condition = "baseline",
    attempt: int = 1,
    timeout_seconds: float = 300.0,
    results_dir: Path,
    cases_dir: Path | None = None,
    skill_dir: Path | None = None,
    direct_context: str | None = None,
    agent: str | None = None,
    model: str | None = None,
    model_version: str | None = None,
    harness: str | None = "run_local",
    env_source: Mapping[str, str] | None = None,
    keep_workspace: bool = False,
) -> RunResult:
    """Run one eval case end-to-end and write result JSON under ``results_dir``."""
    case_dir = resolve_case_dir(case_id, cases_dir)
    checks = load_checks(case_dir / "checks.toml")
    footprint_cfg = checks.get("footprint", {})
    unrelated_globs = list(footprint_cfg.get("unrelated_glob", []))

    results_dir = results_dir.resolve()
    results_dir.mkdir(parents=True, exist_ok=True)

    tmp = tempfile.mkdtemp(prefix=f"stelvio-eval-{case_id}-")
    work_dir = Path(tmp)
    try:
        results_dir.resolve().relative_to(work_dir.resolve())
    except ValueError:
        pass
    else:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise RuntimeError("results_dir must be outside the temporary agent workspace")

    timed_out = False
    agent_exit: int | None = None
    error: str | None = None
    duration: float | None = None
    skill_loaded: bool | None = None
    references_loaded: bool | None = None
    footprint = Footprint(0, 0, 0, 0)
    grade_functional = False
    grade_semantic: dict[str, bool] = {}
    observations: list[str] = []
    failure_tags: list[str] = []

    try:
        skill_loaded, references_loaded = _prepare_workspace(
            case_dir,
            work_dir,
            condition=condition,
            skill_dir=skill_dir,
            direct_context=direct_context,
        )
        before = _snapshot_files(work_dir)
        env = scrubbed_env(env_source)
        env["PWD"] = str(work_dir)

        started = time.monotonic()
        try:
            completed = subprocess.run(
                list(agent_command),
                cwd=work_dir,
                env=env,
                timeout=timeout_seconds,
                check=False,
                capture_output=True,
                text=True,
            )
            agent_exit = completed.returncode
            if completed.returncode != 0:
                observations.append("agent_nonzero_exit")
                if completed.stderr:
                    observations.append(f"agent_stderr:{completed.stderr[:500]}")
        except subprocess.TimeoutExpired:
            timed_out = True
            agent_exit = None
            failure_tags.append("HARNESS")
            observations.append("timed_out")
            error = f"agent timed out after {timeout_seconds}s"
        duration = time.monotonic() - started

        after = _snapshot_files(work_dir)
        footprint = change_footprint(before, after, unrelated_globs=unrelated_globs)

        _ensure_grade_context()
        grade = grade_project(work_dir, case_dir / "checks.toml")
        grade_functional = grade.functional_pass
        grade_semantic = dict(grade.semantic_checks)
        observations.extend(grade.observations)
        if grade.error:
            failure_tags.append("HARNESS")
            error = error or grade.error

    finally:
        if not keep_workspace:
            shutil.rmtree(work_dir, ignore_errors=True)

    result = RunResult(
        task_id=case_id,
        agent=agent,
        model=model,
        model_version=model_version,
        harness=harness,
        condition=condition,
        attempt=attempt,
        functional_pass=grade_functional if not timed_out else False,
        semantic_checks=grade_semantic,
        observations=observations,
        failure_tags=failure_tags,
        skill_loaded=skill_loaded,
        references_loaded=references_loaded,
        files_changed=footprint.files_changed,
        lines_added=footprint.lines_added,
        lines_removed=footprint.lines_removed,
        unrelated_files_changed=footprint.unrelated_files_changed,
        tokens_in=None,
        tokens_out=None,
        duration=duration,
        cost=None,
        agent_exit_code=agent_exit,
        timed_out=timed_out,
        error=error,
    )

    result_path = results_dir / f"{case_id}.{condition}.{attempt}.json"
    result.result_path = str(result_path)
    result_path.write_text(json.dumps(result.to_dict(), indent=2) + "\n", encoding="utf-8")
    return result


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", required=True, help="Case id, e.g. e1-linked-dynamodb")
    parser.add_argument(
        "--agent-command",
        required=True,
        help="Shell command to run as the agent (cwd = temp fixture copy)",
    )
    parser.add_argument(
        "--condition",
        choices=("baseline", "skill_invoked", "direct_context"),
        default="baseline",
    )
    parser.add_argument("--attempt", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument(
        "--results-dir",
        type=Path,
        required=True,
        help="Directory for result JSON (must be outside the temp workspace)",
    )
    parser.add_argument("--cases-dir", type=Path, default=None)
    parser.add_argument("--skill-dir", type=Path, default=None)
    parser.add_argument("--direct-context-file", type=Path, default=None)
    parser.add_argument("--agent", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--model-version", default=None)
    parser.add_argument("--harness", default="run_local")
    parser.add_argument("--keep-workspace", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    direct_context = None
    if args.direct_context_file is not None:
        direct_context = args.direct_context_file.read_text(encoding="utf-8")

    # Prefer shell form so maintainers can pass pipelines; still scrub env.
    command = ["/bin/sh", "-c", args.agent_command]
    result = run_case(
        args.case,
        command,
        condition=args.condition,
        attempt=args.attempt,
        timeout_seconds=args.timeout,
        results_dir=args.results_dir,
        cases_dir=args.cases_dir,
        skill_dir=args.skill_dir,
        direct_context=direct_context,
        agent=args.agent,
        model=args.model,
        model_version=args.model_version,
        harness=args.harness,
        keep_workspace=args.keep_workspace,
    )
    print(json.dumps(result.to_dict(), indent=2))
    if result.timed_out or result.error:
        return 1
    return 0 if result.agent_exit_code == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
