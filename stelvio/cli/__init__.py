# Must be first - redirects gRPC C++ stderr
import stelvio._suppress_grpc  # noqa: F401  # isort: skip

import getpass
import json
import logging
import os
import sys
from datetime import datetime
from enum import IntEnum
from importlib import metadata
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import NoReturn

import click
from platformdirs import user_log_dir
from rich.console import Console
from rich.logging import RichHandler

from stelvio.cli.commands import (
    run_deploy,
    run_destroy,
    run_dev,
    run_diff,
    run_outputs,
    run_refresh,
    run_state_list,
    run_state_remove,
    run_state_repair,
    run_unlock,
)
from stelvio.cli.init_command import create_stlv_app_file, get_stlv_app_path, stelvio_art
from stelvio.command_run import get_environment_confirmation_info
from stelvio.exceptions import StateLockedError, StelvioProjectError, StelvioValidationError
from stelvio.git import copy_from_github
from stelvio.project import get_user_env, save_user_env
from stelvio.pulumi import ensure_pulumi

console = Console()

app_logger = logging.getLogger("stelvio")
# Set the logger to capture ALL messages from 'stelvio' internally
app_logger.setLevel(logging.DEBUG)

app_name = "stelvio"
log_dir = Path(user_log_dir(app_name))
log_dir.mkdir(parents=True, exist_ok=True)
log_file_path = log_dir / f"{app_name}.log"
file_handler = TimedRotatingFileHandler(
    filename=str(log_file_path), when="D", interval=1, backupCount=7, encoding="utf-8"
)
file_handler.setLevel(logging.DEBUG)  # All debug messages and above go to the file
file_formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
file_handler.setFormatter(file_formatter)
app_logger.addHandler(file_handler)

logger = logging.getLogger(__name__)

# Suppress gRPC and absl Python loggers
# Note: Currently ineffective for C++ logs (see _suppress_grpc module), but may work
# once grpc PR #39779 is released and _suppress_grpc workaround is removed
logging.getLogger("grpc").setLevel(logging.ERROR)
logging.getLogger("absl").setLevel(logging.ERROR)


class CliExitCode(IntEnum):
    SUCCESS = 0
    OPERATION_FAILED = 1
    USAGE_ERROR = 2
    STATE_LOCKED = 4


def _format_lock_time(created: str) -> str:
    """Format ISO timestamp to local time for display."""
    try:
        dt = datetime.fromisoformat(created)
        local_dt = dt.astimezone()  # Convert to local timezone
        return local_dt.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return created


def _exit_with_code(code: CliExitCode) -> NoReturn:
    raise SystemExit(int(code)) from None


def _validate_exclusive_flags(json_output: bool, stream_output: bool) -> None:
    if json_output and stream_output:
        raise StelvioValidationError("--json and --stream are mutually exclusive.")


def _require_yes_for_machine_output(json_output: bool, stream_output: bool, message: str) -> None:
    """Raise if json/stream mode is active without --yes."""
    if json_output or stream_output:
        mode_flag = "--stream" if stream_output else "--json"
        raise StelvioValidationError(f"{mode_flag} {message}")


def _emit_json_cli_error(
    *,
    operation: str,
    env: str | None,
    error: Exception,
    exit_code: CliExitCode,
    stream: bool = False,
) -> None:
    """Build and emit a JSON/stream error payload."""
    payload = {
        "operation": operation,
        "app": None,
        "env": env,
        "timestamp": datetime.now().astimezone().isoformat(),
        "status": "failed",
        "exit_code": int(exit_code),
        "errors": [{"message": str(error)}],
    }
    if stream:
        payload["event"] = "error"
        sys.stdout.write(json.dumps(payload) + "\n")
        sys.stdout.flush()
    else:
        console.print_json(data=payload)


def _handle_cli_error(
    error: Exception,
    *,
    operation: str | None = None,
    env: str | None = None,
    json_output: bool = False,
    stream_output: bool = False,
) -> NoReturn:
    """Format and display a CLI error, then exit with the appropriate code."""
    if isinstance(error, StateLockedError):
        code = CliExitCode.STATE_LOCKED
    else:
        code = CliExitCode.USAGE_ERROR

    if (json_output or stream_output) and operation is not None:
        _emit_json_cli_error(
            operation=operation, env=env, error=error, exit_code=code, stream=stream_output
        )
    elif isinstance(error, StateLockedError):
        lock_time = _format_lock_time(error.created)
        console.print("\n[bold red]✗ State is locked[/bold red]")
        console.print(
            f"  Environment '{error.env}' is locked by '[cyan]{error.command}[/cyan]' "
            f"since [cyan]{lock_time}[/cyan]",
            highlight=False,
        )
        console.print("\n  If you're sure no other operation is running, force unlock with:")
        console.print(f"  [bold]stlv unlock {error.env}[/bold]\n")
    else:
        console.print(f"[red]{error}[/red]")

    _exit_with_code(code)


@click.group(invoke_without_command=True)
@click.option(
    "--verbose", "-v", count=True, help="Increase verbosity. -v for INFO, -vv for DEBUG logs."
)
@click.option("--version", is_flag=True, help="Show Stelvio and Pulumi versions.")
@click.pass_context
def cli(ctx: click.Context, verbose: int, version: bool) -> None:
    if version:
        _version()  # Exits after printing versions

    # If no command was invoked, show help
    if ctx.invoked_subcommand is None:
        console.print(ctx.get_help())
        ctx.exit(0)

    if verbose > 0:
        console_handler = RichHandler(
            console=console,
            show_time=False,
            show_level=True,
            markup=True,
            tracebacks_suppress=[click],
            rich_tracebacks=True,
        )
        if verbose == 1:
            console_handler.setLevel(logging.INFO)
            console.print("[italic blue]Console verbosity: INFO[/]")
        elif verbose >= 2:  # noqa: PLR2004
            console_handler.setLevel(logging.DEBUG)
            console.print("[italic green]Console verbosity: DEBUG[/]")

        console.print(f"[italic dim]Logs saved to: {log_file_path}[/]")
        app_logger.addHandler(console_handler)


@click.command()
@click.option("--template", default=None, help="Template to use for initialization")
def init(template: str | None) -> None:
    """
    Initialize a Stelvio project in the current directory.
    Creates stlv_app.py with AWS configuration template.
    """
    ensure_pulumi()
    stelvio_art(console)
    stlv_app_path, app_exists = get_stlv_app_path()
    if app_exists:
        logger.info("stlv_app.py exists")
        console.print("[green]Stelvio project already exists.")
        return

    logger.info("stlv_app.py does not exist. Initializing Stelvio project")
    console.print("[bold]Initializing Stelvio project...[/bold]")

    if template is not None:
        owner, repo, branch, subdirectory = _parse_template_string(template)

        try:
            copy_from_github(
                owner=owner,
                repo=repo,
                branch=branch,
                subdirectory=subdirectory,
                destination=stlv_app_path.parent,
            )
        except Exception as e:
            console.print(f"[bold red]Error copying template:[/bold red] {e}")
            return
    else:
        create_stlv_app_file(stlv_app_path)

    console.print("\n[bold green]✓[/bold green] Created stlv_app.py")
    console.print("\nEdit stlv_app.py to customize AWS profile and region if needed.")
    console.print("By default, Stelvio uses your AWS CLI configuration and environment variables.")
    console.print("\n[bold]You're all set up! Let's build something great![/bold]")


@click.command()
def version() -> None:
    """Shows version and exit."""
    _version()


@click.command()
def system() -> None:
    """Performs a system check for Stelvio."""
    ensure_pulumi()
    console.print("[green]✓[/green] System check passed")
    """Shows Stelvio and Pulumi versions."""
    _version()


@click.command()
@click.argument("env", default=None, required=False)
@click.option("--show-unchanged", is_flag=True, help="Show resources that won't change")
@click.option("--compact", is_flag=True, help="Show only component-level summary without details")
@click.option("--json", "json_output", is_flag=True, help="Output in JSON format")
def diff(env: str | None, show_unchanged: bool, compact: bool, json_output: bool) -> None:
    """Shows the changes that will be made when you deploy."""
    ensure_pulumi(show_status=not json_output)
    try:
        env = determine_env(env, require_explicit_in_ci=True, command_name="diff")
        run_diff(env, show_unchanged=show_unchanged, compact=compact, json_output=json_output)
    except (StelvioProjectError, StelvioValidationError) as e:
        _handle_cli_error(e, operation="diff", env=env, json_output=json_output)


@click.command()
@click.argument("env", default=None, required=False)
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompts")
@click.option("--show-unchanged", is_flag=True, help="Show resources that won't change")
@click.option("--json", "json_output", is_flag=True, help="Output in JSON format")
@click.option(
    "--stream", "stream_output", is_flag=True, help="Output newline-delimited JSON events"
)
def deploy(
    env: str | None, yes: bool, show_unchanged: bool, json_output: bool, stream_output: bool
) -> None:
    """Deploys your app."""
    error_ctx = {
        "operation": "deploy",
        "env": env,
        "json_output": json_output,
        "stream_output": stream_output,
    }
    try:
        _validate_exclusive_flags(json_output, stream_output)
        ensure_pulumi(show_status=not (json_output or stream_output))
        env = determine_env(env, require_explicit_in_ci=True, command_name="deploy")
        error_ctx["env"] = env
        _, is_shared_env = get_environment_confirmation_info(env)
        if is_shared_env and not yes:
            _require_yes_for_machine_output(
                json_output, stream_output, "deploy to a shared environment requires --yes."
            )
            console.print(f"About to deploy to [bold red]{env}[/bold red] environment.")
            if not click.confirm(f"Deploy to {env}?"):
                console.print("Deployment cancelled.")
                return
        run_deploy(
            env,
            show_unchanged=show_unchanged,
            json_output=json_output,
            stream_output=stream_output,
        )
    except (StelvioProjectError, StelvioValidationError) as e:
        _handle_cli_error(e, **error_ctx)
    except StateLockedError as e:
        _handle_cli_error(e, **error_ctx)


@click.command()
@click.argument("env", default=None, required=False)
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompts")
@click.option("--show-unchanged", is_flag=True, help="Show resources that won't change")
def dev(env: str | None, yes: bool, show_unchanged: bool) -> None:
    """Starts your app in dev mode."""
    ensure_pulumi()
    try:
        env = determine_env(env, require_explicit_in_ci=True, command_name="dev")
        _, is_shared_env = get_environment_confirmation_info(env)
        if is_shared_env and not yes:
            console.print(f"About to deploy to [bold red]{env}[/bold red] environment.")
            if not click.confirm(f"Deploy to {env}?"):
                console.print("Deployment cancelled.")
                return
        run_dev(env, show_unchanged=show_unchanged)
    except (StelvioProjectError, StelvioValidationError) as e:
        _handle_cli_error(e)
    except StateLockedError as e:
        _handle_cli_error(e)


@click.command()
@click.argument("env", default=None, required=False)
@click.option("--json", "json_output", is_flag=True, help="Output in JSON format")
def refresh(env: str | None, json_output: bool) -> None:
    """
    Compares your local state with actual state in the cloud.
    Any changes will be sync to your local state.
    """
    ensure_pulumi(show_status=not json_output)
    try:
        env = determine_env(env, require_explicit_in_ci=True, command_name="refresh")
        run_refresh(env, json_output=json_output)
    except (StelvioProjectError, StelvioValidationError) as e:
        _handle_cli_error(e, operation="refresh", env=env, json_output=json_output)
    except StateLockedError as e:
        _handle_cli_error(e, operation="refresh", env=env, json_output=json_output)


@click.command()
@click.argument("env", default=None, required=False)
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompts")
@click.option("--json", "json_output", is_flag=True, help="Output in JSON format")
@click.option(
    "--stream", "stream_output", is_flag=True, help="Output newline-delimited JSON events"
)
def destroy(env: str | None, yes: bool, json_output: bool, stream_output: bool) -> None:
    """Destroys all resources in your app."""
    error_ctx = {
        "operation": "destroy",
        "env": env,
        "json_output": json_output,
        "stream_output": stream_output,
    }
    try:
        _validate_exclusive_flags(json_output, stream_output)
        ensure_pulumi(show_status=not (json_output or stream_output))
        env = determine_env(env, require_explicit_in_ci=True, command_name="destroy")
        error_ctx["env"] = env
        if not yes:
            _require_yes_for_machine_output(
                json_output, stream_output, "destroy requires --yes to avoid interactive prompts."
            )
        run_destroy(env, skip_confirm=yes, json_output=json_output, stream_output=stream_output)
    except (StelvioProjectError, StelvioValidationError) as e:
        _handle_cli_error(e, **error_ctx)
    except StateLockedError as e:
        _handle_cli_error(e, **error_ctx)


@click.command()
@click.argument("env", default=None, required=False)
def unlock(env: str | None) -> None:
    """
    Force unlock state. Use when a previous command was interrupted and left state locked.
    """
    ensure_pulumi()
    try:
        env = determine_env(env)
        lock_info = run_unlock(env)
        if lock_info:
            lock_time = _format_lock_time(lock_info["created"])
            console.print(
                f"[bold green]✓ Unlocked[/bold green] "
                f"(was locked by '{lock_info['command']}' since {lock_time})"
            )
        else:
            console.print(f"[yellow]No lock found for environment '{env}'[/yellow]")
    except (StelvioProjectError, StelvioValidationError) as e:
        _handle_cli_error(e)


@click.command()
@click.argument("env", default=None, required=False)
@click.option("--json", is_flag=True, help="Output in JSON format")
def outputs(env: str | None, json: bool) -> None:
    """Show component URLs and user-defined exports."""
    ensure_pulumi(show_status=not json)
    try:
        env = determine_env(env)
        run_outputs(env, json_output=json)
    except (StelvioProjectError, StelvioValidationError) as e:
        _handle_cli_error(e, operation="outputs", env=env, json_output=json)


@click.group()
def state() -> None:
    """Manage Pulumi state directly (for recovery scenarios)."""
    ensure_pulumi()


@state.command("list")
@click.option("--env", "-e", default=None, help="Environment (defaults to personal env)")
@click.option("--json", "json_output", is_flag=True, help="Output in JSON format")
@click.option("--outputs", is_flag=True, help="Show Pulumi outputs stored per resource")
def state_list(env: str | None, json_output: bool, outputs: bool) -> None:
    """List all resources in state."""
    try:
        env = determine_env(env)
        run_state_list(env, json_output=json_output, show_outputs=outputs)
    except (StelvioProjectError, StelvioValidationError) as e:
        _handle_cli_error(e, operation="state_list", env=env, json_output=json_output)


@state.command("rm")
@click.argument("name")
@click.option("--env", "-e", default=None, help="Environment (defaults to personal env)")
def state_rm(name: str, env: str | None) -> None:
    """Remove resource from state (does NOT delete from cloud)."""
    try:
        env = determine_env(env)
        run_state_remove(env, name)
    except (StelvioProjectError, StelvioValidationError) as e:
        _handle_cli_error(e)
    except StateLockedError as e:
        _handle_cli_error(e)


@state.command("repair")
@click.option("--env", "-e", default=None, help="Environment (defaults to personal env)")
def state_repair(env: str | None) -> None:
    """Repair state by fixing orphans and broken dependencies."""
    try:
        env = determine_env(env)
        run_state_repair(env)
    except (StelvioProjectError, StelvioValidationError) as e:
        _handle_cli_error(e)
    except StateLockedError as e:
        _handle_cli_error(e)


cli.add_command(version)
cli.add_command(init)
cli.add_command(diff)
cli.add_command(deploy)
cli.add_command(dev)
cli.add_command(refresh)
cli.add_command(destroy)
cli.add_command(unlock)
cli.add_command(outputs)
cli.add_command(state)
cli.add_command(system)


def determine_env(
    environment: str | None,
    *,
    require_explicit_in_ci: bool = False,
    command_name: str | None = None,
) -> str:
    if require_explicit_in_ci and environment is None and os.getenv("CI"):
        if command_name:
            raise StelvioValidationError(
                "Environment is required in CI. "
                f"Pass an explicit env like 'stlv {command_name} prod'."
            )
        raise StelvioValidationError("Environment is required in CI. Pass an explicit env.")
    if environment:
        return environment

    user_env = get_user_env()
    if not user_env:
        user_env = getpass.getuser()
        save_user_env(user_env)
    return user_env


OWNER_REPO_SUBDIR_PARTS = 3  # owner/repo/subdirectory format


def _parse_template_string(template: str) -> tuple[str, str, str, str | None]:
    """Parse template string into GitHub repository components.

    Supports formats:
    - 'base' → stelviodev/templates/base (main branch, subdirectory 'base')
    - 'gh:owner/repo' → owner/repo (main branch)
    - 'gh:owner/repo@branch' → with specific branch
    - 'gh:owner/repo/subdir' → with subdirectory
    - 'gh:owner/repo@branch/subdir' → branch + subdirectory

    Returns:
        Tuple of (owner, repo, branch, subdirectory)
    """
    if not template.startswith("gh:"):
        owner = "stelviodev"
        repo = "templates"
        branch = "main"
        subdirectory = template
    else:
        gh_template = template[3:]
        if "@" in gh_template:
            repo_part, branch_subdir_part = gh_template.split("@", 1)
            if "/" in branch_subdir_part:
                branch, subdirectory = branch_subdir_part.split("/", 1)
            else:
                branch = branch_subdir_part
                subdirectory = None
        else:
            repo_part = gh_template
            branch = "main"
            subdirectory = None

        if "/" in repo_part:
            parts = repo_part.split("/", 2)
            owner = parts[0]
            repo = parts[1]
            if len(parts) == OWNER_REPO_SUBDIR_PARTS:
                subdirectory = parts[2]
        else:
            raise ValueError(
                f"Invalid template format: '{repo_part}'. "
                "Expected format: gh:owner/repo[@branch][/subdirectory]"
            )
    return owner, repo, branch, subdirectory


def _version() -> None:
    stelvio_version = metadata.version("stelvio")
    pulumi_version = metadata.version("pulumi")
    console.print(f"Stelvio version: {stelvio_version}", highlight=False)
    console.print(f"Pulumi version: {pulumi_version}", highlight=False)
    sys.exit(int(CliExitCode.SUCCESS))
