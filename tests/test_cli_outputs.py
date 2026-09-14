import json

from click.testing import CliRunner
from pytest import mark, raises

from stelvio.exceptions import StelvioValidationError
from tests.cli_test_helpers import FakeCommandRun


def _state_with_api_url() -> dict:
    """State with an Api component that has a url output."""
    return {
        "checkpoint": {
            "latest": {
                "resources": [
                    {
                        "urn": "urn:pulumi:test::demo::pulumi:pulumi:Stack::demo-test",
                        "type": "pulumi:pulumi:Stack",
                    },
                    {
                        "urn": "urn:pulumi:test::demo::stelvio:aws:RestApi::rest",
                        "type": "stelvio:aws:RestApi",
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


def test_outputs_command_passes_json_flag(cli) -> None:
    result = CliRunner().invoke(cli.outputs, ["dev", "--json"])

    assert result.exit_code == 0
    cli.run_outputs.assert_called_once_with("dev", json_output=True)


def test_outputs_command_defaults_to_personal_env(cli, monkeypatch) -> None:
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr(cli, "get_user_env", lambda: "alice")

    result = CliRunner().invoke(cli.outputs, ["--json"])

    assert result.exit_code == 0
    cli.run_outputs.assert_called_once_with("alice", json_output=True)


def test_run_outputs_human_mode_shows_component_urls(cli_commands) -> None:
    cli_commands.CommandRun.return_value = FakeCommandRun(_state_with_api_url())

    cli_commands.run_outputs("dev")

    cli_commands.print_operation_header.assert_called_once_with("Outputs for", "demo", "dev")
    assert cli_commands.console.lines == [
        "",
        "[bold]Outputs:",
        "  [bold]RestApi[/bold] rest",
        "    [cyan]url[/cyan]  https://example.com",
    ]


def test_run_outputs_json_with_component_outputs(cli_commands) -> None:
    cli_commands.CommandRun.return_value = FakeCommandRun(_state_with_api_url())

    cli_commands.run_outputs("dev", json_output=True)

    cli_commands.console.print_json.assert_called_once_with(
        data={
            "components": [
                {
                    "type": "RestApi",
                    "name": "rest",
                    "outputs": {"url": "https://example.com"},
                }
            ]
        }
    )


def test_run_outputs_json_prints_empty_object_when_no_outputs(cli_commands) -> None:
    cli_commands.CommandRun.return_value = FakeCommandRun(_state_no_outputs())

    cli_commands.run_outputs("dev", json_output=True)

    cli_commands.console.print_json.assert_called_once_with(data={})


def test_run_outputs_human_shows_no_outputs_message(cli_commands) -> None:
    cli_commands.CommandRun.return_value = FakeCommandRun(_state_no_outputs())

    cli_commands.run_outputs("dev")

    assert cli_commands.console.lines == ["[yellow]No outputs found for demo in dev[/yellow]"]


def test_run_deploy_passes_output_lines_to_completion(cli_commands) -> None:
    cli_commands.CommandRun.return_value = FakeCommandRun(_state_with_api_url())

    cli_commands.run_deploy("dev")

    cli_commands.RichDeploymentHandler.return_value.show_completion.assert_called_once_with(
        output_lines=[
            "",
            "[bold]Outputs:",
            "  [bold]RestApi[/bold] rest",
            "    [cyan]url[/cyan]  https://example.com",
        ]
    )


@mark.parametrize(
    ("command", "kwargs"),
    [
        ("run_diff", {}),
        ("run_deploy", {}),
        ("run_refresh", {}),
        ("run_destroy", {"skip_confirm": True}),
    ],
)
def test_run_json_prints_summary_without_human_header(cli_commands, command, kwargs) -> None:
    summary = cli_commands.RichDeploymentHandler.return_value.build_json_summary.return_value

    getattr(cli_commands, command)("dev", json_output=True, **kwargs)

    cli_commands.print_operation_header.assert_not_called()
    assert cli_commands.console.lines == []
    cli_commands.console.print_json.assert_called_once_with(data=summary)


def test_run_deploy_stream_prints_jsonl_start_and_summary_only(cli_commands, capsys) -> None:
    cli_commands.RichDeploymentHandler.return_value.build_json_summary.return_value = {
        "operation": "deploy",
        "status": "success",
        "exit_code": 0,
    }

    cli_commands.run_deploy("dev", stream_output=True)

    cli_commands.print_operation_header.assert_not_called()
    start, summary = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert isinstance(start.pop("timestamp"), str)
    assert start == {"event": "start", "operation": "deploy", "app": "demo", "env": "dev"}
    assert isinstance(summary.pop("timestamp"), str)
    assert summary == {
        "event": "summary",
        "operation": "deploy",
        "status": "success",
        "exit_code": 0,
    }


# `cli` imports stelvio.cli with its log handler patched; a bare json_output import would
# trigger the real one and create a log file.
@mark.usefixtures("cli")
def test_print_stream_error_includes_timestamp(capsys) -> None:
    from stelvio.cli.json_output import print_stream_error

    print_stream_error(operation="outputs", app_name="demo", env="dev", error="boom", exit_code=1)

    payload = json.loads(capsys.readouterr().out)
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


def test_run_outputs_json_no_deployed_prints_empty_object_only(cli_commands) -> None:
    cli_commands.CommandRun.return_value.has_deployed = False

    cli_commands.run_outputs("dev", json_output=True)

    assert cli_commands.console.lines == []
    cli_commands.console.print_json.assert_called_once_with(data={})


@mark.parametrize(
    ("command", "message"),
    [
        ("run_refresh", "No app deployed yet. Nothing to refresh."),
        ("run_destroy", "No app deployed yet. Nothing to destroy."),
    ],
)
def test_run_json_no_deployed_prints_json_only(cli_commands, command, message) -> None:
    cli_commands.CommandRun.return_value.has_deployed = False
    handler = cli_commands.RichDeploymentHandler.return_value

    getattr(cli_commands, command)("dev", json_output=True)

    assert cli_commands.console.lines == []
    handler.build_json_summary.assert_called_once_with(outputs={}, message=message)
    cli_commands.console.print_json.assert_called_once_with(
        data=handler.build_json_summary.return_value
    )


def test_run_diff_stops_spinner_when_app_fails_to_load(cli_commands) -> None:
    """The 'Loading app...' spinner used to outlive a failed CommandRun and sit on screen
    under the error. rich renders no spinner off a TTY, so captured output can't show it;
    the status context manager's exit is the observable hook."""
    cli_commands.CommandRun.side_effect = StelvioValidationError("no credentials")

    with raises(StelvioValidationError, match="no credentials"):
        cli_commands.run_diff("dev")

    assert cli_commands.console.spinner.calls == ["start", "stop"]
