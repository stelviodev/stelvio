import json
from io import StringIO
from types import SimpleNamespace
from unittest.mock import Mock, patch

from click.testing import CliRunner
from pulumi.automation import OutputValue

from tests.cli_test_helpers import import_cli_commands_module, import_cli_module


def _state_with_function_and_api() -> dict:
    return {
        "checkpoint": {
            "latest": {
                "resources": [
                    {
                        "urn": "urn:pulumi:test::demo::pulumi:pulumi:Stack::demo-test",
                        "type": "pulumi:pulumi:Stack",
                    },
                    {
                        "urn": "urn:pulumi:test::demo::stelvio:aws:Function::api-handler",
                        "type": "stelvio:aws:Function",
                    },
                    {
                        "urn": "urn:pulumi:test::demo::stelvio:aws:Api::rest",
                        "type": "stelvio:aws:Api",
                    },
                ]
            }
        }
    }


class _FakeStack:
    def __init__(self, outputs: dict[str, OutputValue]):
        self._outputs = outputs

    def outputs(self) -> dict[str, OutputValue]:
        return self._outputs

    def up(self, on_event) -> None:
        return None

    def preview(self, on_event) -> None:
        return None

    def refresh(self, on_event) -> None:
        return None

    def destroy(self, on_event) -> None:
        return None

    def export_stack(self):
        return SimpleNamespace(deployment={"resources": [{"type": "pulumi:pulumi:Stack"}]})


class _FakeRun:
    def __init__(self, outputs: dict[str, OutputValue], state: dict, *, has_deployed: bool = True):
        self.app_name = "demo"
        self.has_deployed = has_deployed
        self.stack = _FakeStack(outputs)
        self._state = state

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def load_state(self) -> dict:
        return self._state

    def start_partial_push(self) -> None:
        return None

    def stop_partial_push(self) -> None:
        return None

    def push_state(self) -> None:
        return None

    def create_state_snapshot(self) -> None:
        return None

    def delete_snapshots(self) -> None:
        return None

    def complete_update(self, errors=None) -> None:
        return None

    def event_handler(self, display):
        return Mock()


def test_outputs_command_accepts_component_and_grouped_flags() -> None:
    cli_module = import_cli_module()
    runner = CliRunner()

    with (
        patch.object(cli_module, "ensure_pulumi"),
        patch.object(cli_module, "determine_env", return_value="dev"),
        patch.object(cli_module, "run_outputs") as run_outputs_mock,
    ):
        result = runner.invoke(
            cli_module.outputs,
            ["prod", "--json", "-g", "-c", "api-handler"],
        )

    assert result.exit_code == 0
    run_outputs_mock.assert_called_once_with(
        "dev",
        json_output=True,
        grouped=True,
        component_name="api-handler",
    )


def test_run_outputs_human_mode_is_grouped_by_default() -> None:
    commands_module = import_cli_commands_module()
    outputs = {
        "function_api-handler_arn": OutputValue("handler-arn", False),
        "api_rest_invoke_url": OutputValue("https://example.com", False),
    }
    printed: list[str] = []
    fake_console = SimpleNamespace(
        status=lambda *_args, **_kwargs: SimpleNamespace(start=lambda: None, stop=lambda: None),
        print=lambda *args, **_kwargs: printed.append(str(args[0])),
        print_json=Mock(),
    )

    with (
        patch.object(commands_module, "console", fake_console),
        patch.object(commands_module, "print_operation_header"),
        patch.object(
            commands_module,
            "CommandRun",
            return_value=_FakeRun(outputs, _state_with_function_and_api()),
        ),
    ):
        commands_module.run_outputs("dev")

    assert printed == [
        "",
        "[bold]Outputs:",
        "  [bold]Function[/bold]  api-handler",
        "    [cyan]arn[/cyan]  handler-arn",
        "  [bold]Api[/bold]  rest",
        "    [cyan]invoke_url[/cyan]  https://example.com",
        "",
    ]


def test_run_outputs_json_grouped_and_component_filtered() -> None:
    commands_module = import_cli_commands_module()
    outputs = {
        "function_api-handler_arn": OutputValue("handler-arn", False),
        "function_api-handler_name": OutputValue("handler-name", False),
        "api_rest_invoke_url": OutputValue("https://example.com", False),
    }
    fake_console = SimpleNamespace(
        status=lambda *_args, **_kwargs: SimpleNamespace(start=lambda: None, stop=lambda: None),
        print=Mock(),
        print_json=Mock(),
    )

    with (
        patch.object(commands_module, "console", fake_console),
        patch.object(
            commands_module,
            "CommandRun",
            return_value=_FakeRun(outputs, _state_with_function_and_api()),
        ),
    ):
        commands_module.run_outputs(
            "dev",
            json_output=True,
            grouped=True,
            component_name="api-handler",
        )

    fake_console.print_json.assert_called_once_with(
        data={
            "components": {
                "api-handler": {
                    "arn": "handler-arn",
                    "name": "handler-name",
                }
            }
        }
    )


def test_run_outputs_reports_missing_component_in_human_mode() -> None:
    commands_module = import_cli_commands_module()
    outputs = {
        "function_api-handler_arn": OutputValue("handler-arn", False),
    }
    printed: list[str] = []
    fake_console = SimpleNamespace(
        status=lambda *_args, **_kwargs: SimpleNamespace(start=lambda: None, stop=lambda: None),
        print=lambda *args, **_kwargs: printed.append(str(args[0])),
        print_json=Mock(),
    )

    with (
        patch.object(commands_module, "console", fake_console),
        patch.object(commands_module, "print_operation_header"),
        patch.object(
            commands_module,
            "CommandRun",
            return_value=_FakeRun(outputs, _state_with_function_and_api()),
        ),
    ):
        commands_module.run_outputs("dev", component_name="missing")

    assert printed == ["[yellow]No outputs found for component 'missing' in demo → dev[/yellow]"]


def test_run_outputs_json_prints_empty_object_when_stack_has_no_outputs() -> None:
    commands_module = import_cli_commands_module()
    fake_console = SimpleNamespace(
        status=lambda *_args, **_kwargs: SimpleNamespace(start=lambda: None, stop=lambda: None),
        print=Mock(),
        print_json=Mock(),
    )

    with (
        patch.object(commands_module, "console", fake_console),
        patch.object(
            commands_module,
            "CommandRun",
            return_value=_FakeRun({}, _state_with_function_and_api()),
        ),
    ):
        commands_module.run_outputs("dev", json_output=True)

    fake_console.print_json.assert_called_once_with(data={})


def test_run_deploy_passes_grouped_output_lines_to_completion() -> None:
    commands_module = import_cli_commands_module()
    outputs = {
        "function_api-handler_arn": OutputValue("handler-arn", False),
        "api_rest_invoke_url": OutputValue("https://example.com", False),
    }
    fake_run = _FakeRun(outputs, _state_with_function_and_api())
    handler = Mock()

    with (
        patch.object(commands_module, "_reset_cache_tracking"),
        patch.object(commands_module, "_clean_stale_caches"),
        patch.object(commands_module, "print_operation_header"),
        patch.object(commands_module, "CommandRun", return_value=fake_run),
        patch.object(commands_module, "RichDeploymentHandler", return_value=handler),
    ):
        commands_module.run_deploy("dev")

    handler.show_completion.assert_called_once_with(
        output_lines=[
            "",
            "[bold]Outputs:",
            "  [bold]Function[/bold]  api-handler",
            "    [cyan]arn[/cyan]  handler-arn",
            "  [bold]Api[/bold]  rest",
            "    [cyan]invoke_url[/cyan]  https://example.com",
            "",
        ]
    )


def test_run_diff_json_prints_summary_without_human_header() -> None:
    commands_module = import_cli_commands_module()
    fake_console = SimpleNamespace(
        print=Mock(),
        print_json=Mock(),
        status=lambda *_args, **_kwargs: SimpleNamespace(start=lambda: None, stop=lambda: None),
    )
    fake_handler = Mock()
    fake_handler.build_json_summary.return_value = {
        "operation": "diff",
        "status": "success",
        "exit_code": 0,
    }

    with (
        patch.object(commands_module, "console", fake_console),
        patch.object(commands_module, "_reset_cache_tracking"),
        patch.object(commands_module, "_clean_stale_caches"),
        patch.object(commands_module, "print_operation_header") as header_mock,
        patch.object(
            commands_module,
            "CommandRun",
            return_value=_FakeRun({}, _state_with_function_and_api()),
        ),
        patch.object(commands_module, "RichDeploymentHandler", return_value=fake_handler),
    ):
        commands_module.run_diff("dev", json_output=True)

    header_mock.assert_not_called()
    fake_console.print.assert_not_called()
    fake_console.print_json.assert_called_once_with(
        data={"operation": "diff", "status": "success", "exit_code": 0}
    )


def test_run_deploy_json_prints_summary_without_human_header() -> None:
    commands_module = import_cli_commands_module()
    outputs = {
        "function_api-handler_arn": OutputValue("handler-arn", False),
    }
    fake_console = SimpleNamespace(
        print=Mock(),
        print_json=Mock(),
        status=lambda *_args, **_kwargs: SimpleNamespace(start=lambda: None, stop=lambda: None),
    )
    fake_handler = Mock()
    fake_handler.build_json_summary.return_value = {
        "operation": "deploy",
        "status": "success",
        "exit_code": 0,
    }

    with (
        patch.object(commands_module, "console", fake_console),
        patch.object(commands_module, "_reset_cache_tracking"),
        patch.object(commands_module, "_clean_stale_caches"),
        patch.object(commands_module, "print_operation_header") as header_mock,
        patch.object(
            commands_module,
            "CommandRun",
            return_value=_FakeRun(outputs, _state_with_function_and_api()),
        ),
        patch.object(commands_module, "RichDeploymentHandler", return_value=fake_handler),
    ):
        commands_module.run_deploy("dev", json_output=True)

    header_mock.assert_not_called()
    fake_console.print.assert_not_called()
    fake_console.print_json.assert_called_once_with(
        data={"operation": "deploy", "status": "success", "exit_code": 0}
    )


def test_run_refresh_json_prints_summary_without_human_header() -> None:
    commands_module = import_cli_commands_module()
    outputs = {
        "function_api-handler_arn": OutputValue("handler-arn", False),
    }
    fake_console = SimpleNamespace(
        print=Mock(),
        print_json=Mock(),
        status=lambda *_args, **_kwargs: SimpleNamespace(start=lambda: None, stop=lambda: None),
    )
    fake_handler = Mock()
    fake_handler.build_json_summary.return_value = {
        "operation": "refresh",
        "status": "success",
        "exit_code": 0,
    }

    with (
        patch.object(commands_module, "console", fake_console),
        patch.object(commands_module, "print_operation_header") as header_mock,
        patch.object(
            commands_module,
            "CommandRun",
            return_value=_FakeRun(outputs, _state_with_function_and_api()),
        ),
        patch.object(commands_module, "RichDeploymentHandler", return_value=fake_handler),
    ):
        commands_module.run_refresh("dev", json_output=True)

    header_mock.assert_not_called()
    fake_console.print.assert_not_called()
    fake_console.print_json.assert_called_once_with(
        data={"operation": "refresh", "status": "success", "exit_code": 0}
    )


def test_run_destroy_json_prints_summary_without_human_header() -> None:
    commands_module = import_cli_commands_module()
    fake_console = SimpleNamespace(
        print=Mock(),
        print_json=Mock(),
        status=lambda *_args, **_kwargs: SimpleNamespace(start=lambda: None, stop=lambda: None),
    )
    fake_handler = Mock()
    fake_handler.build_json_summary.return_value = {
        "operation": "destroy",
        "status": "success",
        "exit_code": 0,
    }

    with (
        patch.object(commands_module, "console", fake_console),
        patch.object(commands_module, "print_operation_header") as header_mock,
        patch.object(
            commands_module,
            "CommandRun",
            return_value=_FakeRun({}, _state_with_function_and_api()),
        ),
        patch.object(commands_module, "RichDeploymentHandler", return_value=fake_handler),
    ):
        commands_module.run_destroy("dev", skip_confirm=True, json_output=True)

    header_mock.assert_not_called()
    fake_console.print.assert_not_called()
    fake_console.print_json.assert_called_once_with(
        data={"operation": "destroy", "status": "success", "exit_code": 0}
    )


def test_run_deploy_stream_prints_jsonl_start_and_summary_only() -> None:
    commands_module = import_cli_commands_module()
    outputs = {
        "function_api-handler_arn": OutputValue("handler-arn", False),
    }
    stdout = StringIO()
    fake_handler = Mock()
    fake_handler.build_json_summary.return_value = {
        "operation": "deploy",
        "status": "success",
        "exit_code": 0,
    }

    with (
        patch.object(commands_module, "_reset_cache_tracking"),
        patch.object(commands_module, "_clean_stale_caches"),
        patch.object(commands_module, "print_operation_header") as header_mock,
        patch.object(
            commands_module,
            "CommandRun",
            return_value=_FakeRun(outputs, _state_with_function_and_api()),
        ),
        patch.object(commands_module, "RichDeploymentHandler", return_value=fake_handler),
        patch.object(commands_module.sys, "stdout", stdout),
    ):
        commands_module.run_deploy("dev", stream_output=True)

    header_mock.assert_not_called()
    events = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert len(events) == 2
    assert events[0]["event"] == "start"
    assert events[0]["operation"] == "deploy"
    assert events[0]["app"] == "demo"
    assert events[0]["env"] == "dev"
    assert isinstance(events[0]["timestamp"], str)
    assert events[1]["event"] == "summary"
    assert events[1]["operation"] == "deploy"
    assert events[1]["status"] == "success"
    assert events[1]["exit_code"] == 0
    assert isinstance(events[1]["timestamp"], str)


def test_print_stream_error_includes_timestamp() -> None:
    commands_module = import_cli_commands_module()
    stdout = StringIO()

    with (
        patch.object(commands_module.sys, "stdout", stdout),
        patch.object(
            commands_module,
            "_stream_timestamp",
            return_value="2026-03-21T12:00:00+00:00",
        ),
    ):
        commands_module._print_stream_error(
            operation="outputs",
            app_name="demo",
            env="dev",
            error="boom",
            exit_code=1,
        )

    payload = json.loads(stdout.getvalue())
    assert payload == {
        "event": "error",
        "operation": "outputs",
        "app": "demo",
        "env": "dev",
        "timestamp": "2026-03-21T12:00:00+00:00",
        "status": "failed",
        "exit_code": 1,
        "errors": [{"message": "boom"}],
    }


def test_run_outputs_json_no_deployed_prints_empty_object_only() -> None:
    commands_module = import_cli_commands_module()
    fake_console = SimpleNamespace(
        print=Mock(),
        print_json=Mock(),
        status=lambda *_args, **_kwargs: SimpleNamespace(start=lambda: None, stop=lambda: None),
    )

    with (
        patch.object(commands_module, "console", fake_console),
        patch.object(
            commands_module,
            "CommandRun",
            return_value=_FakeRun({}, _state_with_function_and_api(), has_deployed=False),
        ),
    ):
        commands_module.run_outputs("dev", json_output=True)

    fake_console.print.assert_not_called()
    fake_console.print_json.assert_called_once_with(data={})


def test_run_refresh_json_no_deployed_prints_json_only() -> None:
    commands_module = import_cli_commands_module()
    fake_console = SimpleNamespace(
        print=Mock(),
        print_json=Mock(),
        status=lambda *_args, **_kwargs: SimpleNamespace(start=lambda: None, stop=lambda: None),
    )
    fake_handler = Mock()
    fake_handler.build_json_summary.return_value = {
        "operation": "refresh",
        "status": "success",
        "exit_code": 0,
        "message": "No app deployed yet. Nothing to refresh.",
        "outputs": {},
    }

    with (
        patch.object(commands_module, "console", fake_console),
        patch.object(
            commands_module,
            "CommandRun",
            return_value=_FakeRun({}, _state_with_function_and_api(), has_deployed=False),
        ),
        patch.object(commands_module, "RichDeploymentHandler", return_value=fake_handler),
    ):
        commands_module.run_refresh("dev", json_output=True)

    fake_console.print.assert_not_called()
    fake_console.print_json.assert_called_once_with(
        data={
            "operation": "refresh",
            "status": "success",
            "exit_code": 0,
            "message": "No app deployed yet. Nothing to refresh.",
            "outputs": {},
        }
    )


def test_run_destroy_json_no_deployed_prints_json_only() -> None:
    commands_module = import_cli_commands_module()
    fake_console = SimpleNamespace(
        print=Mock(),
        print_json=Mock(),
        status=lambda *_args, **_kwargs: SimpleNamespace(start=lambda: None, stop=lambda: None),
    )
    fake_handler = Mock()
    fake_handler.build_json_summary.return_value = {
        "operation": "destroy",
        "status": "success",
        "exit_code": 0,
        "message": "No app deployed yet. Nothing to destroy.",
        "outputs": {},
    }

    with (
        patch.object(commands_module, "console", fake_console),
        patch.object(
            commands_module,
            "CommandRun",
            return_value=_FakeRun({}, _state_with_function_and_api(), has_deployed=False),
        ),
        patch.object(commands_module, "RichDeploymentHandler", return_value=fake_handler),
    ):
        commands_module.run_destroy("dev", json_output=True)

    fake_console.print.assert_not_called()
    fake_console.print_json.assert_called_once_with(
        data={
            "operation": "destroy",
            "status": "success",
            "exit_code": 0,
            "message": "No app deployed yet. Nothing to destroy.",
            "outputs": {},
        }
    )
