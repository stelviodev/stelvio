"""Standalone CLI. No command publishes acceptance to GitHub automatically."""

from __future__ import annotations

import json
import os
import tempfile
import traceback
from pathlib import Path

import click

from . import __version__, candidate, process, recovery
from .progress import heartbeat
from .report import generate
from .runner import environment, execute_stages, stage_commands, verify
from .stages import STAGES, select
from .store import SCHEMA, Store, event, now


@click.group()
@click.version_option(__version__)
def main():
    """Run acceptance against a frozen Stelvio commit and local Traforo build."""
    os.umask(0o077)


@main.command()
def stages():
    """List stages, groups, dependencies, estimates, and default deadlines."""
    for stage in STAGES.values():
        click.echo(
            f"{stage.name:18} group={stage.group:13} requires={','.join(stage.needs) or '-'} "
            f"estimate={stage.estimate} timeout={stage.timeout}s"
        )
    click.echo("Cleanup, independent audit, and reporting always run after possible live effects.")


def timeouts(values):
    result = {}
    for value in values:
        try:
            name, seconds = value.split("=", 1)
            seconds = int(seconds)
            if name not in STAGES or seconds <= 0:
                raise ValueError
        except ValueError as error:
            raise click.BadParameter("Use --timeout STAGE=POSITIVE_SECONDS") from error
        result[name] = seconds
    return result


@main.command()
@click.argument("commit")
@click.option("--patch", multiple=True, help="Apply a single-parent commit delta, in order.")
@click.option("--repo", type=click.Path(path_type=Path, exists=True), default=".")
@click.option("--only", multiple=True, help="Include a stage or group; repeat as needed.")
@click.option("--skip", multiple=True, help="Exclude a stage or group; repeat as needed.")
@click.option(
    "--run-dir", type=click.Path(path_type=Path), help="New directory; retained after the run."
)
@click.option("--profile", default="default", show_default=True)
@click.option("--region", default="us-east-1", show_default=True)
@click.option("--expected-account")
@click.option("--go", default="1.27.3", show_default=True)
@click.option("--timeout", multiple=True, help="Override a stage deadline: STAGE=SECONDS.")
@click.option("--yes", is_flag=True, help="Live execution also requires --expected-account.")
@click.option("--dry-run", is_flag=True, help="Print the selected graph without creating a run.")
def run(
    commit,
    patch,
    repo,
    only,
    skip,
    run_dir,
    profile,
    region,
    expected_account,
    go,
    timeout,
    yes,
    dry_run,
):
    """Test COMMIT in an isolated checkout; default to the full acceptance round."""
    try:
        selected = select(only, skip)
        configuration = {
            "profile": profile,
            "region": region,
            "expected_account": expected_account,
            "go": go,
            "timeouts": timeouts(timeout),
        }
        if dry_run:
            click.echo(
                json.dumps(
                    {
                        "commit": candidate.git(repo, "rev-parse", f"{commit}^{{commit}}"),
                        "patches": [
                            candidate.git(repo, "rev-parse", f"{p}^{{commit}}") for p in patch
                        ],
                        "commands": {
                            name: [
                                {"argv": argv, "cwd": str(cwd)}
                                for argv, cwd in stage_commands(name, Path("<RUN_DIRECTORY>"), {})
                            ]
                            for name in selected
                        },
                        "stages": selected,
                        "configuration": configuration,
                        "finalization": ["cleanup", "independent-audit", "report"],
                    },
                    indent=2,
                )
            )
            return
        if run_dir:
            run_dir = run_dir.absolute()
            run_dir.mkdir(mode=0o700, parents=False, exist_ok=False)
        else:
            run_dir = Path(tempfile.mkdtemp(prefix="traforo-acceptance-")).resolve()
        store = Store(run_dir)
        data = {
            "schema": SCHEMA,
            "created": now(),
            "tool_version": __version__,
            "selected": selected,
            "configuration": configuration,
            "results": {},
            "artifacts": {},
        }
        data["tool_fingerprint"] = candidate.freeze_tool(run_dir)
        store.save(data)
        click.echo(f"Run directory: {run_dir}")
        with store.locked():
            try:
                data["identity"] = candidate.checkout(
                    repo.resolve(), commit, patch, run_dir / "source"
                )
                store.save(data)
            except Exception as error:
                event(
                    run_dir,
                    "failure",
                    stage="checkout",
                    error=str(error),
                    trace=traceback.format_exc(),
                    stderr=str(getattr(error, "stderr", "")),
                )
                generate(run_dir)
                raise
            report = execute_stages(run_dir, yes)
        if report["outcome"] == "failed-or-incomplete":
            raise click.ClickException(
                f"Acceptance failed or is incomplete: {run_dir / 'report.md'}"
            )
    except (ValueError, RuntimeError, OSError) as error:
        raise click.ClickException(str(error)) from error


@main.command()
@click.argument("run_directory", type=click.Path(path_type=Path, exists=True))
@click.option("--yes", is_flag=True)
def resume(run_directory, yes):
    """Continue pending stages. Failed or interrupted stages require a new run."""
    store = Store(run_directory)
    with store.locked():
        verify(store.root, store.data)
        if any(result["outcome"] != "passed" for result in store.data["results"].values()):
            raise click.ClickException(
                "Failed/interrupted run: use cleanup/audit, then start a new run"
            )
        report = execute_stages(store.root, yes)
        if report["outcome"] == "failed-or-incomplete":
            raise click.ClickException("Acceptance is incomplete; see report.md")


@main.command()
@click.argument("run_directory", type=click.Path(path_type=Path, exists=True))
@click.option("--yes", is_flag=True)
def cleanup(run_directory, yes):
    """Resume recovery of registered proofs and the original helper installation."""
    store = Store(run_directory)
    with store.locked():
        data = store.data
        verify(store.root, data)
        if not data.get("live_authorized"):
            click.echo("No authorized live effects were registered.")
            return
        if not yes:
            click.confirm("Recover this run's owned resources and original helper?", abort=True)
        env = environment(store.root, data)
        execute = lambda stage, argv, cwd, env, timeout: process.run(  # noqa: E731
            store.root, stage, argv, cwd, env, timeout
        )
        with heartbeat(store.root, "cleanup"):
            recovery.recover(store.root, execute, env)
            result = recovery.audit(store.root, execute, env)
        generate(store.root)
        if result["outcome"] != "certified":
            raise click.ClickException("Cleanup not certified; retain all evidence")


@main.command()
@click.argument("run_directory", type=click.Path(path_type=Path, exists=True))
def audit(run_directory):
    """Read-only independent cleanup audit; does not erase earlier failed audits."""
    store = Store(run_directory)
    with store.locked():
        verify(store.root, store.data)
        env = environment(store.root, store.data)
        execute = lambda stage, argv, cwd, env, timeout: process.run(  # noqa: E731
            store.root, stage, argv, cwd, env, timeout
        )
        with heartbeat(store.root, "audit"):
            result = recovery.audit(store.root, execute, env)
        generate(store.root)
        if result["outcome"] != "certified":
            raise click.ClickException("Independent audit is unknown or resources remain")
        click.echo("Cleanup certified. Earlier test failures are unchanged.")


@main.command()
@click.argument("run_directory", type=click.Path(path_type=Path, exists=True))
def report(run_directory):
    """Regenerate deterministic JSON and Markdown from retained evidence."""
    store = Store(run_directory)
    with store.locked():
        result = generate(store.root)
    click.echo(f"{result['outcome']}: {store.root / 'report.md'}")


if __name__ == "__main__":
    main()
