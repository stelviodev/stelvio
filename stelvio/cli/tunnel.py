"""Native helper setup and host recovery commands, independent of an app."""

from collections.abc import Callable

import click

from stelvio.tunnel.assets import NativeAssetError
from stelvio.tunnel.helper_client import HelperError, NativeHelper
from stelvio.tunnel.host_trust import HostIdentity, HostTrustError, fingerprint, locked_host_trust
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


@tunnel.group("trust")
def trust() -> None:
    """Manage persistent SSH host-key trust without changing AWS or the helper."""


@trust.command("reset")
@click.option("--partition", default="aws", show_default=True)
@click.option("--account", required=True)
@click.option("--region", required=True)
@click.option("--instance-id", required=True)
def reset_trust(partition: str, account: str, region: str, instance_id: str) -> None:
    """Forget one host key after explicit confirmation. Stop its tunnels first."""
    from threading import Event  # noqa: PLC0415 - no networking workers at import time

    try:
        target = HostIdentity(partition, account, region, instance_id)
        with locked_host_trust(target, Event()) as entry:
            if entry.key is None:
                click.echo(f"No SSH host key is stored for {target.label}.")
                return
            expected = fingerprint(entry.key)
        click.echo(f"Instance: {target.label}\nStored fingerprint: {expected}")
        click.confirm(
            "Stop all tunnels for this instance. "
            "Forget this key and trust the next SSM connection?",
            abort=True,
        )
        with locked_host_trust(target, Event()) as entry:
            entry.reset(expected)
        click.echo("SSH host trust removed. The next connection will establish a new pin.")
    except (HostTrustError, NativeAssetError, OSError) as error:
        raise click.ClickException(str(error)) from error


@tunnel.command("start")
@click.argument("env", required=False)
def start(env: str | None) -> None:
    """Connect to deployed private and isolated subnets without deploying the app."""
    from stelvio.cli import determine_env  # noqa: PLC0415 - avoid the CLI registration cycle
    from stelvio.command_run import CommandRun  # noqa: PLC0415 - load project state on demand
    from stelvio.exceptions import (  # noqa: PLC0415 - keep admin commands app-independent
        StateLockedError,
        StelvioProjectError,
        StelvioValidationError,
    )

    session = None
    interrupted = False
    try:
        environment = determine_env(env, require_explicit_in_ci=True, command_name="tunnel start")
        with CommandRun(environment, state_only=True) as run:
            if not run.has_deployed:
                raise click.ClickException(
                    f"No deployed environment '{environment}'; run stlv deploy first"
                )
            session = run.capture_network_session(click.echo)

        session.start()
        if session.description is not None:
            session.wait()
    except KeyboardInterrupt:
        interrupted = True
    except (
        HelperError,
        NativeAssetError,
        PluginAssetError,
        OSError,
        RuntimeError,
        ValueError,
        StelvioProjectError,
        StelvioValidationError,
        StateLockedError,
    ) as error:
        raise click.ClickException(str(error)) from error
    finally:
        if session is not None:
            try:
                session.close()
            except Exception as error:
                raise click.ClickException(str(error)) from error

    if interrupted:
        click.echo("Tunnel stopped.")


@tunnel.command("diagnose")
@click.option("--json", "as_json", is_flag=True, help="Print one JSON diagnostic report.")
def diagnose(as_json: bool) -> None:
    """Read local tunnel and network state without changing networking."""
    import json  # noqa: PLC0415
    import os  # noqa: PLC0415

    from rich.console import Console  # noqa: PLC0415

    from stelvio.tunnel.diagnostics.report import collect, render  # noqa: PLC0415

    if hasattr(os, "geteuid") and not os.geteuid():
        raise click.ClickException("Run tunnel diagnose as your ordinary user")
    try:
        report = collect()
        if as_json:
            click.echo(json.dumps(report, allow_nan=False, indent=2))
        else:
            render(report, Console())
    except (OSError, RuntimeError, ValueError, TypeError) as error:
        raise click.ClickException("Cannot create the tunnel diagnostic report") from error
