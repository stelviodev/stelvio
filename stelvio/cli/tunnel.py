"""Native helper setup and host recovery commands, independent of an app."""

from collections.abc import Callable

import boto3
import click

from stelvio.pulumi import get_stelvio_config_dir
from stelvio.tunnel.assets import NativeAssetError
from stelvio.tunnel.helper_client import HelperError, NativeHelper
from stelvio.tunnel.installation import SYSTEM_CHANGES, cleanup_helper, install_helper
from stelvio.tunnel.recovery import recover_session


@click.group()
def tunnel() -> None:
    """Manage the native macOS VPC dev helper."""


def _run(action: Callable[[], None]) -> None:
    try:
        action()
    except (HelperError, NativeAssetError, OSError) as error:
        raise click.ClickException(str(error)) from error


@tunnel.command("install")
def install() -> None:
    """Install the trusted helper using macOS administrator authorization."""
    click.echo(SYSTEM_CHANGES)
    _run(install_helper)
    click.echo("Native VPC helper ready.")


@tunnel.command("inspect")
def inspect() -> None:
    """Inspect the installed helper and its owned host units."""

    def show() -> None:
        value = NativeHelper().inspect()
        click.echo(f"owned={value.owned} uncertain={value.uncertain} mutating={value.mutating}")
        for unit in value.units:
            click.echo(f"{unit.unit} generation={unit.generation} {unit.status.name.lower()}")

    _run(show)


@tunnel.command("reconcile")
def reconcile() -> None:
    """Reconcile stale host state, refusing a live owner."""
    _run(lambda: NativeHelper().reconcile())
    click.echo("Host recovery complete.")


@tunnel.command("recover")
@click.option("--bucket", required=True)
@click.option("--home-account", required=True)
@click.option("--home-region", required=True)
@click.option("--home-profile", default=None)
@click.option("--account", required=True)
@click.option("--region", required=True)
@click.option("--profile", default=None)
@click.option("--app", required=True)
@click.option("--env", required=True)
@click.option("--session", required=True)
def recover(**options: str) -> None:
    """Recover this user's stale temporary AWS access; never stop a live owner."""
    try:
        home = boto3.Session(
            profile_name=options.pop("home_profile"), region_name=options.pop("home_region")
        )
        target = boto3.Session(
            profile_name=options.pop("profile"), region_name=options.pop("region")
        )
        recover_session(
            home=home,
            target=target,
            config_dir=get_stelvio_config_dir(),
            report=click.echo,
            **options,
        )
    except Exception as error:
        raise click.ClickException(str(error)) from error


@tunnel.command("cleanup")
def cleanup() -> None:
    """Uninstall the helper after owned host cleanup; refuse active sessions."""
    click.echo("Stop the native helper and remove its owned installation and host changes.")
    _run(cleanup_helper)
    click.echo("Native VPC helper removed.")
