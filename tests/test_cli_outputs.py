import importlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from click.testing import CliRunner
from pulumi.automation import OutputValue


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


class _FakeRun:
    def __init__(self, outputs: dict[str, OutputValue], state: dict):
        self.app_name = "demo"
        self.has_deployed = True
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

    def complete_update(self, errors=None) -> None:
        return None

    def event_handler(self, display):
        return Mock()


def _import_cli_module():
    with (
        patch("platformdirs.user_log_dir", return_value=str(Path.cwd() / ".tmp-test-logs")),
        patch("logging.handlers.TimedRotatingFileHandler"),
    ):
        return importlib.import_module("stelvio.cli")


def _import_cli_commands_module():
    _import_cli_module()
    return importlib.import_module("stelvio.cli.commands")


def test_outputs_command_accepts_component_and_grouped_flags() -> None:
    cli_module = _import_cli_module()
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
    commands_module = _import_cli_commands_module()
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
    commands_module = _import_cli_commands_module()
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
    commands_module = _import_cli_commands_module()
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


def test_run_deploy_passes_grouped_output_lines_to_completion() -> None:
    commands_module = _import_cli_commands_module()
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
