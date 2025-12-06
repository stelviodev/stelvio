import os

from pulumi.automation import CommandError
from rich.console import Console

from stelvio.aws.function.dependencies import (
    clean_function_active_dependencies_caches_file,
    clean_function_stale_dependency_caches,
)
from stelvio.aws.layer import (
    clean_layer_active_dependencies_caches_file,
    clean_layer_stale_dependency_caches,
)
from stelvio.command_run import CommandRun, force_unlock
from stelvio.project import get_last_deployed_app_name, save_deployed_app_name
from stelvio.pulumi import _show_simple_error, print_operation_header
from stelvio.rich_deployment_handler import RichDeploymentHandler

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


def run_deploy(env: str, confirmed_new_app: bool = False, show_unchanged: bool = False) -> None:
    from stelvio.exceptions import AppRenamedError

    status = console.status("Loading app...")
    status.start()
    _reset_cache_tracking()

    with CommandRun(env, lock_as="deploy") as run:
        last_deployed_name = get_last_deployed_app_name()
        if last_deployed_name and last_deployed_name != run.app_name and not confirmed_new_app:
            status.stop()
            raise AppRenamedError(last_deployed_name, run.app_name)

        status.stop()
        print_operation_header("Deploying", run.app_name, env)
        handler = RichDeploymentHandler(run.app_name, env, "deploy", show_unchanged=show_unchanged)
        error_exc: CommandError | None = None
        try:
            run.stack.up(on_event=handler.handle_event)
            _clean_stale_caches()
            save_deployed_app_name(run.app_name)
        except CommandError as e:
            error_exc = e
            _show_simple_error(e, handler)

        run.push_state()
        run.create_state_snapshot()
        run.complete_update(errors=[str(error_exc)] if error_exc else None)

        if error_exc:
            _handle_error(error_exc)

        handler.show_completion(run.stack.outputs())


def run_refresh(env: str) -> None:
    status = console.status("Loading app...")
    status.start()

    with CommandRun(env, lock_as="refresh") as run:
        status.stop()
        print_operation_header("Refreshing", run.app_name, env)
        handler = RichDeploymentHandler(run.app_name, env, "refresh")
        error_exc: CommandError | None = None
        try:
            run.stack.refresh(on_event=handler.handle_event)
        except CommandError as e:
            error_exc = e
            _show_simple_error(e, handler)

        run.push_state()
        run.complete_update(errors=[str(error_exc)] if error_exc else None)

        if error_exc:
            _handle_error(error_exc)

        handler.show_completion()


def run_destroy(env: str) -> None:
    status = console.status("Loading app...")
    status.start()

    with CommandRun(env, lock_as="destroy") as run:
        status.stop()
        print_operation_header("Destroying", run.app_name, env)
        handler = RichDeploymentHandler(run.app_name, env, "destroy")
        error_exc: CommandError | None = None
        try:
            run.stack.destroy(on_event=handler.handle_event)
        except CommandError as e:
            error_exc = e
            _show_simple_error(e, handler)

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

        handler.show_completion()


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
