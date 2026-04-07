import json
import sys
from io import StringIO
from types import SimpleNamespace
from unittest.mock import Mock, patch

from click.testing import CliRunner

from tests.cli_test_helpers import FakeCommandRun, import_cli_commands_module, import_cli_module


def _make_fake_console(*, print_fn: Mock | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        print=print_fn if print_fn is not None else Mock(),
        print_json=Mock(),
        status=lambda *_args, **_kwargs: SimpleNamespace(start=lambda: None, stop=lambda: None),
    )


def _state_with_api_url() -> dict:
    """State with an Api component that has a url output (new model)."""
    return {
        "checkpoint": {
            "latest": {
                "resources": [
                    {
                        "urn": "urn:pulumi:test::demo::pulumi:pulumi:Stack::demo-test",
                        "type": "pulumi:pulumi:Stack",
                    },
                    {
                        "urn": "urn:pulumi:test::demo::stelvio:aws:Api::rest",
                        "type": "stelvio:aws:Api",
                        "outputs": {"url": "https://example.com"},
                    },
                ]
            }
        }
    }


def _state_no_outputs() -> dict:
    """State with components that have no display outputs."""
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
                ]
            }
        }
    }


def test_outputs_command_passes_json_flag() -> None:
    cli_module = import_cli_module()
    runner = CliRunner()

    with (
        patch.object(cli_module, "ensure_pulumi"),
        patch.object(cli_module, "determine_env", return_value="dev"),
        patch.object(cli_module, "run_outputs") as run_outputs_mock,
    ):
        result = runner.invoke(cli_module.outputs, ["prod", "--json"])

    assert result.exit_code == 0
    run_outputs_mock.assert_called_once_with("dev", json_output=True)


def test_run_outputs_human_mode_shows_component_urls() -> None:
    commands_module = import_cli_commands_module()
    printed: list[str] = []
    fake_console = _make_fake_console(
        print_fn=lambda *args, **_kwargs: printed.append(str(args[0]))
    )

    with (
        patch.object(commands_module, "console", fake_console),
        patch.object(commands_module, "print_operation_header"),
        patch.object(
            commands_module,
            "CommandRun",
            return_value=FakeCommandRun(_state_with_api_url(), outputs={}),
        ),
    ):
        commands_module.run_outputs("dev")

    assert printed == [
        "",
        "[bold]Outputs:",
        "  [bold]Api[/bold] rest",
        "    [cyan]url[/cyan]  https://example.com",
    ]


def test_run_outputs_json_with_component_outputs() -> None:
    commands_module = import_cli_commands_module()
    fake_console = _make_fake_console()

    with (
        patch.object(commands_module, "console", fake_console),
        patch.object(
            commands_module,
            "CommandRun",
            return_value=FakeCommandRun(_state_with_api_url(), outputs={}),
        ),
    ):
        commands_module.run_outputs("dev", json_output=True)

    fake_console.print_json.assert_called_once_with(
        data={
            "components": [
                {
                    "type": "Api",
                    "name": "rest",
                    "outputs": {"url": "https://example.com"},
                }
            ]
        }
    )


def test_run_outputs_json_prints_empty_object_when_no_outputs() -> None:
    commands_module = import_cli_commands_module()
    fake_console = _make_fake_console()

    with (
        patch.object(commands_module, "console", fake_console),
        patch.object(
            commands_module,
            "CommandRun",
            return_value=FakeCommandRun(_state_no_outputs(), outputs={}),
        ),
    ):
        commands_module.run_outputs("dev", json_output=True)

    fake_console.print_json.assert_called_once_with(data={})


def test_run_outputs_human_shows_no_outputs_message() -> None:
    commands_module = import_cli_commands_module()
    printed: list[str] = []
    fake_console = _make_fake_console(
        print_fn=lambda *args, **_kwargs: printed.append(str(args[0]))
    )

    with (
        patch.object(commands_module, "console", fake_console),
        patch.object(commands_module, "print_operation_header"),
        patch.object(
            commands_module,
            "CommandRun",
            return_value=FakeCommandRun(_state_no_outputs(), outputs={}),
        ),
    ):
        commands_module.run_outputs("dev")

    assert printed == ["[yellow]No outputs found for demo in dev[/yellow]"]


def test_run_deploy_passes_output_lines_to_completion() -> None:
    commands_module = import_cli_commands_module()
    fake_run = FakeCommandRun(_state_with_api_url(), outputs={})
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
            "  [bold]Api[/bold] rest",
            "    [cyan]url[/cyan]  https://example.com",
        ]
    )


def test_run_diff_json_prints_summary_without_human_header() -> None:
    commands_module = import_cli_commands_module()
    fake_console = _make_fake_console()
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
            return_value=FakeCommandRun(_state_no_outputs(), outputs={}),
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
    fake_console = _make_fake_console()
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
            return_value=FakeCommandRun(_state_no_outputs(), outputs={}),
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
    fake_console = _make_fake_console()
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
            return_value=FakeCommandRun(_state_no_outputs(), outputs={}),
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
    fake_console = _make_fake_console()
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
            return_value=FakeCommandRun(_state_no_outputs(), outputs={}),
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
            return_value=FakeCommandRun(_state_no_outputs(), outputs={}),
        ),
        patch.object(commands_module, "RichDeploymentHandler", return_value=fake_handler),
        patch.object(sys, "stdout", stdout),
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
    import_cli_commands_module()
    from stelvio.cli.json_output import print_stream_error

    stdout = StringIO()

    with patch.object(sys, "stdout", stdout):
        print_stream_error(
            operation="outputs", app_name="demo", env="dev", error="boom", exit_code=1
        )

    payload = json.loads(stdout.getvalue())
    assert isinstance(payload.pop("timestamp"), str)
    assert payload == {
        "event": "error",
        "operation": "outputs",
        "app": "demo",
        "env": "dev",
        "status": "failed",
        "exit_code": 1,
        "errors": [{"message": "boom"}],
    }


def test_run_outputs_json_no_deployed_prints_empty_object_only() -> None:
    commands_module = import_cli_commands_module()
    fake_console = _make_fake_console()

    with (
        patch.object(commands_module, "console", fake_console),
        patch.object(
            commands_module,
            "CommandRun",
            return_value=FakeCommandRun(_state_no_outputs(), outputs={}, has_deployed=False),
        ),
    ):
        commands_module.run_outputs("dev", json_output=True)

    fake_console.print.assert_not_called()
    fake_console.print_json.assert_called_once_with(data={})


def test_run_refresh_json_no_deployed_prints_json_only() -> None:
    commands_module = import_cli_commands_module()
    fake_console = _make_fake_console()
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
            return_value=FakeCommandRun(_state_no_outputs(), outputs={}, has_deployed=False),
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
    fake_console = _make_fake_console()
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
            return_value=FakeCommandRun(_state_no_outputs(), outputs={}, has_deployed=False),
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
