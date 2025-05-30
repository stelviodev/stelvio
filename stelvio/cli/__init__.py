import logging
from importlib import metadata
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

import click
from appdirs import user_log_dir
from click.core import ParameterSource
from rich.console import Console
from rich.logging import RichHandler

from stelvio.cli.init_command import create_stlv_app_file, get_stlv_app_path, stelvio_art
from stelvio.pulumi import (
    install_pulumi,
    needs_pulumi,
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
            console.print("[italic blue] Console verbosity: INFO[/]")
        elif verbose >= 2:  # noqa: PLR2004
            console_handler.setLevel(logging.DEBUG)
            console.print("[italic green]Console verbosity: DEBUG[/]")
        app_logger.addHandler(console_handler)

    if needs_pulumi():
        with console.status("Downloading Pulumi..."):
            install_pulumi()


@click.command()
@click.option(
    "--profile",
    default="default",
    show_default=True,
    help="The AWS profile name from your config (e.g., default, my-dev-profile).",
)
@click.option(
    "--region",
    default="us-east-1",
    show_default=True,
    help="The AWS region to use (e.g., us-east-1, eu-west-2).",
)
@click.pass_context
def init(ctx: click.Context, profile: str, region: str) -> None:
    """
    Initializes a Stelvio project in the current directory. Creates stlv_app.py file
    with a StelvioApp.
    """
    stelvio_art(console)
    stlv_app_path, app_exists = get_stlv_app_path()
    if app_exists:
        logger.info("stlv_app.py exists")
        console.print("[green]Stelvio project already exists.")
        return

    logger.info("stlv_app.py does not exists. Initializing Stelvio project")
    console.print(
        "[bold]Hello. To get started Stelvio needs AWS profile and region so it can deploy your "
        "infrastructure."
    )
    console.print("\n[bold]You can change both of them later in stlv_app.py\n")

    final_region, final_profile = region, profile
    if ctx.get_parameter_source("profile") == ParameterSource:
        final_profile = click.prompt("Enter AWS Profile Name", default=profile, show_default=True)

    if ctx.get_parameter_source("region") == ParameterSource.DEFAULT:
        final_region = click.prompt("Enter AWS Region", default=region, show_default=True)

    create_stlv_app_file(final_profile, final_region, stlv_app_path)

    console.print("\n[bold]You're all set up! Let's build something great!")


@click.command()
def version() -> None:
    """Prints the version of Stelvio."""
    console.print(metadata.version("stelvio"))


@click.command()
def diff() -> None:
    """Shows the changes that will be made when you deploy."""
    click.echo("Previewing changes...")

    run_pulumi_preview("dev2")


@click.command()
def deploy() -> None:
    """Deploys your app."""
    click.echo("Deploying changes...")
    run_pulumi_deploy("dev2")


@click.command()
def refresh() -> None:
    """
    Compares your local state with actual state in the cloud.
    Any changes will be sync to your local state.
    """
    import sys

    click.echo("Refreshing changes...")
    run_pulumi_refresh("dev2")


@click.command()
def destroy() -> None:
    """Destroys all resources in your app."""
    click.echo("Destroying changes...")
    run_pulumi_destroy("dev2")


cli.add_command(version)
cli.add_command(init)
cli.add_command(diff)
cli.add_command(deploy)
cli.add_command(refresh)
cli.add_command(destroy)
