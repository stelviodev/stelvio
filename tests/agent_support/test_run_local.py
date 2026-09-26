"""Tests for the local eval sandbox loop."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from pytest import raises
from sandbox.run_local import (
    change_footprint,
    run_case,
    scrubbed_env,
)

CASES = Path(__file__).resolve().parents[2] / "agent-support" / "evals" / "cases"
E1 = CASES / "e1-linked-dynamodb"
E1_GOOD = E1 / "solutions" / "good"


def test_scrubbed_env_drops_aws_pulumi_stelvio_credentials():
    source = {
        "PATH": "/usr/bin",
        "HOME": "/home/eval",
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
        "AWS_ACCESS_KEY_ID": "AKIA...",
        "AWS_SECRET_ACCESS_KEY": "secret",
        "AWS_PROFILE": "prod",
        "AWS_REGION": "us-east-1",
        "PULUMI_ACCESS_TOKEN": "pulumi-token",
        "PULUMI_CONFIG_PASSPHRASE": "pass",
        "STLV_SOMETHING": "x",
        "STELVIO_TOKEN": "y",
        "UNRELATED_SECRET": "should-drop",
    }
    env = scrubbed_env(source)

    assert env["PATH"] == "/usr/bin"
    assert env["HOME"] == "/home/eval"
    assert env["LANG"] == "en_US.UTF-8"
    assert env["LC_ALL"] == "en_US.UTF-8"
    assert "AWS_ACCESS_KEY_ID" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert "AWS_PROFILE" not in env
    assert "AWS_REGION" not in env
    assert "PULUMI_ACCESS_TOKEN" not in env
    assert "PULUMI_CONFIG_PASSPHRASE" not in env
    assert "STLV_SOMETHING" not in env
    assert "STELVIO_TOKEN" not in env
    assert "UNRELATED_SECRET" not in env


def test_change_footprint_counts_edits():
    before = {"a.py": ["one", "two"], "keep.py": ["same"]}
    after = {"a.py": ["one", "two", "three"], "keep.py": ["same"], "new.py": ["x"]}
    fp = change_footprint(before, after, unrelated_globs=["new.py"])
    assert fp.files_changed == 2
    assert fp.lines_added >= 1
    assert fp.unrelated_files_changed == 1


def _copy_good_solution_command() -> list[str]:
    """Agent stand-in: replace the workspace with the known-good e1 solution."""
    skip = {".agents", "prompt.md", "AGENT_INSTRUCTION.txt", "DIRECT_CONTEXT.md"}
    return [
        sys.executable,
        "-c",
        (
            "import shutil, pathlib\n"
            f"src = pathlib.Path({str(E1_GOOD)!r})\n"
            "dst = pathlib.Path.cwd()\n"
            f"skip = {skip!r}\n"
            "for p in list(dst.iterdir()):\n"
            "    if p.name in skip:\n"
            "        continue\n"
            "    if p.is_dir():\n"
            "        shutil.rmtree(p)\n"
            "    else:\n"
            "        p.unlink()\n"
            "for p in src.iterdir():\n"
            "    target = dst / p.name\n"
            "    if p.is_dir():\n"
            "        shutil.copytree(p, target)\n"
            "    else:\n"
            "        shutil.copy2(p, target)\n"
        ),
    ]


def test_run_case_writes_result_outside_workspace_and_grades(tmp_path, app_context):
    results_dir = tmp_path / "results"
    poisoned = {
        **os.environ,
        "AWS_ACCESS_KEY_ID": "AKIA_SHOULD_NOT_LEAK",
        "AWS_SECRET_ACCESS_KEY": "secret",
        "PULUMI_ACCESS_TOKEN": "tok",
    }
    result = run_case(
        "e1-linked-dynamodb",
        _copy_good_solution_command(),
        condition="baseline",
        attempt=1,
        timeout_seconds=30,
        results_dir=results_dir,
        cases_dir=CASES,
        env_source=poisoned,
        agent="test-agent",
        model="test-model",
    )

    assert result.result_path is not None
    result_path = Path(result.result_path)
    assert result_path.is_file()
    assert result_path.parent == results_dir.resolve()
    assert result.functional_pass is True
    assert result.semantic_checks["table_linked"] is True
    assert result.skill_loaded is False
    assert result.timed_out is False
    assert result.files_changed is not None
    assert result.files_changed >= 1

    payload = json.loads(result_path.read_text(encoding="utf-8"))
    assert payload["task_id"] == "e1-linked-dynamodb"
    assert payload["condition"] == "baseline"
    assert payload["attempt"] == 1
    assert payload["functional_pass"] is True
    assert "AWS_ACCESS_KEY_ID" not in json.dumps(payload)


def test_run_case_agent_env_dump_has_no_secrets(tmp_path, app_context):
    results_dir = tmp_path / "results"
    dump_path = results_dir / "agent_env.json"
    results_dir.mkdir()
    dump_cmd = [
        sys.executable,
        "-c",
        (
            "import json, os, pathlib\n"
            f"out = pathlib.Path({str(dump_path)!r})\n"
            "out.write_text(json.dumps(dict(os.environ), indent=2))\n"
        ),
    ]
    poisoned = {
        "PATH": os.environ.get("PATH", "/usr/bin"),
        "HOME": str(tmp_path / "home"),
        "AWS_ACCESS_KEY_ID": "AKIA_LEAK",
        "AWS_SECRET_ACCESS_KEY": "secret",
        "PULUMI_ACCESS_TOKEN": "pulumi",
        "STLV_FOO": "bar",
        "STELVIO_TOKEN": "tok",
    }
    run_case(
        "e1-linked-dynamodb",
        dump_cmd,
        condition="baseline",
        attempt=2,
        timeout_seconds=15,
        results_dir=results_dir,
        cases_dir=CASES,
        env_source=poisoned,
    )
    agent_env = json.loads(dump_path.read_text(encoding="utf-8"))
    for key in agent_env:
        assert not key.startswith("AWS_"), key
        assert not key.startswith("PULUMI_"), key
        assert not key.startswith("STLV_"), key
        assert not key.startswith("STELVIO_"), key


def test_run_case_timeout(tmp_path, app_context):
    results_dir = tmp_path / "results"
    hang = [sys.executable, "-c", "import time; time.sleep(30)"]
    result = run_case(
        "e1-linked-dynamodb",
        hang,
        condition="baseline",
        attempt=1,
        timeout_seconds=0.5,
        results_dir=results_dir,
        cases_dir=CASES,
        env_source={"PATH": os.environ.get("PATH", "/usr/bin"), "HOME": str(tmp_path)},
    )
    assert result.timed_out is True
    assert result.functional_pass is False
    assert "HARNESS" in result.failure_tags
    assert Path(result.result_path).is_file()


def test_run_case_skill_condition_mounts_skill(tmp_path, app_context):
    results_dir = tmp_path / "results"
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# practices\n", encoding="utf-8")
    (skill_dir / "references").mkdir()
    (skill_dir / "references" / "links.md").write_text("link things\n", encoding="utf-8")

    seen = results_dir / "seen.json"
    results_dir.mkdir()
    script = (
        "import json, pathlib\n"
        "root = pathlib.Path.cwd()\n"
        "skill = root / '.agents' / 'skills' / 'stelvio-best-practices' / 'SKILL.md'\n"
        "instr = root / 'AGENT_INSTRUCTION.txt'\n"
        f"out = pathlib.Path({str(seen)!r})\n"
        "out.write_text(json.dumps({\n"
        "  'skill_exists': skill.is_file(),\n"
        "  'instruction_exists': instr.is_file(),\n"
        "}))\n"
    )
    result = run_case(
        "e1-linked-dynamodb",
        [sys.executable, "-c", script],
        condition="skill_invoked",
        attempt=1,
        timeout_seconds=15,
        results_dir=results_dir,
        cases_dir=CASES,
        skill_dir=skill_dir,
        env_source={"PATH": os.environ.get("PATH", "/usr/bin"), "HOME": str(tmp_path)},
    )
    assert result.agent_exit_code == 0, result.observations
    assert result.skill_loaded is True
    assert result.references_loaded is True
    payload = json.loads(seen.read_text(encoding="utf-8"))
    assert payload["skill_exists"] is True
    assert payload["instruction_exists"] is True


def test_resolve_unknown_case_raises(tmp_path):
    with raises(FileNotFoundError):
        run_case(
            "no-such-case",
            [sys.executable, "-c", "pass"],
            results_dir=tmp_path / "out",
            cases_dir=CASES,
            timeout_seconds=5,
            env_source={"PATH": "/usr/bin", "HOME": str(tmp_path)},
        )
