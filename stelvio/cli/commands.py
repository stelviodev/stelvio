import os

from pulumi.automation import CommandError
from rich.console import Console
from rich.status import Status

from stelvio import context
from stelvio.aws.function.dependencies import (
    clean_function_active_dependencies_caches_file,
    clean_function_stale_dependency_caches,
)
from stelvio.aws.layer import (
    clean_layer_active_dependencies_caches_file,
    clean_layer_stale_dependency_caches,
)
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


def _reset_cache_tracking() -> None:
    clean_function_active_dependencies_caches_file()
    clean_layer_active_dependencies_caches_file()


def _clean_stale_caches() -> None:
    clean_function_stale_dependency_caches()
    clean_layer_stale_dependency_caches()


def _handle_error(error: CommandError) -> None:
    if os.getenv("STLV_DEBUG", "0") == "1":
        raise error
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
    error: CommandError,
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


def _start_loading(*, enabled: bool) -> Status | None:
    if not enabled:
        return None
    status = console.status("Loading app...")
    status.start()
    return status


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

    action = {"outputs": "show outputs", "state_list": "list"}.get(operation, operation)
    message = f"No app deployed yet. Nothing to {action}."

    if json_output:
        if operation == "outputs":
            console.print_json(data={})
        elif operation == "state_list":
            console.print_json(data={"components": []})
        else:
            handler = RichDeploymentHandler(run.app_name, env, operation, live_enabled=False)
            print_json_summary(console, handler, outputs={}, message=message)
    elif stream_output:
        handler = RichDeploymentHandler(run.app_name, env, operation, live_enabled=False)
        print_stream_summary(handler, outputs={}, message=message)
    else:
        console.print(f"[yellow]{message}[/yellow]")
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
        console.print(f"[red]{error!s}[/red]")
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


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def run_diff(
    env: str, show_unchanged: bool = False, compact: bool = False, *, json_output: bool = False
) -> None:
    status = _start_loading(enabled=not json_output)
    _reset_cache_tracking()

    with CommandRun(env) as run:
        if status:
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
            run.stack.preview(on_event=handler.handle_event)
            _clean_stale_caches()
            if json_output:
                print_json_summary(console, handler)
            else:
                handler.show_completion()
        except CommandError as e:
            if json_output:
                print_json_summary(
                    console, handler, status="failed", exit_code=1, fallback_error=str(e)
                )
            else:
                _show_simple_error(e, handler)
            _handle_error(e)


def run_deploy(
    env: str,
    show_unchanged: bool = False,
    *,
    json_output: bool = False,
    stream_output: bool = False,
) -> None:
    status = _start_loading(enabled=not (json_output or stream_output))
    _reset_cache_tracking()

    with CommandRun(env, lock_as="deploy") as run:
        if status:
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
        error_exc: CommandError | None = None
        run.start_partial_push()
        try:
            run.stack.up(on_event=run.event_handler(display=display_handler))
            _clean_stale_caches()
        except CommandError as e:
            error_exc = e
            if not json_output and not stream_output:
                _show_simple_error(e, display_handler)
        finally:
            run.stop_partial_push()

        run.push_state()
        run.create_state_snapshot()
        run.complete_update(errors=[str(error_exc)] if error_exc else None)

        stack_outputs = _best_effort_outputs(run)
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


def run_dev(env: str, show_unchanged: bool = False) -> None:
    status = console.status("Loading app...")
    status.start()
    _reset_cache_tracking()

    with CommandRun(env, lock_as="dev-mode", dev_mode=True) as run:
        status.stop()
        operation_str = f"Deploying {'' if run.has_deployed else 'NEW '}app in DEV MODE"
        print_operation_header(operation_str, run.app_name, env)
        display_handler = RichDeploymentHandler(
            run.app_name, env, "deploy", show_unchanged=show_unchanged, dev_mode=True
        )
        error_exc: CommandError | None = None
        run.start_partial_push()
        try:
            run.stack.up(on_event=run.event_handler(display=display_handler))
            _clean_stale_caches()
        except CommandError as e:
            error_exc = e
            _show_simple_error(e, display_handler)
        finally:
            run.stop_partial_push()

        run.push_state()
        run.create_state_snapshot()
        run.complete_update(errors=[str(error_exc)] if error_exc else None)

        if error_exc:
            _handle_error(error_exc)

        grouped = group_outputs(run.load_state(), run.stack.outputs())
        display_handler.show_completion(output_lines=format_outputs(grouped))
    # TODO: Here lock is released but maybe we could  find a way to keep lock until dev mode is
    #       finished.

    console.print("\n[bold green]✓[/bold green] Stelvio app deployed in DEV MODE.")
    console.print("Running local dev server now...")

    run_bridge_server(
        region=context().aws.region,
        profile=context().aws.profile,
        app_name=context().name,
        env=env,
    )


def run_refresh(env: str, *, json_output: bool = False) -> None:
    status = _start_loading(enabled=not json_output)

    with CommandRun(env, lock_as="refresh") as run:
        if status:
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
        error_exc: CommandError | None = None
        run.start_partial_push()
        try:
            run.stack.refresh(on_event=run.event_handler(display=display_handler))
        except CommandError as e:
            error_exc = e
            if not json_output:
                _show_simple_error(e, display_handler)
        finally:
            run.stop_partial_push()

        run.push_state()
        run.complete_update(errors=[str(error_exc)] if error_exc else None)

        if error_exc:
            _show_failed_result(display_handler, error_exc, json_output=json_output, outputs={})
            _handle_error(error_exc)

        _show_result(display_handler, json_output=json_output, outputs={})


def run_destroy(
    env: str, skip_confirm: bool = False, *, json_output: bool = False, stream_output: bool = False
) -> None:
    status = _start_loading(enabled=not (json_output or stream_output))

    with CommandRun(env, lock_as="destroy") as run:
        if status:
            status.stop()
        if _handle_not_deployed(
            run, json_output=json_output, stream_output=stream_output, env=env, operation="destroy"
        ):
            return

        if not skip_confirm and not _confirm_destroy(env):
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
        error_exc: CommandError | None = None
        run.start_partial_push()
        try:
            run.stack.destroy(on_event=run.event_handler(display=display_handler))
        except CommandError as e:
            error_exc = e
            if not json_output and not stream_output:
                _show_simple_error(e, display_handler)
        finally:
            run.stop_partial_push()

        run.push_state()

        # Delete snapshots only if all resources were destroyed
        deployment = run.stack.export_stack()
        resources = deployment.deployment.get("resources", [])
        actual_resources = [r for r in resources if r.get("type") != "pulumi:pulumi:Stack"]
        if len(actual_resources) == 0:
            run.delete_snapshots()

        run.complete_update(errors=[str(error_exc)] if error_exc else None)

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


def run_unlock(env: str) -> dict | None:
    """Returns lock info if lock existed, None otherwise."""
    status = console.status("Loading app...")
    status.start()
    lock_info = force_unlock(env)
    status.stop()
    return lock_info


def run_outputs(
    env: str,
    *,
    json_output: bool = False,
) -> None:
    status = _start_loading(enabled=not json_output)

    with CommandRun(env) as run:
        if status:
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
    status = _start_loading(enabled=not json_output)

    with CommandRun(env, state_only=True) as run:
        if status:
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
    status = console.status("Loading app...")
    status.start()

    with CommandRun(env, lock_as="state-remove", state_only=True) as run:
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
    status = console.status("Loading app...")
    status.start()

    with CommandRun(env, lock_as="state-repair", state_only=True) as run:
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
