"""Tests for stlv agents install / update."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from stelvio.cli.agents_command import (
    _destination_kinds,
    _install_or_update,
    _skills_dir_for_kind,
    agents,
    get_skills_root,
)


@pytest.fixture
def skills_tree(tmp_path: Path) -> Path:
    root = tmp_path / "skills"
    skill = root / "stelvio-best-practices"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# skill\n", encoding="utf-8")
    refs = skill / "references"
    refs.mkdir()
    (refs / "note.md").write_text("note\n", encoding="utf-8")
    return root


@pytest.fixture
def project_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, skills_tree: Path):
    project = tmp_path / "project"
    home = tmp_path / "home"
    project.mkdir()
    home.mkdir()
    monkeypatch.chdir(project)
    monkeypatch.setattr("stelvio.cli.agents_command._skills_root_override", skills_tree)
    monkeypatch.setattr(Path, "home", lambda: home)
    return project, home, skills_tree


def test_destination_kinds_shared_agents_once() -> None:
    assert _destination_kinds(["cursor", "codex", "opencode"]) == ["agents"]
    assert _destination_kinds(["claude"]) == ["claude"]
    assert _destination_kinds(["cursor", "claude"]) == ["agents", "claude"]


def test_skills_dir_mapping(tmp_path: Path) -> None:
    assert _skills_dir_for_kind("agents", base=tmp_path) == tmp_path / ".agents" / "skills"
    assert _skills_dir_for_kind("claude", base=tmp_path) == tmp_path / ".claude" / "skills"


def test_install_writes_shared_agents_once(
    project_home: tuple[Path, Path, Path],
) -> None:
    project, _home, _skills = project_home
    runner = CliRunner()
    result = runner.invoke(agents, ["install", "--target", "cursor,codex,opencode"])
    assert result.exit_code == 0, result.output

    skill_dir = project / ".agents" / "skills" / "stelvio-best-practices"
    assert (skill_dir / "SKILL.md").read_text(encoding="utf-8") == "# skill\n"
    assert (skill_dir / "references" / "note.md").is_file()
    assert not (project / ".claude").exists()

    manifest = json.loads((project / ".stelvio" / "agents.json").read_text(encoding="utf-8"))
    assert set(manifest["targets"]) == {"cursor", "codex", "opencode"}
    assert manifest["skills"] == ["stelvio-best-practices"]
    assert ".agents/skills/stelvio-best-practices/SKILL.md" in manifest["files"]
    assert ".agents/skills/stelvio-best-practices/references/note.md" in manifest["files"]
    assert manifest["scope"] == "project"


def test_install_cursor_and_claude_separate_trees(
    project_home: tuple[Path, Path, Path],
) -> None:
    project, _home, _skills = project_home
    runner = CliRunner()
    result = runner.invoke(agents, ["install", "--target", "cursor", "--target", "claude"])
    assert result.exit_code == 0, result.output

    agents_skill = project / ".agents" / "skills" / "stelvio-best-practices" / "SKILL.md"
    claude_skill = project / ".claude" / "skills" / "stelvio-best-practices" / "SKILL.md"
    assert agents_skill.is_file()
    assert claude_skill.is_file()
    assert agents_skill.read_text(encoding="utf-8") == claude_skill.read_text(encoding="utf-8")

    manifest = json.loads((project / ".stelvio" / "agents.json").read_text(encoding="utf-8"))
    assert set(manifest["targets"]) == {"cursor", "claude"}
    assert ".claude/skills/stelvio-best-practices/SKILL.md" in manifest["files"]


def test_global_install_paths(project_home: tuple[Path, Path, Path]) -> None:
    project, home, _skills = project_home
    runner = CliRunner()
    result = runner.invoke(agents, ["install", "--global", "--target", "cursor,claude"])
    assert result.exit_code == 0, result.output

    assert (home / ".agents" / "skills" / "stelvio-best-practices" / "SKILL.md").is_file()
    assert (home / ".claude" / "skills" / "stelvio-best-practices" / "SKILL.md").is_file()
    manifest = json.loads((home / ".stelvio" / "agents.json").read_text(encoding="utf-8"))
    assert manifest["scope"] == "global"
    assert not (project / ".agents").exists()
    assert not (project / ".stelvio" / "agents.json").exists()


def test_update_refuses_dirty_hashes_without_force(
    project_home: tuple[Path, Path, Path],
) -> None:
    project, _home, _skills = project_home
    runner = CliRunner()
    assert runner.invoke(agents, ["install", "--target", "cursor"]).exit_code == 0

    skill = project / ".agents" / "skills" / "stelvio-best-practices" / "SKILL.md"
    skill.write_text("# dirty\n", encoding="utf-8")

    result = runner.invoke(agents, ["update", "--target", "cursor"])
    assert result.exit_code == 2
    assert "modified" in result.output.lower() or "Refusing" in result.output
    assert skill.read_text(encoding="utf-8") == "# dirty\n"


def test_update_force_overwrites_dirty(
    project_home: tuple[Path, Path, Path],
) -> None:
    project, _home, skills = project_home
    runner = CliRunner()
    assert runner.invoke(agents, ["install", "--target", "cursor"]).exit_code == 0

    skill = project / ".agents" / "skills" / "stelvio-best-practices" / "SKILL.md"
    skill.write_text("# dirty\n", encoding="utf-8")
    (skills / "stelvio-best-practices" / "SKILL.md").write_text("# fresh\n", encoding="utf-8")

    result = runner.invoke(agents, ["update", "--target", "cursor", "--force"])
    assert result.exit_code == 0, result.output
    assert skill.read_text(encoding="utf-8") == "# fresh\n"


def test_update_without_manifest_fails(project_home: tuple[Path, Path, Path]) -> None:
    runner = CliRunner()
    result = runner.invoke(agents, ["update", "--target", "claude"])
    assert result.exit_code == 2
    assert "install" in result.output.lower()


def test_install_requires_target(project_home: tuple[Path, Path, Path]) -> None:
    runner = CliRunner()
    result = runner.invoke(agents, ["install"])
    assert result.exit_code != 0


def test_get_skills_root_override(skills_tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("stelvio.cli.agents_command._skills_root_override", skills_tree)
    assert get_skills_root() == skills_tree


def test_install_or_update_direct_api(tmp_path: Path, skills_tree: Path) -> None:
    project = tmp_path / "proj"
    home = tmp_path / "home"
    project.mkdir()
    home.mkdir()
    _install_or_update(
        targets=["codex"],
        global_scope=False,
        force=False,
        update=False,
        cwd=project,
        home=home,
        skills_root=skills_tree,
    )
    assert (project / ".agents" / "skills" / "stelvio-best-practices" / "SKILL.md").is_file()
