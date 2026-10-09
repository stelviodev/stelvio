"""Tests for stelvio.cli.init_command and _maybe_init_git_repo."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

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


# --- stlv init --template ---


def _invoke_init_template(cli, monkeypatch, tmp_path, template, checkout):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "stelvio_art", lambda console: None)
    monkeypatch.setattr(cli, "is_git_available", lambda: False)
    monkeypatch.setattr("stelvio.git._checkout_from_github", checkout)
    return CliRunner().invoke(cli.init, ["--template", template])


def test_init_template_copy_failure_exits_nonzero(cli, monkeypatch, tmp_path):
    def checkout(*args, **kwargs):
        raise RuntimeError("Git command failed: clone")

    result = _invoke_init_template(cli, monkeypatch, tmp_path, "gh:owner/repo", checkout)

    assert result.exit_code == 1
    assert "Error copying template" in result.output
    assert "Created stlv_app.py" not in result.output
    assert "Copied template" not in result.output
    assert list(tmp_path.iterdir()) == []


def test_init_template_without_stlv_app_exits_nonzero(cli, monkeypatch, tmp_path):
    def checkout(owner, repo, branch, subdirectory, destination):
        target = destination / (subdirectory if subdirectory else "")
        target.mkdir(parents=True, exist_ok=True)
        (target / "README.md").write_text("hi\n")
        return target

    result = _invoke_init_template(cli, monkeypatch, tmp_path, "gh:owner/repo", checkout)

    assert result.exit_code == 1
    assert "stlv_app.py" in result.output
    assert "Copied template" not in result.output
    assert not (tmp_path / "README.md").exists()


def test_init_template_with_stlv_app_exits_zero(cli, monkeypatch, tmp_path):
    seen_branch = {}

    def checkout(owner, repo, branch, subdirectory, destination):
        seen_branch["branch"] = branch
        target = destination / (subdirectory if subdirectory else "")
        target.mkdir(parents=True, exist_ok=True)
        (target / "stlv_app.py").write_text("app\n")
        return target

    result = _invoke_init_template(cli, monkeypatch, tmp_path, "gh:owner/repo", checkout)

    assert result.exit_code == 0
    assert seen_branch["branch"] is None
    assert "Copied template" in result.output
    assert "Created stlv_app.py" not in result.output
    assert "AWS profile" not in result.output
    assert (tmp_path / "stlv_app.py").read_text() == "app\n"


def test_init_template_invalid_selector_exits_nonzero(cli, monkeypatch, tmp_path):
    def checkout(*args, **kwargs):
        raise AssertionError("checkout should not run")

    result = _invoke_init_template(cli, monkeypatch, tmp_path, "gh:owner", checkout)

    assert result.exit_code == 1
    assert "Invalid template format" in result.output
    assert list(tmp_path.iterdir()) == []
