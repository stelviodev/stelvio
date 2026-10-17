"""Native helper setup and host recovery commands, independent of an app."""

from collections.abc import Callable

import click

from stelvio.exceptions import StateLockedError, StelvioProjectError, StelvioValidationError
from stelvio.tunnel.assets import NativeAssetError
from stelvio.tunnel.helper_client import HelperError, NativeHelper
from stelvio.tunnel.installation import SYSTEM_CHANGES, install_helper, uninstall_helper


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

    def perform() -> None:
        selected = install_helper()
        if selected.notice:
            click.echo(selected.notice)

    _run(perform)
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


@tunnel.command("start")
@click.argument("env", required=False)
def start(env: str | None) -> None:
    """Connect to deployed handler VPC dependencies without running dev mode."""
    from stelvio.cli import determine_env  # noqa: PLC0415 - avoid CLI registration cycle
    from stelvio.command_run import CommandRun  # noqa: PLC0415 - lazy application loading

    try:
        environment = determine_env(env, require_explicit_in_ci=True, command_name="tunnel start")
        with CommandRun(environment, state_only=True) as run:
            if not run.has_deployed:
                raise click.ClickException("No deployed environment; run stlv deploy first")
            network = run.capture_network_session(click.echo)
        try:
            network.start()
            if network.description is not None:
                network.wait()
        finally:
            network.close()
    except KeyboardInterrupt:
        click.echo("Tunnel stopped.")
    except (
        HelperError,
        NativeAssetError,
        OSError,
        RuntimeError,
        ValueError,
        StelvioProjectError,
        StelvioValidationError,
        StateLockedError,
    ) as error:
        raise click.ClickException(str(error)) from error
