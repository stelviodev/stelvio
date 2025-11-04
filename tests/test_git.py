"""Tests for stelvio.git helper functions."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from stelvio import git

if TYPE_CHECKING:
    from pathlib import Path


def test_get_git_executable_returns_path(monkeypatch, tmp_path):
    fake_git = tmp_path / "git"
    fake_git.write_text("")
    monkeypatch.setattr("stelvio.git.shutil.which", lambda _: fake_git.as_posix())

    result = git._get_git_executable()

    assert result == fake_git.as_posix()


def test_get_git_executable_missing(monkeypatch):
    monkeypatch.setattr("stelvio.git.shutil.which", lambda _: None)

    with pytest.raises(RuntimeError, match="Git is not installed"):
        git._get_git_executable()


@pytest.mark.parametrize(
    ("value", "name"),
    [
        ("invalid owner", "owner"),
        ("repo$", "repo"),
        ("bad..branch", "branch"),
    ],
)
def test_validate_github_identifier_rejects_invalid(value, name):
    with pytest.raises(ValueError):  # noqa: PT011
        git._validate_github_identifier(value, name)


def test_validate_github_identifier_accepts_valid_values():
    for value, name in [("my-org", "owner"), ("repo_name", "repo"), ("feature/add", "branch")]:
        git._validate_github_identifier(value, name)


def test_validate_subdirectory_checks_for_traversal():
    with pytest.raises(ValueError, match="Subdirectory cannot contain '..'"):
        git._validate_subdirectory("../../secret")


def test_validate_subdirectory_accepts_valid_path():
    git._validate_subdirectory("src/app")


def test_run_git_command_executes_with_safe_arguments(tmp_path, monkeypatch):
    git_path = tmp_path / "git"
    git_path.write_text("")
    destination = tmp_path / "dest"

    calls: list[SimpleNamespace] = []

    def fake_run(cmd_list, check, shell, cwd, capture_output, text, timeout):  # noqa: PLR0913
        calls.append(SimpleNamespace(cmd=cmd_list, cwd=cwd))
        assert check is True
        assert shell is False
        assert capture_output is True
        assert text is True
        assert timeout == 300
        return subprocess.CompletedProcess(cmd_list, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    git._run_git_command(
        git_path.as_posix(),
        ["clone", "https://github.com/owner/repo.git", destination.as_posix()],
    )

    assert calls[0].cmd[0] == git_path.as_posix()
    assert calls[0].cmd[1] == "clone"


def test_run_git_command_rejects_unsafe_command(tmp_path):
    git_path = tmp_path / "git"
    git_path.write_text("")

    with pytest.raises(ValueError, match="Disallowed git command"):
        git._run_git_command(git_path.as_posix(), ["clone;rm"])


def test_run_git_command_rejects_non_string_argument(tmp_path):
    git_path = tmp_path / "git"
    git_path.write_text("")

    with pytest.raises(TypeError):
        git._run_git_command(git_path.as_posix(), ["clone", 123])


def test_checkout_from_github_with_subdirectory(tmp_path, monkeypatch):
    destination = tmp_path / "checkout"
    destination.mkdir()
    (destination / ".git").mkdir()

    run_calls: list[tuple[list[str], Path | None]] = []

    monkeypatch.setattr("stelvio.git._get_git_executable", lambda: "git")

    def fake_run(git_executable, args, cwd=None):
        run_calls.append((args, cwd))

    monkeypatch.setattr("stelvio.git._run_git_command", fake_run)

    result = git._checkout_from_github(
        owner="owner",
        repo="repo",
        branch="main",
        subdirectory="src/app",
        destination=destination,
    )

    expected_clone = [
        "clone",
        "--branch",
        "main",
        "https://github.com/owner/repo.git",
        destination.as_posix(),
        "--single-branch",
        "--depth",
        "1",
        "--filter=blob:none",
        "--sparse",
    ]

    assert run_calls[0] == (expected_clone, None)
    assert run_calls[1] == (["sparse-checkout", "init", "--cone"], destination)
    assert run_calls[2] == (["sparse-checkout", "add", "src/app"], destination)
    assert not (destination / ".git").exists()
    assert result == destination / "src/app"


def test_copy_from_github_copies_files(monkeypatch, tmp_path):
    created_files: list[Path] = []

    def fake_checkout(owner, repo, branch, subdirectory, destination):
        target = destination / (subdirectory if subdirectory else "")
        target.mkdir(parents=True, exist_ok=True)
        file_path = target / "example.txt"
        file_path.write_text("hello")
        created_files.append(file_path)
        return target

    monkeypatch.setattr("stelvio.git._checkout_from_github", fake_checkout)

    dest_path = tmp_path / "final"
    result = git.copy_from_github("owner", "repo", subdirectory="src", destination=dest_path)

    assert result == dest_path
    copied_file = dest_path / "example.txt"
    assert copied_file.read_text() == "hello"
    assert created_files[0].name == "example.txt"
