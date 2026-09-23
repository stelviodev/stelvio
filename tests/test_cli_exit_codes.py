import json
import multiprocessing
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

from click.testing import CliRunner, Result
from pytest import mark, raises
from rich.console import Console

from stelvio.exceptions import StateLockedError, StelvioProjectError, StelvioValidationError

USAGE_ERROR = 2  # CliExitCode.USAGE_ERROR, pinned by test_cli_exit_code_values_are_stable


def _assert_usage_error(
    result: Result, *, operation: str, env: str | None, message: str, stream: bool = False
) -> None:
    assert result.exit_code == USAGE_ERROR
    payload = json.loads(result.output)
    assert isinstance(payload.pop("timestamp"), str)
    assert payload == {
        **({"event": "error"} if stream else {}),
        "operation": operation,
        "app": None,
        "env": env,
        "status": "failed",
        "exit_code": USAGE_ERROR,
        "errors": [{"message": message}],
    }


def test_cli_exit_code_values_are_stable(cli) -> None:
    assert int(cli.CliExitCode.SUCCESS) == 0
    assert int(cli.CliExitCode.OPERATION_FAILED) == 1
    assert int(cli.CliExitCode.USAGE_ERROR) == 2
    assert int(cli.CliExitCode.STATE_LOCKED) == 4


def test_deploy_exits_with_locked_state_code(cli) -> None:
    cli.run_deploy.side_effect = StateLockedError(
        command="deploy",
        created="2026-03-17T12:00:00+00:00",
        update_id="abc123",
        env="dev",
    )

    result = CliRunner().invoke(cli.deploy, ["dev", "--yes"])

    assert result.exit_code == int(cli.CliExitCode.STATE_LOCKED)
    assert "State is locked" in result.output
    assert "stlv unlock dev" in result.output


def test_outputs_exits_with_usage_code_for_missing_project(cli) -> None:
    cli.run_outputs.side_effect = StelvioProjectError("No Stelvio project found.")

    result = CliRunner().invoke(cli.outputs, ["dev"])

    assert result.exit_code == int(cli.CliExitCode.USAGE_ERROR)
    assert "No Stelvio project found." in result.output


def test_error_text_that_looks_like_rich_markup_prints_verbatim(cli) -> None:
    """Error text comes from AWS, Pulumi, user code and resource names; Rich would eat
    `[dev]` as a style tag and raise on `[/x]` as an unmatched closing tag."""
    cli.run_outputs.side_effect = StelvioProjectError("Function 'api-[dev]' not found [/x]")

    result = CliRunner().invoke(cli.outputs, ["dev"])

    assert result.exit_code == int(cli.CliExitCode.USAGE_ERROR)
    assert "Function 'api-[dev]' not found [/x]" in result.output


def test_command_error_text_that_looks_like_rich_markup_prints_verbatim(monkeypatch) -> None:
    """The deploy/diff failure path prints Pulumi's CommandError text the same way."""
    from stelvio.cli import commands

    console = Console(record=True, width=160)
    monkeypatch.setattr(commands, "console", console)
    monkeypatch.delenv("STLV_DEBUG", raising=False)
    error = Exception("creating Queue 'jobs-[dev]' failed [/x]")

    with raises(SystemExit) as exc:
        commands._handle_command_error(
            json_output=False,
            stream_output=False,
            operation="deploy",
            app_name="myapp",
            env="dev",
            error=error,  # type: ignore[arg-type]
        )

    assert exc.value.code == 1
    assert "creating Queue 'jobs-[dev]' failed [/x]" in console.export_text()


def test_outputs_exits_with_usage_code_for_invalid_environment(cli) -> None:
    cli.run_outputs.side_effect = StelvioValidationError("Invalid environment 'invalid-env'.")

    result = CliRunner().invoke(cli.outputs, ["invalid-env"])

    assert result.exit_code == int(cli.CliExitCode.USAGE_ERROR)
    assert "Invalid environment 'invalid-env'." in result.output


def test_deploy_json_requires_yes_for_shared_environment(cli) -> None:
    cli.get_environment_confirmation_info.return_value = ("stelvio-app", True)

    result = CliRunner().invoke(cli.deploy, ["prod", "--json"])

    _assert_usage_error(
        result,
        operation="deploy",
        env="prod",
        message="--json deploy to a shared environment requires --yes.",
    )


def test_deploy_json_invalid_environment_uses_validation_error(cli) -> None:
    cli.get_environment_confirmation_info.side_effect = StelvioValidationError(
        "Invalid environment 'prod'."
    )

    result = CliRunner().invoke(cli.deploy, ["prod", "--json"])

    _assert_usage_error(
        result, operation="deploy", env="prod", message="Invalid environment 'prod'."
    )


def test_destroy_json_requires_yes_to_avoid_prompt(cli) -> None:
    result = CliRunner().invoke(cli.destroy, ["dev", "--json"])

    _assert_usage_error(
        result,
        operation="destroy",
        env="dev",
        message="--json destroy requires --yes to avoid interactive prompts.",
    )


def test_deploy_stream_requires_yes_for_shared_environment(cli) -> None:
    cli.get_environment_confirmation_info.return_value = ("stelvio-app", True)

    result = CliRunner().invoke(cli.deploy, ["prod", "--stream"])

    _assert_usage_error(
        result,
        operation="deploy",
        env="prod",
        message="--stream deploy to a shared environment requires --yes.",
        stream=True,
    )


def test_destroy_stream_requires_yes_to_avoid_prompt(cli) -> None:
    result = CliRunner().invoke(cli.destroy, ["dev", "--stream"])

    _assert_usage_error(
        result,
        operation="destroy",
        env="dev",
        message="--stream destroy requires --yes to avoid interactive prompts.",
        stream=True,
    )


@mark.parametrize("command_name", ["deploy", "destroy"])
def test_json_and_stream_are_mutually_exclusive(cli, command_name: str) -> None:
    command = getattr(cli, command_name)
    extra_args = ["--yes"] if command_name == "destroy" else []

    result = CliRunner().invoke(command, [*extra_args, "--json", "--stream"])

    assert result.exit_code == int(cli.CliExitCode.USAGE_ERROR)
    assert "--json and --stream are mutually exclusive." in result.output


def test_outputs_json_usage_error_is_machine_readable(cli) -> None:
    cli.run_outputs.side_effect = StelvioProjectError("No Stelvio project found.")

    result = CliRunner().invoke(cli.outputs, ["dev", "--json"])

    _assert_usage_error(
        result, operation="outputs", env="dev", message="No Stelvio project found."
    )


def test_state_list_json_usage_error_is_machine_readable(cli) -> None:
    cli.run_state_list.side_effect = StelvioProjectError("No Stelvio project found.")

    result = CliRunner().invoke(cli.state_list, ["--env", "dev", "--json"])

    _assert_usage_error(
        result, operation="state_list", env="dev", message="No Stelvio project found."
    )


@mark.parametrize(
    ("command_name", "args"),
    [
        ("diff", []),
        ("deploy", []),
        ("dev", []),
        ("refresh", []),
        ("destroy", ["--yes"]),
    ],
)
def test_ci_requires_explicit_environment_for_mutating_and_preview_commands(
    cli, monkeypatch, command_name: str, args: list[str]
) -> None:
    monkeypatch.setenv("CI", "true")

    result = CliRunner().invoke(getattr(cli, command_name), args)

    assert result.exit_code == int(cli.CliExitCode.USAGE_ERROR)
    assert (
        f"Environment is required in CI. Pass an explicit env like 'stlv {command_name} prod'."
        in result.output
    )


def test_ci_requires_explicit_environment_for_diff_json(cli, monkeypatch) -> None:
    monkeypatch.setenv("CI", "true")

    result = CliRunner().invoke(cli.diff, ["--json"])

    _assert_usage_error(
        result,
        operation="diff",
        env=None,
        message="Environment is required in CI. Pass an explicit env like 'stlv diff prod'.",
    )


def _run_cli_with_a_stuck_pool_worker(argv: list[str]) -> None:
    from stelvio.cli import main

    ThreadPoolExecutor().submit(threading.Event().wait)
    sys.argv = ["stlv", *argv]
    main()


@mark.parametrize(("argv", "expected"), [(["--version"], 0), (["no-such-command"], USAGE_ERROR)])
def test_cli_exits_even_when_a_thread_pool_worker_never_finishes(argv, expected) -> None:
    """A failed engine run leaves the Automation API's inline-program worker blocked for good,
    and interpreter shutdown joins it: `stlv` printed its ending and never exited. A spawned
    child runs the same shutdown join, so through `cli()` it never reports an exit code. The
    usage-error row pins that `main()` passes click's exit code through."""
    process = multiprocessing.get_context("spawn").Process(
        target=_run_cli_with_a_stuck_pool_worker, args=(argv,)
    )
    process.start()
    process.join(timeout=30)
    exit_code = process.exitcode
    process.kill()

    assert exit_code == expected
