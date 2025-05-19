import logging

import click

from stelvio.pulumi import install_pulumi, needs_pulumi, run_pulumi

# logger = logging.getLogger(__name__)


@click.group()
def cli() -> None:
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("asyncio").setLevel(logging.WARNING)

    # logger.info("LOGGER LOGGING")
    if needs_pulumi():
        click.echo("Pulumi not installed.")
        install_pulumi()


@click.command()
def preview() -> None:
    click.echo("Previewing changes...")

    run_pulumi()


@click.command()
def deploy() -> None:
    click.echo("Deploying changes...")


cli.add_command(preview)
cli.add_command(deploy)
