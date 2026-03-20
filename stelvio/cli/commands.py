import json
import os
import sys
from collections.abc import Callable
from datetime import datetime
from textwrap import wrap
from types import SimpleNamespace

from pulumi.automation import CommandError, OutputValue
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
from stelvio.stack_outputs import (
    build_flat_outputs_json,
    build_grouped_outputs_json,
    format_flat_outputs,
    format_grouped_outputs,
    group_stack_outputs,
)
from stelvio.state_ops import (
    GroupedStateResources,
    Mutation,
    StateTreeNode,
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


def _json_outputs_data(
    stack_outputs: dict[str, OutputValue] | None,
    state: dict | None,
) -> dict[str, object]:
    if not stack_outputs:
        return {}
    return build_flat_outputs_json(stack_outputs, state)


def _print_json_summary(
    handler: RichDeploymentHandler,
    **summary_kwargs: object,
) -> None:
    console.print_json(data=handler.build_json_summary(**summary_kwargs))


def _write_json_line(data: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(data) + "\n")
    sys.stdout.flush()


def _stream_timestamp() -> str:
    return datetime.now().astimezone().isoformat()


def _stream_writer() -> Callable[[dict[str, object]], None]:
    return _write_json_line


def _emit_stream_start(operation: str, app_name: str, env: str) -> None:
    _write_json_line(
        {
            "event": "start",
            "operation": operation,
            "app": app_name,
            "env": env,
            "timestamp": _stream_timestamp(),
        }
    )


def _print_stream_summary(
    handler: RichDeploymentHandler,
    **summary_kwargs: object,
) -> None:
    payload = handler.build_json_summary(**summary_kwargs)
    payload["event"] = "summary"
    payload["timestamp"] = _stream_timestamp()
    _write_json_line(payload)


def _print_json_error(
    *,
    operation: str,
    app_name: str,
    env: str,
    error: str,
    exit_code: int = 1,
) -> None:
    console.print_json(
        data={
            "operation": operation,
            "app": app_name,
            "env": env,
            "timestamp": _stream_timestamp(),
            "status": "failed",
            "exit_code": exit_code,
            "errors": [{"message": error}],
        }
    )


def _print_stream_error(
    *,
    operation: str,
    app_name: str,
    env: str,
    error: str,
    exit_code: int = 1,
) -> None:
    _write_json_line(
        {
            "event": "error",
            "operation": operation,
            "app": app_name,
            "env": env,
            "timestamp": _stream_timestamp(),
            "status": "failed",
            "exit_code": exit_code,
            "errors": [{"message": error}],
        }
    )


def _best_effort_outputs_data(run: CommandRun) -> dict[str, object]:
    try:
        return _json_outputs_data(run.stack.outputs(), run.load_state())
    except CommandError:
        return {}


def _print_no_outputs_message(app_name: str, env: str, component_name: str | None = None) -> None:
    if component_name:
        console.print(
            f"[yellow]No outputs found for component '{component_name}' in "
            f"{app_name} → {env}[/yellow]"
        )
    else:
        console.print(f"[yellow]No outputs found for {app_name} in {env}[/yellow]")


def _print_json_outputs(
    stack_outputs: dict[str, OutputValue],
    state: dict | None,
    *,
    grouped: bool,
    component_name: str | None,
) -> None:
    grouped_outputs = group_stack_outputs(stack_outputs, state, component_name=component_name)
    data = (
        build_grouped_outputs_json(grouped_outputs)
        if grouped
        else build_flat_outputs_json(stack_outputs, state, component_name=component_name)
    )
    console.print_json(data=data)


def _print_empty_json_outputs() -> None:
    console.print_json(data={})


def _print_empty_json_state() -> None:
    console.print_json(data={"components": []})


def _start_loading_status(*, enabled: bool) -> object | None:
    if not enabled:
        return None
    status = console.status("Loading app...")
    status.start()
    return status


def _stop_loading_status(status: object | None) -> None:
    if status is not None:
        status.stop()


def _print_human_outputs(
    stack_outputs: dict[str, OutputValue],
    state: dict | None,
    *,
    component_name: str | None,
    app_name: str,
    env: str,
) -> None:
    grouped_outputs = group_stack_outputs(stack_outputs, state, component_name=component_name)
    lines = format_grouped_outputs(grouped_outputs) or format_flat_outputs(
        stack_outputs,
        state,
        component_name=component_name,
    )
    if lines:
        for line in lines:
            console.print(line)
        return

    _print_no_outputs_message(app_name, env, component_name)


def run_diff(
    env: str,
    show_unchanged: bool = False,
    compact: bool = False,
    *,
    json_output: bool = False,
) -> None:
    status = _start_loading_status(enabled=not json_output)
    _reset_cache_tracking()

    with CommandRun(env) as run:
        _stop_loading_status(status)
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
                _print_json_summary(handler)
            else:
                handler.show_completion()
        except CommandError as e:
            if json_output:
                _print_json_summary(
                    handler,
                    status="failed",
                    exit_code=1,
                    fallback_error=str(e),
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
    status = _start_loading_status(enabled=not (json_output or stream_output))
    _reset_cache_tracking()

    with CommandRun(env, lock_as="deploy") as run:
        _stop_loading_status(status)
        operation_str = f"Deploying {'' if run.has_deployed else 'NEW '} app"
        if stream_output:
            _emit_stream_start("deploy", run.app_name, env)
        elif not json_output:
            print_operation_header(operation_str, run.app_name, env)
        display_handler = RichDeploymentHandler(
            run.app_name,
            env,
            "deploy",
            show_unchanged=show_unchanged,
            live_enabled=not (json_output or stream_output),
            stream_writer=_stream_writer() if stream_output else None,
        )
        error_exc: CommandError | None = None
        run.start_partial_push()
        try:
            run.stack.up(on_event=run.event_handler(display=display_handler))
            _clean_stale_caches()
        except CommandError as e:
            error_exc = e
            if not json_output:
                _show_simple_error(e, display_handler)
        finally:
            run.stop_partial_push()

        run.push_state()
        run.create_state_snapshot()
        run.complete_update(errors=[str(error_exc)] if error_exc else None)

        stack_outputs = _best_effort_outputs_data(run)
        if error_exc:
            if json_output:
                _print_json_summary(
                    display_handler,
                    status="failed",
                    outputs=stack_outputs,
                    exit_code=1,
                    fallback_error=str(error_exc),
                )
            elif stream_output:
                _print_stream_summary(
                    display_handler,
                    status="failed",
                    outputs=stack_outputs,
                    exit_code=1,
                    fallback_error=str(error_exc),
                )
            _handle_error(error_exc)

        grouped_outputs = group_stack_outputs(run.stack.outputs(), run.load_state())
        if json_output:
            _print_json_summary(display_handler, outputs=stack_outputs)
        elif stream_output:
            _print_stream_summary(display_handler, outputs=stack_outputs)
        else:
            display_handler.show_completion(output_lines=format_grouped_outputs(grouped_outputs))


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

        grouped_outputs = group_stack_outputs(run.stack.outputs(), run.load_state())
        display_handler.show_completion(output_lines=format_grouped_outputs(grouped_outputs))
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


def _print_no_deployed_json(run: CommandRun, env: str, operation: str, action: str) -> None:
    handler = RichDeploymentHandler(run.app_name, env, operation, live_enabled=False)
    _print_json_summary(
        handler,
        outputs={},
        message=f"No app deployed yet. Nothing to {action}.",
    )


def _print_no_deployed_stream(run: CommandRun, env: str, operation: str, action: str) -> None:
    handler = RichDeploymentHandler(run.app_name, env, operation, live_enabled=False)
    _print_stream_summary(
        handler,
        outputs={},
        message=f"No app deployed yet. Nothing to {action}.",
    )


def _print_no_deployed_message(action: str) -> None:
    console.print(f"[yellow]No app deployed yet. Nothing to {action}.[/yellow]")


def _handle_missing_deployment(  # noqa: PLR0913
    run: CommandRun,
    *,
    json_output: bool,
    stream_output: bool,
    env: str,
    operation: str,
    json_empty_handler: Callable[[], None] | None = None,
) -> bool:
    if _has_deployed_app(run):
        return False

    action = {
        "outputs": "show outputs",
        "state_list": "list",
    }.get(operation, operation)

    if json_output:
        if json_empty_handler is not None:
            json_empty_handler()
        else:
            _print_no_deployed_json(run, env, operation, action)
    elif stream_output:
        _print_no_deployed_stream(run, env, operation, action)
    else:
        _print_no_deployed_message(action)
    return True


def _handle_command_error_json(  # noqa: PLR0913
    *,
    json_output: bool,
    stream_output: bool,
    operation: str,
    app_name: str,
    env: str,
    error: CommandError,
) -> None:
    if json_output:
        _print_json_error(
            operation=operation,
            app_name=app_name,
            env=env,
            error=str(error),
        )
    elif stream_output:
        _print_stream_error(
            operation=operation,
            app_name=app_name,
            env=env,
            error=str(error),
        )
    else:
        console.print(f"[red]{error!s}[/red]")
    _handle_error(error)


def run_refresh(
    env: str,
    *,
    json_output: bool = False,
) -> None:
    status = _start_loading_status(enabled=not json_output)

    with CommandRun(env, lock_as="refresh") as run:
        _stop_loading_status(status)
        if _handle_missing_deployment(
            run,
            json_output=json_output,
            stream_output=False,
            env=env,
            operation="refresh",
        ):
            return
        if not json_output:
            print_operation_header("Refreshing", run.app_name, env)
        display_handler = RichDeploymentHandler(
            run.app_name,
            env,
            "refresh",
            live_enabled=not json_output,
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

        outputs = _best_effort_outputs_data(run)
        if error_exc:
            if json_output:
                _print_json_summary(
                    display_handler,
                    status="failed",
                    outputs=outputs,
                    exit_code=1,
                    fallback_error=str(error_exc),
                )
            _handle_error(error_exc)

        if json_output:
            _print_json_summary(display_handler, outputs=outputs)
        else:
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


def run_destroy(  # noqa: C901, PLR0912
    env: str,
    skip_confirm: bool = False,
    *,
    json_output: bool = False,
    stream_output: bool = False,
) -> None:
    status = _start_loading_status(enabled=not (json_output or stream_output))

    with CommandRun(env, lock_as="destroy") as run:
        _stop_loading_status(status)
        if _handle_missing_deployment(
            run,
            json_output=json_output,
            stream_output=stream_output,
            env=env,
            operation="destroy",
        ):
            return

        if not skip_confirm and not _confirm_destroy(env):
            return

        if stream_output:
            _emit_stream_start("destroy", run.app_name, env)
        elif not json_output:
            print_operation_header("Destroying", run.app_name, env)
        display_handler = RichDeploymentHandler(
            run.app_name,
            env,
            "destroy",
            live_enabled=not (json_output or stream_output),
            stream_writer=_stream_writer() if stream_output else None,
        )
        error_exc: CommandError | None = None
        run.start_partial_push()
        try:
            run.stack.destroy(on_event=run.event_handler(display=display_handler))
        except CommandError as e:
            error_exc = e
            if not json_output:
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
            if json_output:
                _print_json_summary(
                    display_handler,
                    status="failed",
                    outputs={},
                    exit_code=1,
                    fallback_error=str(error_exc),
                )
            elif stream_output:
                _print_stream_summary(
                    display_handler,
                    status="failed",
                    outputs={},
                    exit_code=1,
                    fallback_error=str(error_exc),
                )
            _handle_error(error_exc)

        if json_output:
            _print_json_summary(display_handler, outputs={})
        elif stream_output:
            _print_stream_summary(display_handler, outputs={})
        else:
            display_handler.show_completion()


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
    grouped: bool = False,
    component_name: str | None = None,
) -> None:
    status = _start_loading_status(enabled=not json_output)

    with CommandRun(env) as run:
        _stop_loading_status(status)
        if _handle_missing_deployment(
            run,
            json_output=json_output,
            stream_output=False,
            env=env,
            operation="outputs",
            json_empty_handler=_print_empty_json_outputs,
        ):
            return
        if not json_output:
            print_operation_header("Outputs for", run.app_name, env)
        try:
            stack_outputs = run.stack.outputs()
            state = run.load_state()
            if stack_outputs:
                if json_output:
                    _print_json_outputs(
                        stack_outputs,
                        state,
                        grouped=grouped,
                        component_name=component_name,
                    )
                else:
                    _print_human_outputs(
                        stack_outputs,
                        state,
                        component_name=component_name,
                        app_name=run.app_name,
                        env=env,
                    )
            elif json_output:
                _print_empty_json_outputs()
            else:
                _print_no_outputs_message(run.app_name, env, component_name)
        except CommandError as e:
            _handle_command_error_json(
                json_output=json_output,
                stream_output=False,
                operation="outputs",
                app_name=run.app_name,
                env=env,
                error=e,
            )


def _has_deployed_app(run: CommandRun) -> bool:
    """Check if there's a deployed app we can work with."""
    return run.has_deployed


def _state_list_width() -> int:
    size = getattr(console, "size", SimpleNamespace(width=100))
    return max(size.width, 40)


def _wrap_state_value(
    *, prefix: str, prefix_visible: str, value: str, width: int, style: str | None = None
) -> list[str]:
    available_width = max(width - len(prefix_visible), 10)
    wrapped = wrap(
        value,
        width=available_width,
        break_long_words=False,
        break_on_hyphens=True,
    ) or [value]
    first_value = wrapped[0] if style is None else f"[{style}]{wrapped[0]}[/{style}]"
    lines = [f"{prefix}{first_value}"]
    continuation_prefix = " " * len(prefix_visible)
    for part in wrapped[1:]:
        continuation_value = part if style is None else f"[{style}]{part}[/{style}]"
        lines.append(f"{continuation_prefix}{continuation_value}")
    return lines


def _format_state_node_lines(node: StateTreeNode, indent: int, *, width: int) -> list[str]:
    pad = "  " * indent
    if node.resource.component_type is not None:
        lines = _wrap_state_value(
            prefix=f"{pad}[bold]{node.resource.component_type}[/bold]  ",
            prefix_visible=f"{pad}{node.resource.component_type}  ",
            value=node.resource.name,
            width=width,
        )
    else:
        lines = _wrap_state_value(
            prefix=pad,
            prefix_visible=pad,
            value=node.resource.name,
            width=width,
            style="cyan",
        )

    lines.append(f"{pad}  Type: {node.resource.type}")
    if node.resource.dependencies:
        dependency_names = [
            dependency.split("::")[-1] for dependency in node.resource.dependencies
        ]
        lines.extend(
            _wrap_state_value(
                prefix=f"{pad}  Depends on: ",
                prefix_visible=f"{pad}  Depends on: ",
                value=", ".join(dependency_names),
                width=width,
            )
        )
    for child in node.children:
        lines.extend(_format_state_node_lines(child, indent + 1, width=width))
    return lines


def _append_state_section(
    lines: list[str], title: str, nodes: tuple[StateTreeNode, ...], *, indent: int, width: int
) -> None:
    if not nodes:
        return

    lines.append(title)
    for node in nodes:
        lines.extend(_format_state_node_lines(node, indent, width=width))
        lines.append("")


def _format_state_tree_lines(grouped_state: GroupedStateResources) -> list[str]:
    lines: list[str] = []
    width = _state_list_width()

    if grouped_state.stack is not None:
        lines.append(f"[bold]Stack[/bold]  {grouped_state.stack.name}")
        for node in grouped_state.components:
            lines.extend(_format_state_node_lines(node, 1, width=width))
            lines.append("")
    else:
        for node in grouped_state.components:
            lines.extend(_format_state_node_lines(node, 0, width=width))
            lines.append("")

    _append_state_section(
        lines,
        "[bold]Providers[/bold]",
        grouped_state.providers,
        indent=1,
        width=width,
    )
    _append_state_section(
        lines,
        "[bold]Other roots[/bold]",
        grouped_state.other_roots,
        indent=1,
        width=width,
    )

    if lines and lines[-1] == "":
        lines.pop()
    return lines


def run_state_list(env: str, *, json_output: bool = False) -> None:
    """List all resources in state."""
    status = _start_loading_status(enabled=not json_output)

    with CommandRun(env, state_only=True) as run:
        _stop_loading_status(status)
        if _handle_missing_deployment(
            run,
            json_output=json_output,
            stream_output=False,
            env=env,
            operation="state_list",
            json_empty_handler=_print_empty_json_state,
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

            grouped_state = build_state_tree(state)
            if json_output:
                console.print_json(data=build_state_tree_json(grouped_state))
                return

            console.print(f"[bold]Resources ({len(resources)}):[/bold]\n")
            for line in _format_state_tree_lines(grouped_state):
                console.print(line)
        except CommandError as e:
            _handle_command_error_json(
                json_output=json_output,
                stream_output=False,
                operation="state_list",
                app_name=getattr(run, "app_name", ""),
                env=env,
                error=e,
            )


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
        if not _has_deployed_app(run):
            _print_no_deployed_message("remove")
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
        if not _has_deployed_app(run):
            _print_no_deployed_message("repair")
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
