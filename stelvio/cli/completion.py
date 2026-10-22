"""Install Click's native shell completions for the Stelvio CLI."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import click
from click.shell_completion import get_completion_class

if TYPE_CHECKING:
    from collections.abc import Mapping

SUPPORTED_SHELLS = ("bash", "zsh", "fish", "powershell")
EXECUTABLE_NAMES = ("stlv", "stelvio")
PROFILE_BLOCK_START = "# >>> Stelvio shell completions >>>"
PROFILE_BLOCK_END = "# <<< Stelvio shell completions <<<"


def _normalize_shell(value: str | None) -> str | None:
    if not value:
        return None

    name = value.strip().replace("\\", "/").rsplit("/", maxsplit=1)[-1].lower().lstrip("-")
    name = name.removesuffix(".exe")

    if name in SUPPORTED_SHELLS:
        return name
    if name in {"pwsh", "powershell_ise"}:
        return "powershell"
    return None


def _get_windows_parent_process_name() -> str | None:
    powershell = shutil.which("pwsh") or shutil.which("powershell.exe")
    if powershell is None:
        return None

    script = (
        f"$process = Get-CimInstance Win32_Process -Filter 'ProcessId = {os.getpid()}'; "
        "if ($process) { "
        "$parent = Get-CimInstance Win32_Process "
        "-Filter ('ProcessId = ' + $process.ParentProcessId); "
        "if ($parent) { $parent.Name } "
        "}"
    )
    try:
        result = subprocess.run(  # noqa: S603  # Executable is resolved from PATH; no shell is used.
            [powershell, "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            check=False,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip().splitlines()[-1] if result.stdout.strip() else None


def _get_parent_process_name() -> str | None:
    if os.name == "nt":
        return _get_windows_parent_process_name()

    ps_executable = shutil.which("ps")
    if ps_executable is None:
        return None
    try:
        result = subprocess.run(  # noqa: S603  # Executable is resolved from PATH; no shell is used.
            [ps_executable, "-p", str(os.getppid()), "-o", "comm="],
            capture_output=True,
            check=False,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip().splitlines()[-1] if result.stdout.strip() else None


def _environment_shells(env: Mapping[str, str]) -> set[str]:
    shells: set[str] = set()
    for variable, shell in (
        ("BASH_VERSION", "bash"),
        ("ZSH_VERSION", "zsh"),
        ("FISH_VERSION", "fish"),
        ("PSMODULEPATH", "powershell"),
        ("PSEXECUTIONPOLICYPREFERENCE", "powershell"),
    ):
        if any(key.upper() == variable and value for key, value in env.items()):
            shells.add(shell)
    return shells


def _detect_shell(env: Mapping[str, str] | None = None) -> str | None:
    shell_env = os.environ if env is None else env
    signals = _environment_shells(shell_env)
    if parent_shell := _normalize_shell(_get_parent_process_name()):
        signals.add(parent_shell)

    if len(signals) == 1:
        return signals.pop()
    if len(signals) > 1:
        return None

    # SHELL commonly names a user's login shell, which can differ from the shell
    # that launched this process. Use it only when no stronger signal is present.
    return _normalize_shell(shell_env.get("SHELL"))


def _is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _select_shell(shell: str | None, *, interactive: bool | None = None) -> str:
    if shell is not None:
        return shell

    if detected_shell := _detect_shell():
        return detected_shell

    if interactive is None:
        interactive = _is_interactive()
    if not interactive:
        raise click.UsageError(
            "Could not determine the current shell. Pass --shell with bash, zsh, fish, "
            "or powershell."
        )

    return click.prompt(
        "Could not determine the current shell. Choose a shell",
        type=click.Choice(SUPPORTED_SHELLS, case_sensitive=False),
    ).lower()


def _choose_profile_edit(edit_profile: bool | None, *, interactive: bool | None = None) -> bool:
    if edit_profile is not None:
        return edit_profile

    if interactive is None:
        interactive = _is_interactive()
    if not interactive:
        raise click.UsageError(
            "Choose whether to edit your shell profile with --edit-profile or "
            "--no-edit-profile when running non-interactively."
        )

    return click.confirm(
        "Update the shell profile to load completions where needed?", default=False
    )


def _completion_directory(shell: str, home: Path) -> Path:
    if shell == "bash":
        return home / ".local/share/bash-completion/completions"
    if shell == "zsh":
        return home / ".zsh/completions"
    if shell == "fish":
        return home / ".config/fish/completions"
    return home / ".config/powershell/completions"


def _completion_filename(shell: str, executable: str) -> str:
    if shell == "zsh":
        return f"_{executable}"
    if shell == "fish":
        return f"{executable}.fish"
    if shell == "powershell":
        return f"{executable}.ps1"
    return executable


def _generate_scripts(command: click.Command, shell: str) -> dict[str, str]:
    completion_class = get_completion_class(shell)
    scripts: dict[str, str] = {}
    for executable in EXECUTABLE_NAMES:
        completion_var = f"_{executable.upper()}_COMPLETE"
        scripts[executable] = completion_class(
            command,
            {},
            prog_name=executable,
            complete_var=completion_var,
        ).source()
    return scripts


def _profile_path(shell: str, home: Path) -> Path | None:
    if shell in {"bash", "zsh"}:
        return home / (".bashrc" if shell == "bash" else ".zshrc")
    if shell != "powershell":
        return None

    powershell = shutil.which("pwsh") or shutil.which("powershell.exe")
    if powershell is None:
        return None
    try:
        result = subprocess.run(  # noqa: S603  # Executable is resolved from PATH; no shell is used.
            [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "[Console]::Out.Write($PROFILE.CurrentUserCurrentHost)",
            ],
            capture_output=True,
            check=False,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return Path(result.stdout.strip())


def _profile_block(shell: str, existing_profile: str = "") -> str:
    if shell == "bash":
        body = (
            'for _stlv_completion in "$HOME/.local/share/bash-completion/completions/stlv" '
            '"$HOME/.local/share/bash-completion/completions/stelvio"; do\n'
            '  if [ -r "$_stlv_completion" ]; then\n'
            '    . "$_stlv_completion"\n'
            "  fi\n"
            "done\n"
            "unset _stlv_completion"
        )
    elif shell == "zsh":
        body = 'fpath=( "$HOME/.zsh/completions" $fpath )'
        if re.search(r"(?m)^(?!\s*#)[^\n]*\bcompinit\b", existing_profile) is None:
            body += "\nautoload -Uz compinit && compinit"
    else:
        body = (
            "$StelvioCompletionDirectory = Join-Path $HOME '.config/powershell/completions'\n"
            "if (Test-Path $StelvioCompletionDirectory) {\n"
            "    Get-ChildItem -Path $StelvioCompletionDirectory -Filter '*.ps1' -File | "
            "ForEach-Object { . $_.FullName }\n"
            "}"
        )
    return f"{PROFILE_BLOCK_START}\n{body}\n{PROFILE_BLOCK_END}"


def _insert_profile_block(profile: Path, shell: str) -> bool:
    try:
        original = profile.read_text(encoding="utf-8")
    except FileNotFoundError:
        original = ""

    content = original
    start = content.find(PROFILE_BLOCK_START)
    if start >= 0:
        end = content.find(PROFILE_BLOCK_END, start)
        if end < 0:
            return False
        end += len(PROFILE_BLOCK_END)
        if content[end : end + 1] == "\n":
            end += 1
        content = content[:start] + content[end:]

    block = _profile_block(shell, content)
    if shell == "zsh":
        match = re.search(r"(?m)^(?!\s*#)[^\n]*\bcompinit\b", content)
        insertion = match.start() if match is not None else 0
        prefix = content[:insertion]
        suffix = content[insertion:]
        if prefix and not prefix.endswith("\n"):
            prefix += "\n"
        updated = f"{prefix}{block}\n{suffix}"
    else:
        separator = "\n" if content and not content.endswith("\n") else ""
        updated = f"{content}{separator}{block}\n"

    if updated == original:
        return False
    profile.parent.mkdir(parents=True, exist_ok=True)
    profile.write_text(updated, encoding="utf-8")
    return True


def _manual_activation(shell: str) -> str:
    if shell == "bash":
        return (
            'Add this to ~/.bashrc:\n'
            'for f in "$HOME/.local/share/bash-completion/completions/stlv" '
            '"$HOME/.local/share/bash-completion/completions/stelvio"; do\n'
            '  [ -r "$f" ] && . "$f"\n'
            "done"
        )
    if shell == "zsh":
        return (
            'Add `fpath=( "$HOME/.zsh/completions" $fpath )` to ~/.zshrc before '
            "`compinit` (add `autoload -Uz compinit && compinit` if needed)."
        )
    if shell == "fish":
        return "Fish loads completions from ~/.config/fish/completions automatically."
    return (
        "Add this to your current-user PowerShell profile:\n"
        "$StelvioCompletionDirectory = Join-Path $HOME '.config/powershell/completions'\n"
        "if (Test-Path $StelvioCompletionDirectory) {\n"
        "    Get-ChildItem -Path $StelvioCompletionDirectory -Filter '*.ps1' -File | "
        "ForEach-Object { . $_.FullName }\n"
        "}"
    )


@click.command("install-completion")
@click.option(
    "--shell",
    "shell_name",
    type=click.Choice(SUPPORTED_SHELLS, case_sensitive=False),
    help="Shell to install completions for (detected when omitted).",
)
@click.option(
    "--edit-profile/--no-edit-profile",
    default=None,
    help="Add or skip startup configuration that loads the completions.",
)
@click.pass_context
def install_completion(
    ctx: click.Context, shell_name: str | None, edit_profile: bool | None
) -> None:
    """Install shell completions for both `stlv` and `stelvio`."""
    shell = _select_shell(shell_name)
    should_edit_profile = _choose_profile_edit(edit_profile)
    home = Path.home()
    command = ctx.find_root().command
    scripts = _generate_scripts(command, shell)

    output_directory = _completion_directory(shell, home)
    output_directory.mkdir(parents=True, exist_ok=True)
    for executable, script in scripts.items():
        destination = output_directory / _completion_filename(shell, executable)
        destination.write_text(script, encoding="utf-8")
        click.echo(f"Installed completion: {destination}")

    if should_edit_profile and shell != "fish":
        profile = _profile_path(shell, home)
        if profile is None:
            click.echo("Could not determine the current-user PowerShell profile path.")
            click.echo(_manual_activation(shell))
        else:
            changed = _insert_profile_block(profile, shell)
            if changed:
                click.echo(f"Updated shell profile: {profile}")
            else:
                click.echo(f"Shell profile already loads completions: {profile}")
    elif shell == "fish":
        click.echo(_manual_activation(shell))
    else:
        click.echo(_manual_activation(shell))
