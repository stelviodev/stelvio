from pathlib import Path

import click
import pytest
from click.testing import CliRunner

SHELL_FILES = {
    "bash": (".local/share/bash-completion/completions", ("stlv", "stelvio")),
    "zsh": (".zsh/completions", ("_stlv", "_stelvio")),
    "fish": (".config/fish/completions", ("stlv.fish", "stelvio.fish")),
    "powershell": (".config/powershell/completions", ("stlv.ps1", "stelvio.ps1")),
}


@pytest.fixture
def completion_module(cli):
    from stelvio.cli import completion

    return completion


@pytest.mark.parametrize("shell", SHELL_FILES)
def test_install_completion_writes_click_scripts_for_both_executables(
    cli, shell: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    result = CliRunner().invoke(
        cli.cli, ["install-completion", "--shell", shell, "--no-edit-profile"]
    )

    assert result.exit_code == 0, result.output
    relative_directory, filenames = SHELL_FILES[shell]
    output_directory = tmp_path / relative_directory
    scripts = [output_directory / filename for filename in filenames]
    assert all(script.is_file() for script in scripts)
    assert all(script.stat().st_size > 0 for script in scripts)
    assert "Installed completion:" in result.output

    first_script, second_script = (script.read_text() for script in scripts)
    assert "stlv" in first_script
    assert "_STLV_COMPLETE" in first_script
    assert "stelvio" in second_script
    assert "_STELVIO_COMPLETE" in second_script


@pytest.mark.parametrize("shell", SHELL_FILES)
@pytest.mark.parametrize("executable", ["stlv", "stelvio"])
def test_click_completion_includes_nested_commands_and_options(
    cli, completion_module, shell: str, executable: str
) -> None:
    completion_class = completion_module.get_completion_class(shell)
    shell_completion = completion_class(
        cli.cli,
        {},
        prog_name=executable,
        complete_var=f"_{executable.upper()}_COMPLETE",
    )

    state_commands = shell_completion.get_completions(["state"], "")
    diff_options = shell_completion.get_completions(["diff"], "--")

    assert {item.value for item in state_commands} >= {"list", "rm", "repair"}
    assert {item.value for item in diff_options} >= {"--json", "--compact", "--show-unchanged"}


def test_shell_detection_prefers_parent_over_login_shell(
    completion_module, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(completion_module, "_get_parent_process_name", lambda: "/bin/zsh")

    assert completion_module._detect_shell({"SHELL": "/bin/bash"}) == "zsh"


def test_shell_detection_uses_environment_marker_before_shell_fallback(
    completion_module, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(completion_module, "_get_parent_process_name", lambda: "python3")

    assert completion_module._detect_shell({"ZSH_VERSION": "5.9", "SHELL": "/bin/bash"}) == "zsh"


def test_shell_detection_returns_ambiguous_for_conflicting_current_signals(
    completion_module, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(completion_module, "_get_parent_process_name", lambda: "fish")

    assert completion_module._detect_shell({"ZSH_VERSION": "5.9", "SHELL": "/bin/fish"}) is None


def test_shell_detection_uses_shell_environment_as_fallback(
    completion_module, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(completion_module, "_get_parent_process_name", lambda: "python3")

    assert completion_module._detect_shell({"SHELL": "/bin/bash"}) == "bash"


def test_ambiguous_shell_prompts_interactively(
    completion_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(completion_module, "_detect_shell", lambda: None)
    monkeypatch.setattr(click, "prompt", lambda *args, **kwargs: "fish")

    assert completion_module._select_shell(None, interactive=True) == "fish"


def test_ambiguous_shell_requires_shell_in_noninteractive_use(
    completion_module, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(completion_module, "_detect_shell", lambda: None)

    with pytest.raises(click.UsageError, match="Pass --shell"):
        completion_module._select_shell(None, interactive=False)


def test_install_fails_before_writing_when_profile_choice_is_missing(
    cli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    result = CliRunner().invoke(cli.cli, ["install-completion", "--shell", "bash"])

    assert result.exit_code == 2
    assert "--edit-profile or --no-edit-profile" in result.output
    assert not (tmp_path / ".local/share/bash-completion/completions").exists()


def test_profile_choice_defaults_to_no(
    completion_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    confirm_args: dict[str, object] = {}

    def confirm(*args: object, **kwargs: object) -> bool:
        confirm_args.update(kwargs)
        return False

    monkeypatch.setattr(click, "confirm", confirm)

    assert completion_module._choose_profile_edit(None, interactive=True) is False
    assert confirm_args["default"] is False


def test_profile_choice_is_required_noninteractively(completion_module) -> None:
    with pytest.raises(click.UsageError, match="--edit-profile or --no-edit-profile"):
        completion_module._choose_profile_edit(None, interactive=False)


def test_bash_profile_edit_is_idempotent(
    cli, completion_module, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    first = CliRunner().invoke(
        cli.cli, ["install-completion", "--shell", "bash", "--edit-profile"]
    )
    profile = home / ".bashrc"
    first_contents = profile.read_text()
    second = CliRunner().invoke(
        cli.cli, ["install-completion", "--shell", "bash", "--edit-profile"]
    )

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert profile.read_text() == first_contents
    assert first_contents.count(completion_module.PROFILE_BLOCK_START) == 1
    assert '"$HOME/.local/share/bash-completion/completions/stlv"' in first_contents


def test_zsh_profile_edit_precedes_compinit(
    cli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    profile = tmp_path / ".zshrc"
    profile.write_text("autoload -Uz compinit\ncompinit\n")

    result = CliRunner().invoke(
        cli.cli, ["install-completion", "--shell", "zsh", "--edit-profile"]
    )

    contents = profile.read_text()
    assert result.exit_code == 0, result.output
    assert contents.index("fpath=(") < contents.index("compinit")

    repeated = CliRunner().invoke(
        cli.cli, ["install-completion", "--shell", "zsh", "--edit-profile"]
    )
    assert repeated.exit_code == 0, repeated.output
    assert profile.read_text() == contents


def test_no_edit_profile_leaves_profile_untouched(
    cli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    profile = tmp_path / ".bashrc"
    profile.write_text("# existing profile\n")

    result = CliRunner().invoke(
        cli.cli, ["install-completion", "--shell", "bash", "--no-edit-profile"]
    )

    assert result.exit_code == 0, result.output
    assert profile.read_text() == "# existing profile\n"


def test_powershell_profile_edit_uses_the_current_user_profile(
    cli, completion_module, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    profile = tmp_path / "Documents/PowerShell/Microsoft.PowerShell_profile.ps1"
    monkeypatch.setattr(completion_module, "_profile_path", lambda shell, home: profile)

    result = CliRunner().invoke(
        cli.cli, ["install-completion", "--shell", "powershell", "--edit-profile"]
    )

    assert result.exit_code == 0, result.output
    assert "*.ps1" in profile.read_text()
    contents = profile.read_text()
    assert contents.count(completion_module.PROFILE_BLOCK_START) == 1

    repeated = CliRunner().invoke(
        cli.cli, ["install-completion", "--shell", "powershell", "--edit-profile"]
    )
    assert repeated.exit_code == 0, repeated.output
    assert profile.read_text() == contents
