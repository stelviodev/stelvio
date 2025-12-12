import getpass
import logging
import os
import sys
from collections.abc import Callable
from importlib import metadata
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

import click
from appdirs import user_log_dir
from rich.console import Console
from rich.logging import RichHandler

from stelvio import context
from stelvio.bridge.local.listener import run_bridge_server
from stelvio.cli.init_command import create_stlv_app_file, get_stlv_app_path, stelvio_art
from stelvio.git import copy_from_github
from stelvio.project import get_user_env, save_user_env
from stelvio.pulumi import (
    install_pulumi,
    needs_pulumi,
    run_pulumi_cancel,
    run_pulumi_deploy,
    run_pulumi_destroy,
    run_pulumi_dev,
    run_pulumi_outputs,
    run_pulumi_preview,
    run_pulumi_refresh,
)

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

# Suppress gRPC and absl logging
os.environ["GRPC_VERBOSITY"] = "ERROR"
os.environ["GRPC_TRACE"] = ""
logging.getLogger("grpc").setLevel(logging.ERROR)
logging.getLogger("absl").setLevel(logging.ERROR)


def safe_run_pulumi(func: Callable, env: str | None, **kwargs: bool) -> None:
    from stelvio.exceptions import StelvioProjectError

    try:
        return func(env, **kwargs)
    except StelvioProjectError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        raise click.Abort from e


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
    _ensure_pulumi()
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
    _ensure_pulumi()
    console.print("[green]✓[/green] System check passed")
    """Shows Stelvio and Pulumi versions."""
    _version()


@click.command()
@click.argument("env", default=None, required=False)
@click.option("--show-unchanged", is_flag=True, help="Show resources that won't change")
def diff(env: str | None, show_unchanged: bool) -> None:
    """Shows the changes that will be made when you deploy."""
    _ensure_pulumi()
    env = determine_env(env)

    safe_run_pulumi(run_pulumi_preview, env, show_unchanged=show_unchanged)


@click.command()
@click.argument("env", default=None, required=False)
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompts")
@click.option("--show-unchanged", is_flag=True, help="Show resources that won't change")
def deploy(env: str | None, yes: bool, show_unchanged: bool) -> None:
    """Deploys your app."""
    _ensure_pulumi()
    from stelvio.exceptions import AppRenamedError

    # Ask for confirmation on shared environments unless --yes
    if not yes and env is not None:
        console.print(f"About to deploy to [bold red]{env}[/bold red] environment.")
        if not click.confirm(f"Deploy to {env}?"):
            console.print("Deployment cancelled.")
            return
    env = determine_env(env)

    try:
        safe_run_pulumi(run_pulumi_deploy, env, show_unchanged=show_unchanged)
    except AppRenamedError as e:
        console.print(
            f"\n[bold yellow]⚠️  Warning:[/bold yellow] App name changed from '{e.old_name}' "
            f"to '{e.new_name}'\n"
        )
        console.print(
            "This will create a [bold]NEW app[/bold] in AWS, not rename the existing one."
        )
        console.print(f"The old app '{e.old_name}' will continue to exist.\n")
        console.print("To remove the old app, you'll need to:")
        console.print(f"  1. Change the app name back to '{e.old_name}' in stlv_app.py")
        console.print("  2. Run: [bold]stlv destroy[/bold]\n")

        if yes or click.confirm("Deploy as new app?"):
            console.print(f"\nDeploying new app '{e.new_name}'...")
            safe_run_pulumi(
                run_pulumi_deploy, env, confirmed_new_app=True, show_unchanged=show_unchanged
            )
        else:
            console.print("Deployment cancelled.")


@click.command()
@click.argument("env", default=None, required=False)
def dev(env: str | None) -> None:
    """Deploys your app in bridge mode."""
    _ensure_pulumi()
    from stelvio.exceptions import AppRenamedError

    env = determine_env(env)

    try:
        safe_run_pulumi(
            run_pulumi_dev,
            env,
        )
    except AppRenamedError as e:
        console.print(
            f"\n[bold yellow]⚠️  Warning:[/bold yellow] App name changed from '{e.old_name}' "
            f"to '{e.new_name}'\n"
        )
        console.print(
            "This will create a [bold]NEW app[/bold] in AWS, not rename the existing one."
        )
        console.print(f"The old app '{e.old_name}' will continue to exist.\n")
        console.print("To remove the old app, you'll need to:")
        console.print(f"  1. Change the app name back to '{e.old_name}' in stlv_app.py")
        console.print("  2. Run: [bold]stlv destroy[/bold]\n")

        if click.confirm("Deploy as new app in bridge mode?"):
            console.print(f"\nDeploying new app '{e.new_name}' in bridge mode...")
            safe_run_pulumi(run_pulumi_dev, env, confirmed_new_app=True)
        else:
            console.print("Deployment cancelled.")
            return

    console.print("\n[bold green]✓[/bold green] Stelvio app deployed in bridge mode.")
    console.print("Running local dev server now...")

    run_bridge_server(
        region=context().aws.region,
        profile=context().aws.profile,
        app_name=context().name,
        stage=env,
    )


@click.command()
@click.argument("env", default=None, required=False)
def refresh(env: str | None) -> None:
    """
    Compares your local state with actual state in the cloud.
    Any changes will be sync to your local state.
    """
    _ensure_pulumi()
    env = determine_env(env)
    safe_run_pulumi(run_pulumi_refresh, env)


@click.command()
@click.argument("env", default=None, required=False)
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompts")
def destroy(env: str | None, yes: bool) -> None:
    """Destroys all resources in your app."""
    # Always ask for confirmation unless --yes
    _ensure_pulumi()
    env = determine_env(env)
    if not yes:
        console.print(
            f"About to [bold red]destroy all resources[/bold red] "
            f"in [bold]{env}[/bold] environment."
        )
        console.print("⚠️  This action cannot be undone!")

        # Ask user to type environment name for extra safety
        typed_env = click.prompt(f"Type the environment name '{env}' to confirm")
        if typed_env != env:
            console.print(f"Environment name mismatch. Expected '{env}', got '{typed_env}'.")
            console.print("Destruction cancelled.")
            return

    safe_run_pulumi(run_pulumi_destroy, env)


@click.command()
@click.argument("env", default=None, required=False)
def unlock(env: str | None) -> None:
    """
    Unlocks state. Stelvio locks state file during deployment but if deployment fails abruptly
    or is killed then state stays locked. This command will unlock it.
    """
    _ensure_pulumi()
    env = determine_env(env)
    safe_run_pulumi(run_pulumi_cancel, env)


@click.command()
@click.argument("env", default=None, required=False)
@click.option("--json", is_flag=True, help="Output in JSON format")
def outputs(env: str | None, json: bool) -> None:
    """
    Shows environment outputs in key-value pairs (as JSON object if `--json` is passed).
    """
    _ensure_pulumi()
    env = determine_env(env)
    safe_run_pulumi(run_pulumi_outputs, env, json=json)


cli.add_command(version)
cli.add_command(init)
cli.add_command(diff)
cli.add_command(deploy)
cli.add_command(dev)
cli.add_command(refresh)
cli.add_command(destroy)
cli.add_command(unlock)
cli.add_command(outputs)
cli.add_command(system)


def determine_env(environment: str | None) -> str:
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


def _ensure_pulumi() -> None:
    if needs_pulumi():
        with console.status("Downloading Pulumi..."):
            install_pulumi()


def _version() -> None:
    stelvio_version = metadata.version("stelvio")
    pulumi_version = metadata.version("pulumi")
    console.print(f"Stelvio version: {stelvio_version}", highlight=False)
    console.print(f"Pulumi version: {pulumi_version}", highlight=False)
    sys.exit(0)
