import getpass
import logging
import os
from collections.abc import Callable
from importlib import metadata
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

import click
from appdirs import user_log_dir
from rich.console import Console
from rich.logging import RichHandler

from stelvio.cli.init_command import create_stlv_app_file, get_stlv_app_path, stelvio_art
from stelvio.project import get_user_env, save_user_env
from stelvio.pulumi import (
    install_pulumi,
    needs_pulumi,
    run_pulumi_cancel,
    run_pulumi_deploy,
    run_pulumi_destroy,
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


@click.group()
@click.option(
    "--verbose", "-v", count=True, help="Increase verbosity. -v for INFO, -vv for DEBUG logs."
)
def cli(verbose: int) -> None:
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

    if needs_pulumi():
        with console.status("Downloading Pulumi..."):
            install_pulumi()


@click.command()
def init() -> None:
    """
    Initialize a Stelvio project in the current directory.
    Creates stlv_app.py with AWS configuration template.
    """
    stelvio_art(console)
    stlv_app_path, app_exists = get_stlv_app_path()
    if app_exists:
        logger.info("stlv_app.py exists")
        console.print("[green]Stelvio project already exists.")
        return

    logger.info("stlv_app.py does not exist. Initializing Stelvio project")
    console.print("[bold]Initializing Stelvio project...[/bold]")

    create_stlv_app_file(stlv_app_path)

    console.print("\n[bold green]✓[/bold green] Created stlv_app.py")
    console.print("\nEdit stlv_app.py to customize AWS profile and region if needed.")
    console.print("By default, Stelvio uses your AWS CLI configuration and environment variables.")
    console.print("\n[bold]You're all set up! Let's build something great![/bold]")


@click.command()
def version() -> None:
    """Prints the version of Stelvio."""
    console.print(metadata.version("stelvio"))


@click.command()
@click.argument("env", default=None, required=False)
@click.option("--show-unchanged", is_flag=True, help="Show resources that won't change")
def diff(env: str | None, show_unchanged: bool) -> None:
    """Shows the changes that will be made when you deploy."""
    env = determine_env(env)

    safe_run_pulumi(run_pulumi_preview, env, show_unchanged=show_unchanged)


@click.command()
@click.argument("env", default=None, required=False)
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompts")
@click.option("--show-unchanged", is_flag=True, help="Show resources that won't change")
def deploy(env: str | None, yes: bool, show_unchanged: bool) -> None:
    """Deploys your app."""
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
def refresh(env: str | None) -> None:
    """
    Compares your local state with actual state in the cloud.
    Any changes will be sync to your local state.
    """
    env = determine_env(env)
    safe_run_pulumi(run_pulumi_refresh, env)


@click.command()
@click.argument("env", default=None, required=False)
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompts")
def destroy(env: str | None, yes: bool) -> None:
    """Destroys all resources in your app."""
    # Always ask for confirmation unless --yes
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
    env = determine_env(env)
    safe_run_pulumi(run_pulumi_cancel, env)


cli.add_command(version)
cli.add_command(init)
cli.add_command(diff)
cli.add_command(deploy)
cli.add_command(refresh)
cli.add_command(destroy)
cli.add_command(unlock)


def determine_env(environment: str) -> str:
    if environment:
        return environment

    user_env = get_user_env()
    if not user_env:
        user_env = getpass.getuser()
        save_user_env(user_env)
    return user_env
