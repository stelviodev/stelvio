"""Native helper setup and host recovery commands, independent of an app."""

from collections.abc import Callable

import click

from stelvio.tunnel.assets import NativeAssetError
from stelvio.tunnel.helper_client import HelperError, NativeHelper
from stelvio.tunnel.installation import SYSTEM_CHANGES, install_helper, uninstall_helper
from stelvio.tunnel.plugin import PluginAssetError


@click.group()
def tunnel() -> None:
    """Manage the native macOS VPC dev helper."""


def _run(action: Callable[[], None]) -> None:
    try:
        action()
    except (HelperError, NativeAssetError, PluginAssetError, OSError) as error:
        raise click.ClickException(str(error)) from error


@tunnel.command("install")
def install() -> None:
    """Install the trusted helper using macOS administrator authorization."""
    click.echo(SYSTEM_CHANGES)

    def perform() -> None:
        selected = install_helper()
        if selected.notice:
            click.echo(selected.notice)

    _run(perform)
    click.echo("Native VPC helper ready.")


@tunnel.command("status")
def status() -> None:
    """Show the helper's owned networking state."""

    def show() -> None:
        value = NativeHelper().status()
        click.echo(f"owned={value.owned} uncertain={value.uncertain} mutating={value.mutating}")
        for unit in value.units:
            click.echo(f"{unit.unit} generation={unit.generation} {unit.status.name.lower()}")

    _run(show)


@tunnel.command("cleanup")
def cleanup() -> None:
    """Clean stale owned host networking, leaving the helper installed."""
    _run(lambda: NativeHelper().reconcile())
    click.echo("Host cleanup complete.")


@tunnel.command("uninstall")
def uninstall() -> None:
    """Remove Traforo after safe host cleanup; refuse active sessions."""
    _run(uninstall_helper)
    click.echo("Native VPC helper removed.")
