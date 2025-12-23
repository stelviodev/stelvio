import os

from pulumi.automation import CommandError
from rich.console import Console

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
from stelvio.command_run import CommandRun, force_unlock
from stelvio.pulumi import _show_simple_error, print_operation_header
from stelvio.rich_deployment_handler import RichDeploymentHandler
from stelvio.state_ops import (
    Mutation,
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


def run_diff(env: str, show_unchanged: bool = False) -> None:
    status = console.status("Loading app...")
    status.start()
    _reset_cache_tracking()

    with CommandRun(env) as run:
        status.stop()
        print_operation_header("Diff for", run.app_name, env)
        handler = RichDeploymentHandler(
            run.app_name, env, "preview", show_unchanged=show_unchanged
        )
        try:
            run.stack.preview(on_event=handler.handle_event)
            _clean_stale_caches()
            handler.show_completion(run.stack.outputs())
        except CommandError as e:
            _show_simple_error(e, handler)
            _handle_error(e)


def run_deploy(env: str, show_unchanged: bool = False) -> None:
    status = console.status("Loading app...")
    status.start()
    _reset_cache_tracking()

    with CommandRun(env, lock_as="deploy") as run:
        status.stop()
        operation_str = f"Deploying {'' if run.has_deployed else 'NEW '} app"
        print_operation_header(operation_str, run.app_name, env)
        display_handler = RichDeploymentHandler(
            run.app_name, env, "deploy", show_unchanged=show_unchanged
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

        display_handler.show_completion(run.stack.outputs())


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

        display_handler.show_completion(run.stack.outputs())
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


def run_refresh(env: str) -> None:
    status = console.status("Loading app...")
    status.start()

    with CommandRun(env, lock_as="refresh") as run:
        status.stop()
        if not _has_deployed_app(run, "refresh"):
            return
        print_operation_header("Refreshing", run.app_name, env)
        display_handler = RichDeploymentHandler(run.app_name, env, "refresh")
        error_exc: CommandError | None = None
        run.start_partial_push()
        try:
            run.stack.refresh(on_event=run.event_handler(display=display_handler))
        except CommandError as e:
            error_exc = e
            _show_simple_error(e, display_handler)
        finally:
            run.stop_partial_push()

        run.push_state()
        run.complete_update(errors=[str(error_exc)] if error_exc else None)

        if error_exc:
            _handle_error(error_exc)

        display_handler.show_completion()


def _confirm_destroy(env: str) -> bool:
    """Ask user to confirm destroy by typing environment name. Returns True if confirmed."""
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


def run_destroy(env: str, skip_confirm: bool = False) -> None:
    status = console.status("Loading app...")
    status.start()

    with CommandRun(env, lock_as="destroy") as run:
        status.stop()
        if not _has_deployed_app(run, "destroy"):
            return

        if not skip_confirm and not _confirm_destroy(env):
            return

        print_operation_header("Destroying", run.app_name, env)
        display_handler = RichDeploymentHandler(run.app_name, env, "destroy")
        error_exc: CommandError | None = None
        run.start_partial_push()
        try:
            run.stack.destroy(on_event=run.event_handler(display=display_handler))
        except CommandError as e:
            error_exc = e
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
            _handle_error(error_exc)

        display_handler.show_completion()


def run_unlock(env: str) -> dict | None:
    """Returns lock info if lock existed, None otherwise."""
    status = console.status("Loading app...")
    status.start()
    lock_info = force_unlock(env)
    status.stop()
    return lock_info


def run_outputs(env: str, json_output: bool = False) -> None:
    status = console.status("Loading app...")
    status.start()

    with CommandRun(env) as run:
        status.stop()
        if not _has_deployed_app(run, "show outputs"):
            return
        if not json_output:
            print_operation_header("Outputs for", run.app_name, env)
        try:
            stack_outputs = run.stack.outputs()
            if stack_outputs:
                if json_output:
                    console.print_json(
                        data={key: value.value for key, value in stack_outputs.items()}
                    )
                else:
                    for key, value in stack_outputs.items():
                        console.print(f"[cyan]{key}[/cyan]: {value.value}")
            elif not json_output:
                console.print(f"[yellow]No outputs found for {run.app_name} in {env}[/yellow]")
        except CommandError as e:
            console.print(f"[red]{e!s}[/red]")
            _handle_error(e)


def _has_deployed_app(run: CommandRun, action: str) -> bool:
    """Check if there's a deployed app we can work with. Prints message if not."""
    if run.has_deployed:
        return True
    console.print(f"[yellow]No app deployed yet. Nothing to {action}.[/yellow]")
    return False


def run_state_list(env: str) -> None:
    """List all resources in state."""
    status = console.status("Loading app...")
    status.start()

    with CommandRun(env, state_only=True) as run:
        status.stop()
        if not _has_deployed_app(run, "list"):
            return
        state = run.load_state()
        resources = list_resources(state)
        if not resources:
            console.print("[yellow]No resources in state[/yellow]")
            return

        console.print(f"[bold]Resources ({len(resources)}):[/bold]\n")
        for r in resources:
            console.print(f"  [cyan]{r.name}[/cyan]")
            console.print(f"    Type: {r.type}")
            if r.parent:
                parent_name = r.parent.split("::")[-1]
                console.print(f"    Parent: {parent_name}")
            if r.dependencies:
                dep_names = [d.split("::")[-1] for d in r.dependencies]
                console.print(f"    Dependencies: {', '.join(dep_names)}")
            console.print()


def _confirm_mutations(mutations: list[Mutation]) -> bool:
    """Show pending changes and ask for confirmation. Returns True if confirmed."""
    removed_count = sum(1 for m in mutations if m.action == "remove_resource")

    console.print("\n[bold]Pending changes:[/bold]")
    for m in mutations:
        console.print(f"  • {m.detail}")

    if removed_count > 0:
        console.print(
            f"\n[yellow]Warning: {removed_count} resource(s) will be removed from state.[/yellow]"
        )
        console.print("[yellow]These may still exist in AWS but won't be managed.[/yellow]")
        console.print("[yellow]Delete manually from AWS console if no longer needed.[/yellow]")

    console.print()
    response = console.input("[bold]Apply these changes? (y/n):[/bold] ")
    return response.lower() == "y"


def run_state_remove(env: str, name: str) -> None:
    """Remove resource from state by name."""
    status = console.status("Loading app...")
    status.start()

    with CommandRun(env, lock_as="state-remove", state_only=True) as run:
        status.stop()
        if not _has_deployed_app(run, "remove"):
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
        if not _has_deployed_app(run, "repair"):
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
