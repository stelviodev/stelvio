import logging
import os
import traceback
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from typing import NoReturn

from pulumi.automation import CommandError
from rich.console import Console
from rich.markup import escape
from rich.status import Status

from stelvio import context, signals
from stelvio._signals import (
    _cancel_confirmation,
    _current,
    _diagnostic,
    _operation,
    _record_failure,
    _record_result,
    _recover,
    _run_phase,
)
from stelvio.aws._packaging.dependencies import clean_stale_dependency_caches
from stelvio.bridge.local.listener import run_bridge_server
from stelvio.cli.json_output import (
    emit_stream_start,
    print_json_error,
    print_json_summary,
    print_stream_error,
    print_stream_summary,
    stream_writer,
)
from stelvio.cli.state_rendering import format_state_tree_lines
from stelvio.command_run import CommandRun, force_unlock
from stelvio.provider import ProviderStore
from stelvio.pulumi import _show_simple_error, print_operation_header
from stelvio.rich_deployment_handler import RichDeploymentHandler
from stelvio.stack_outputs import (
    build_outputs_json,
    format_outputs,
    group_outputs,
)
from stelvio.state_ops import (
    Mutation,
    build_state_tree,
    build_state_tree_json,
    find_resources_by_name,
    list_resources,
    remove_resource,
    repair_state,
)

console = Console()
logger = logging.getLogger(__name__)


def _clean_stale_caches() -> None:
    # Runs before the final state push: a failed cleanup (another stlv run on this project
    # removing the same dirs) must not skip it.
    try:
        clean_stale_dependency_caches()
    except OSError:
        logger.warning("Could not clean stale dependency caches", exc_info=True)


def _handle_error(error: CommandError) -> NoReturn:
    # Printed rather than re-raised so the exit still goes through `cli.main`'s os._exit.
    _record_failure(error)
    if os.getenv("STLV_DEBUG", "0") == "1":
        traceback.print_exception(error)
    raise SystemExit(1) from None


def _show_result(
    handler: RichDeploymentHandler,
    *,
    json_output: bool,
    stream_output: bool = False,
    outputs: dict[str, object],
    output_lines: list[str] | None = None,
) -> None:
    """Emit the final success output in the appropriate mode."""
    if (session := _current.get()) is not None and session.prepared:
        session.completion = lambda: _emit_result(
            handler,
            json_output=json_output,
            stream_output=stream_output,
            outputs=outputs,
            output_lines=output_lines,
        )

        def failed(error: BaseException) -> None:
            if json_output or stream_output:
                _show_failed_result(
                    handler,
                    error,
                    json_output=json_output,
                    stream_output=stream_output,
                    outputs=outputs,
                )
            else:
                _diagnostic(error, handler=session.handler)

        session.failed_completion = failed
        return
    _emit_result(
        handler,
        json_output=json_output,
        stream_output=stream_output,
        outputs=outputs,
        output_lines=output_lines,
    )


def _emit_result(
    handler: RichDeploymentHandler,
    *,
    json_output: bool,
    stream_output: bool,
    outputs: dict[str, object],
    output_lines: list[str] | None,
) -> None:
    if json_output:
        print_json_summary(console, handler, outputs=outputs)
    elif stream_output:
        print_stream_summary(handler, outputs=outputs)
    elif output_lines:
        handler.show_completion(output_lines=output_lines)
    else:
        handler.show_completion()


def _show_failed_result(
    handler: RichDeploymentHandler,
    error: BaseException,
    *,
    json_output: bool,
    stream_output: bool = False,
    outputs: dict[str, object],
) -> None:
    """Emit a failure summary in the appropriate output mode."""
    if json_output:
        print_json_summary(
            console,
            handler,
            status="failed",
            outputs=outputs,
            exit_code=1,
            fallback_error=str(error),
        )
    elif stream_output:
        print_stream_summary(
            handler, status="failed", outputs=outputs, exit_code=1, fallback_error=str(error)
        )


def _loading(*, enabled: bool = True) -> AbstractContextManager[Status]:
    """Spinner that stops on exit, so a failing CommandRun.__enter__ leaves no residue."""
    status = console.status("Loading app...")
    return status if enabled else nullcontext(status)


def _best_effort_outputs(run: CommandRun) -> dict[str, object]:
    try:
        grouped = group_outputs(run.load_state(), run.stack.outputs())
        return build_outputs_json(grouped)
    except CommandError:
        return {}


def _handle_not_deployed(
    run: CommandRun, *, json_output: bool, stream_output: bool, env: str, operation: str
) -> bool:
    """Handle the case when no app is deployed yet. Returns True if handled."""
    if run.has_deployed:
        return False

    _record_result(no_op=True)
    if operation in ("destroy", "refresh"):
        run.complete_update()

    action = {"outputs": "show outputs", "state_list": "list"}.get(operation, operation)
    message = f"No app deployed yet. Nothing to {action}."

    def emit() -> None:
        if json_output:
            if operation == "outputs":
                console.print_json(data={})
            elif operation == "state_list":
                console.print_json(data={"components": []})
            else:
                handler = RichDeploymentHandler(run.app_name, env, operation, live_enabled=False)
                print_json_summary(console, handler, outputs={}, message=message)
        elif stream_output:
            emit_stream_start(operation, run.app_name, env)
            handler = RichDeploymentHandler(run.app_name, env, operation, live_enabled=False)
            print_stream_summary(handler, outputs={}, message=message)
        else:
            console.print(f"[yellow]{message}[/yellow]")

    if (session := _current.get()) is not None and session.prepared:
        session.completion = emit
    else:
        emit()
    return True


def _handle_command_error(  # noqa: PLR0913
    *,
    json_output: bool,
    stream_output: bool,
    operation: str,
    app_name: str,
    env: str,
    error: CommandError,
) -> None:
    if json_output:
        print_json_error(
            console, operation=operation, app_name=app_name, env=env, error=str(error)
        )
    elif stream_output:
        print_stream_error(operation=operation, app_name=app_name, env=env, error=str(error))
    else:
        console.print(f"[red]{escape(str(error))}[/red]")
    _handle_error(error)


def _confirm_destroy(env: str) -> bool:
    """Ask user to confirm destroy by typing environment name."""
    console.print(
        f"About to [bold red]destroy all resources[/bold red] in [bold]{env}[/bold] environment."
    )
    console.print("[bold yellow]Warning:[/bold yellow] This action cannot be undone!")

    typed_env = console.input(f"Type the environment name '[bold]{env}[/bold]' to confirm: ")
    if typed_env != env:
        console.print(f"Environment name mismatch. Expected '{env}', got '{typed_env}'.")
        console.print("Destruction cancelled.")
        return False
    return True


def _confirm_mutations(mutations: list[Mutation]) -> bool:
    """Show pending state mutations and ask for confirmation."""
    removed_count = sum(1 for mutation in mutations if mutation.action == "remove_resource")

    console.print("\n[bold]Pending changes:[/bold]")
    for mutation in mutations:
        console.print(f"  • {mutation.detail}")

    if removed_count > 0:
        console.print(
            f"\n[yellow]Warning: {removed_count} resource(s) will be removed from state.[/yellow]"
        )
        console.print("[yellow]These may still exist in AWS but won't be managed.[/yellow]")
        console.print("[yellow]Delete manually from AWS console if no longer needed.[/yellow]")

    console.print()
    response = console.input("[bold]Apply these changes? (y/n):[/bold] ")
    return response.lower() == "y"


# Commands


def _perform_update(
    run: CommandRun,
    action: Callable[[], object],
    *,
    snapshot: bool = False,
    delete_snapshots: bool = False,
) -> CommandError | None:
    primary: BaseException | None = None
    session = _current.get()
    if session is not None:
        session.phase = "provision"
    try:
        run.start_partial_push()

        _run_phase("provision", lambda: _provision(run, action, capture_outputs=snapshot))
        if snapshot:
            _clean_stale_caches()
    except BaseException as error:
        primary = error
        _record_failure(error)

    def finish_record() -> None:
        failure = (session.failure or primary) if session is not None else primary
        message = (str(failure) or type(failure).__name__) if failure is not None else None
        run.complete_update(errors=[message] if message is not None else None)

    def remove_snapshots_if_empty() -> None:
        resources = run.stack.export_stack().deployment.get("resources", [])
        if not any(resource.get("type") != "pulumi:pulumi:Stack" for resource in resources):
            run.delete_snapshots()

    steps = [("checkpoint_stop", run.stop_partial_push), ("state_save", run.push_state)]
    if snapshot:
        steps.append(("snapshot_create", run.create_state_snapshot))
    if delete_snapshots:
        steps.append(("snapshot_delete", remove_snapshots_if_empty))
    steps.append(("update_complete", finish_record))
    primary = _recover(steps, primary)
    if primary is not None and not isinstance(primary, CommandError):
        raise primary
    return primary


def _provision(run: CommandRun, action: Callable[[], object], *, capture_outputs: bool) -> None:
    action()
    if capture_outputs:
        _record_result(outputs=_best_effort_outputs(run))


@_operation("diff")
def run_diff(
    env: str, show_unchanged: bool = False, compact: bool = False, *, json_output: bool = False
) -> None:
    with _loading(enabled=not json_output) as status, CommandRun(env) as run:
        status.stop()
        if not json_output:
            print_operation_header("Diff for", run.app_name, env)
        handler = RichDeploymentHandler(
            run.app_name,
            env,
            "preview",
            show_unchanged=show_unchanged,
            compact=compact,
            live_enabled=not json_output,
        )
        try:
            _run_phase("provision", lambda: run.stack.preview(on_event=handler.handle_event))
            _clean_stale_caches()
            _record_result()
            _show_result(handler, json_output=json_output, outputs={})
        except CommandError as e:
            if json_output:
                print_json_summary(
                    console, handler, status="failed", exit_code=1, fallback_error=str(e)
                )
            else:
                _show_simple_error(e, handler)
            _handle_error(e)


@_operation("deploy")
def run_deploy(
    env: str,
    show_unchanged: bool = False,
    *,
    json_output: bool = False,
    stream_output: bool = False,
) -> None:
    with (
        _loading(enabled=not (json_output or stream_output)) as status,
        CommandRun(env, lock_as="deploy") as run,
    ):
        status.stop()
        operation_str = f"Deploying {'NEW ' if not run.has_deployed else ''}app"
        if stream_output:
            emit_stream_start("deploy", run.app_name, env)
        elif not json_output:
            print_operation_header(operation_str, run.app_name, env)
        display_handler = RichDeploymentHandler(
            run.app_name,
            env,
            "deploy",
            show_unchanged=show_unchanged,
            live_enabled=not (json_output or stream_output),
            stream_writer=stream_writer() if stream_output else None,
        )
        error_exc = _perform_update(
            run,
            lambda: run.stack.up(on_event=run.event_handler(display=display_handler)),
            snapshot=True,
        )
        if error_exc and not json_output and not stream_output:
            _show_simple_error(error_exc, display_handler)

        stack_outputs = _best_effort_outputs(run)
        _record_result(outputs=stack_outputs)
        if error_exc:
            _show_failed_result(
                display_handler,
                error_exc,
                json_output=json_output,
                stream_output=stream_output,
                outputs=stack_outputs,
            )
            _handle_error(error_exc)

        grouped = group_outputs(run.load_state(), run.stack.outputs())
        _show_result(
            display_handler,
            json_output=json_output,
            stream_output=stream_output,
            outputs=stack_outputs,
            output_lines=format_outputs(grouped),
        )


@_operation("dev")
def run_dev(env: str, show_unchanged: bool = False) -> None:
    with _loading() as status, CommandRun(env, lock_as="dev-mode", dev_mode=True) as run:
        status.stop()
        operation_str = f"Deploying {'' if run.has_deployed else 'NEW '}app in DEV MODE"
        print_operation_header(operation_str, run.app_name, env)
        display_handler = RichDeploymentHandler(
            run.app_name, env, "deploy", show_unchanged=show_unchanged, dev_mode=True
        )
        error_exc = _perform_update(
            run,
            lambda: run.stack.up(on_event=run.event_handler(display=display_handler)),
            snapshot=True,
        )

        if error_exc:
            _show_simple_error(error_exc, display_handler)
            _handle_error(error_exc)

        grouped = group_outputs(run.load_state(), run.stack.outputs())
        _record_result(outputs=build_outputs_json(grouped))
    # TODO: Here lock is released but maybe we could  find a way to keep lock until dev mode is
    #       finished.

    if session := _current.get():
        session.emit(signals.after_deploy)
        session.operation = "dev"

    display_handler.show_completion(output_lines=format_outputs(grouped))

    console.print("\n[bold green]✓[/bold green] Stelvio app deployed in DEV MODE.")
    console.print("Running local dev server now...")

    run_bridge_server(
        region=ProviderStore.region(),
        profile=context().aws.profile,
        app_name=context().name,
        env=env,
    )


@_operation("refresh")
def run_refresh(env: str, *, json_output: bool = False) -> None:
    with _loading(enabled=not json_output) as status, CommandRun(env, lock_as="refresh") as run:
        status.stop()
        if _handle_not_deployed(
            run, json_output=json_output, stream_output=False, env=env, operation="refresh"
        ):
            return
        if not json_output:
            print_operation_header("Refreshing", run.app_name, env)
        display_handler = RichDeploymentHandler(
            run.app_name, env, "refresh", live_enabled=not json_output
        )
        error_exc = _perform_update(
            run, lambda: run.stack.refresh(on_event=run.event_handler(display=display_handler))
        )
        if error_exc and not json_output:
            _show_simple_error(error_exc, display_handler)

        if error_exc:
            _show_failed_result(display_handler, error_exc, json_output=json_output, outputs={})
            _handle_error(error_exc)

        _show_result(display_handler, json_output=json_output, outputs={})
        _record_result()


@_operation("destroy")
def run_destroy(
    env: str, skip_confirm: bool = False, *, json_output: bool = False, stream_output: bool = False
) -> None:
    if not skip_confirm and (session := _current.get()) is not None:
        session.phase = "confirmation"
    if not skip_confirm and not _confirm_destroy(env):
        _cancel_confirmation()
        return
    with (
        _loading(enabled=not (json_output or stream_output)) as status,
        CommandRun(env, lock_as="destroy") as run,
    ):
        status.stop()
        if _handle_not_deployed(
            run, json_output=json_output, stream_output=stream_output, env=env, operation="destroy"
        ):
            return

        if stream_output:
            emit_stream_start("destroy", run.app_name, env)
        elif not json_output:
            print_operation_header("Destroying", run.app_name, env)
        display_handler = RichDeploymentHandler(
            run.app_name,
            env,
            "destroy",
            live_enabled=not (json_output or stream_output),
            stream_writer=stream_writer() if stream_output else None,
        )
        error_exc = _perform_update(
            run,
            lambda: run.stack.destroy(on_event=run.event_handler(display=display_handler)),
            delete_snapshots=True,
        )
        if error_exc and not json_output and not stream_output:
            _show_simple_error(error_exc, display_handler)

        if error_exc:
            _show_failed_result(
                display_handler,
                error_exc,
                json_output=json_output,
                stream_output=stream_output,
                outputs={},
            )
            _handle_error(error_exc)

        _show_result(
            display_handler, json_output=json_output, stream_output=stream_output, outputs={}
        )
        _record_result()


def run_unlock(env: str) -> dict | None:
    """Returns lock info if lock existed, None otherwise."""
    with _loading():
        return force_unlock(env)


def run_outputs(
    env: str,
    *,
    json_output: bool = False,
) -> None:
    with _loading(enabled=not json_output) as status, CommandRun(env) as run:
        status.stop()
        if _handle_not_deployed(
            run, json_output=json_output, stream_output=False, env=env, operation="outputs"
        ):
            return
        if not json_output:
            print_operation_header("Outputs for", run.app_name, env)
        try:
            state = run.load_state()
            stack_outputs = run.stack.outputs()
            grouped = group_outputs(state, stack_outputs)
            if json_output:
                console.print_json(data=build_outputs_json(grouped))
            else:
                lines = format_outputs(grouped)
                if lines:
                    for line in lines:
                        console.print(line)
                else:
                    console.print(f"[yellow]No outputs found for {run.app_name} in {env}[/yellow]")
        except CommandError as e:
            _handle_command_error(
                json_output=json_output,
                stream_output=False,
                operation="outputs",
                app_name=run.app_name,
                env=env,
                error=e,
            )


def run_state_list(env: str, *, json_output: bool = False, show_outputs: bool = False) -> None:
    """List all resources in state."""
    with _loading(enabled=not json_output) as status, CommandRun(env, state_only=True) as run:
        status.stop()
        if _handle_not_deployed(
            run, json_output=json_output, stream_output=False, env=env, operation="state_list"
        ):
            return
        try:
            state = run.load_state()
            resources = list_resources(state)
            if not resources:
                if json_output:
                    console.print_json(data=build_state_tree_json(build_state_tree(state)))
                    return
                console.print("[yellow]No resources in state[/yellow]")
                return

            grouped_state = build_state_tree(state, include_outputs=show_outputs)
            if json_output:
                console.print_json(data=build_state_tree_json(grouped_state))
                return

            console.print(f"[bold]Resources ({len(resources)}):[/bold]\n")
            for line in format_state_tree_lines(grouped_state, width=max(console.size.width, 40)):
                console.print(line)
        except CommandError as e:
            _handle_command_error(
                json_output=json_output,
                stream_output=False,
                operation="state_list",
                app_name=getattr(run, "app_name", ""),
                env=env,
                error=e,
            )


def run_state_remove(env: str, name: str) -> None:
    """Remove resource from state by name."""
    with _loading() as status, CommandRun(env, lock_as="state-remove", state_only=True) as run:
        status.stop()
        if not run.has_deployed:
            console.print("[yellow]No app deployed yet. Nothing to remove.[/yellow]")
            run.complete_update()
            return
        state = run.load_state()

        # Check for ambiguous names
        matches = find_resources_by_name(state, name)
        if len(matches) == 0:
            console.print(f"[red]Resource not found: {name}[/red]")
            run.complete_update()
            return
        if len(matches) > 1:
            console.print(
                f"[red]Ambiguous name '{name}' matches {len(matches)} resources.[/red]\n"
                "Use full URN instead:\n" + "\n".join(f"  {r.urn}" for r in matches)
            )
            run.complete_update()
            return

        resource = matches[0]
        mutations = remove_resource(state, resource.urn)

        if not _confirm_mutations(mutations):
            console.print("[yellow]Cancelled.[/yellow]")
            run.complete_update()
            return

        run.push_state(state)
        run.complete_update()

        console.print(f"\n[bold green]✓ Applied {len(mutations)} changes.[/bold green]")


def run_state_repair(env: str) -> None:
    """Repair state by fixing orphans and broken dependencies."""
    with _loading() as status, CommandRun(env, lock_as="state-repair", state_only=True) as run:
        status.stop()
        if not run.has_deployed:
            console.print("[yellow]No app deployed yet. Nothing to repair.[/yellow]")
            run.complete_update()
            return
        state = run.load_state()

        mutations = repair_state(state)

        if not mutations:
            console.print("[green]✓ State is healthy, no repairs needed[/green]")
            run.complete_update()
            return

        if not _confirm_mutations(mutations):
            console.print("[yellow]Cancelled.[/yellow]")
            run.complete_update()
            return

        run.push_state(state)
        run.complete_update()

        console.print(f"\n[bold green]✓ Applied {len(mutations)} repairs.[/bold green]")
