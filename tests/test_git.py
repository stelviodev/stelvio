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
    with pytest.raises(ValueError, match=r"Subdirectory cannot contain '\.\.'"):
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


# --- is_git_available ---


def test_is_git_available_true(monkeypatch):
    monkeypatch.setattr("stelvio.git.shutil.which", lambda _: "/usr/bin/git")
    assert git.is_git_available() is True


def test_is_git_available_false(monkeypatch):
    monkeypatch.setattr("stelvio.git.shutil.which", lambda _: None)
    assert git.is_git_available() is False


# --- is_git_repo ---


def test_is_git_repo_true_with_git_dir(tmp_path):
    (tmp_path / ".git").mkdir()
    assert git.is_git_repo(tmp_path) is True


def test_is_git_repo_false_without_git_dir(tmp_path):
    assert git.is_git_repo(tmp_path) is False


def test_is_git_repo_false_with_git_file(tmp_path):
    (tmp_path / ".git").write_text("gitdir: ../somewhere")
    assert git.is_git_repo(tmp_path) is False


def test_is_git_repo_true_with_symlinked_git_dir(tmp_path):
    real_git = tmp_path / "real_git"
    real_git.mkdir()
    (tmp_path / "project").mkdir()
    (tmp_path / "project" / ".git").symlink_to(real_git)
    assert git.is_git_repo(tmp_path / "project") is True


# --- is_git_submodule ---


def test_is_git_submodule_true(tmp_path):
    (tmp_path / ".git").write_text("gitdir: ../.git/modules/sub")
    assert git.is_git_submodule(tmp_path) is True


def test_is_git_submodule_false_with_dir(tmp_path):
    (tmp_path / ".git").mkdir()
    assert git.is_git_submodule(tmp_path) is False


def test_is_git_submodule_false_no_git(tmp_path):
    assert git.is_git_submodule(tmp_path) is False


def test_is_git_submodule_false_git_file_no_gitdir(tmp_path):
    (tmp_path / ".git").write_text("something else entirely")
    assert git.is_git_submodule(tmp_path) is False


# --- find_parent_git_repo ---


def test_find_parent_git_repo_found(tmp_path):
    (tmp_path / ".git").mkdir()
    child = tmp_path / "sub" / "child"
    child.mkdir(parents=True)
    assert git.find_parent_git_repo(child) == tmp_path


def test_find_parent_git_repo_none(tmp_path):
    child = tmp_path / "sub"
    child.mkdir()
    assert git.find_parent_git_repo(child) is None


def test_find_parent_git_repo_skips_self(tmp_path):
    (tmp_path / ".git").mkdir()
    assert git.find_parent_git_repo(tmp_path) is None


# --- init_git_repo ---


def test_init_git_repo_calls_git_init(tmp_path, monkeypatch):
    git_path = tmp_path / "git"
    git_path.write_text("")
    monkeypatch.setattr("stelvio.git._get_git_executable", lambda: git_path.as_posix())

    calls: list[SimpleNamespace] = []

    def fake_run(git_executable, args, cwd=None):
        calls.append(SimpleNamespace(exe=git_executable, args=args, cwd=cwd))

    monkeypatch.setattr("stelvio.git._run_git_command", fake_run)

    git.init_git_repo(tmp_path)

    assert len(calls) == 1
    assert calls[0].exe == git_path.as_posix()
    assert calls[0].args == ["init"]
    assert calls[0].cwd == tmp_path


def test_init_git_repo_propagates_error(tmp_path, monkeypatch):
    git_path = tmp_path / "git"
    git_path.write_text("")
    monkeypatch.setattr("stelvio.git._get_git_executable", lambda: git_path.as_posix())

    def fake_run(git_executable, args, cwd=None):
        raise RuntimeError("Git command failed")

    monkeypatch.setattr("stelvio.git._run_git_command", fake_run)

    with pytest.raises(RuntimeError, match="Git command failed"):
        git.init_git_repo(tmp_path)
