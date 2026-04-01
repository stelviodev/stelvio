"""Tests for stelvio.cli.init_command and _maybe_init_git_repo."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from stelvio.cli.init_command import DEFAULT_GITIGNORE, create_default_gitignore

# --- create_default_gitignore ---


def test_create_default_gitignore_creates_file(tmp_path):
    result = create_default_gitignore(tmp_path)

    assert result is True
    gitignore = tmp_path / ".gitignore"
    assert gitignore.exists()
    content = gitignore.read_text(encoding="utf-8")
    assert content == DEFAULT_GITIGNORE


def test_create_default_gitignore_includes_key_entries(tmp_path):
    create_default_gitignore(tmp_path)

    content = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert "__pycache__/" in content
    assert ".stelvio/" in content
    assert ".pulumi/" in content
    assert ".venv/" in content


def test_create_default_gitignore_skips_existing(tmp_path):
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text("# custom\n", encoding="utf-8")

    result = create_default_gitignore(tmp_path)

    assert result is False
    assert gitignore.read_text(encoding="utf-8") == "# custom\n"


# --- _maybe_init_git_repo ---


@pytest.fixture
def git_mocks(monkeypatch, tmp_path):
    """Set up monkeypatches for _maybe_init_git_repo dependencies."""
    monkeypatch.chdir(tmp_path)

    monkeypatch.setattr("stelvio.cli.is_git_available", lambda: True)
    monkeypatch.setattr("stelvio.cli.is_git_repo", lambda _: False)
    monkeypatch.setattr("stelvio.cli.is_git_submodule", lambda _: False)
    monkeypatch.setattr("stelvio.cli.find_parent_git_repo", lambda _: None)
    monkeypatch.setattr("stelvio.cli.click.confirm", lambda *a, **kw: True)

    mock_init = MagicMock()
    monkeypatch.setattr("stelvio.cli.init_git_repo", mock_init)

    mock_gitignore = MagicMock(return_value=True)
    monkeypatch.setattr("stelvio.cli.create_default_gitignore", mock_gitignore)

    mock_console = MagicMock()
    monkeypatch.setattr("stelvio.cli.console", mock_console)

    class Mocks:
        init = mock_init
        gitignore = mock_gitignore
        console = mock_console
        cwd = tmp_path

    return Mocks()


def _call_maybe_init():
    from stelvio.cli import _maybe_init_git_repo

    _maybe_init_git_repo()


def test_maybe_init_skips_when_git_not_available(git_mocks, monkeypatch):
    monkeypatch.setattr("stelvio.cli.is_git_available", lambda: False)

    _call_maybe_init()

    git_mocks.init.assert_not_called()


def test_maybe_init_skips_when_already_repo(git_mocks, monkeypatch):
    monkeypatch.setattr("stelvio.cli.is_git_repo", lambda _: True)

    _call_maybe_init()

    git_mocks.init.assert_not_called()


def test_maybe_init_warns_submodule(git_mocks, monkeypatch):
    monkeypatch.setattr("stelvio.cli.is_git_submodule", lambda _: True)

    _call_maybe_init()

    printed = " ".join(str(c) for c in git_mocks.console.print.call_args_list)
    assert "submodule" in printed


def test_maybe_init_warns_parent_repo(git_mocks, monkeypatch):
    parent = Path("/some/parent")
    monkeypatch.setattr("stelvio.cli.find_parent_git_repo", lambda _: parent)

    _call_maybe_init()

    printed = " ".join(str(c) for c in git_mocks.console.print.call_args_list)
    assert "parent directory" in printed
    assert str(parent) in printed


def test_maybe_init_skips_when_user_declines(git_mocks, monkeypatch):
    monkeypatch.setattr("stelvio.cli.click.confirm", lambda *a, **kw: False)

    _call_maybe_init()

    git_mocks.init.assert_not_called()


def test_maybe_init_success_with_gitignore(git_mocks):
    _call_maybe_init()

    git_mocks.init.assert_called_once_with(git_mocks.cwd)
    git_mocks.gitignore.assert_called_once_with(git_mocks.cwd)
    printed = " ".join(str(c) for c in git_mocks.console.print.call_args_list)
    assert "Initialized git repository" in printed
    assert "Created .gitignore" in printed


def test_maybe_init_success_gitignore_exists(git_mocks):
    git_mocks.gitignore.return_value = False

    _call_maybe_init()

    git_mocks.init.assert_called_once()
    printed = " ".join(str(c) for c in git_mocks.console.print.call_args_list)
    assert "Initialized git repository" in printed
    assert "Created .gitignore" not in printed


def test_maybe_init_handles_init_failure(git_mocks):
    git_mocks.init.side_effect = RuntimeError("boom")

    _call_maybe_init()

    printed = " ".join(str(c) for c in git_mocks.console.print.call_args_list)
    assert "Could not initialize git repository" in printed
    git_mocks.gitignore.assert_not_called()


def test_maybe_init_handles_gitignore_failure(git_mocks):
    git_mocks.gitignore.side_effect = OSError("disk full")

    _call_maybe_init()

    printed = " ".join(str(c) for c in git_mocks.console.print.call_args_list)
    assert "Initialized git repository" in printed
    assert "Could not create .gitignore" in printed
