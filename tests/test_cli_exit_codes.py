import importlib
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from stelvio.exceptions import StateLockedError, StelvioProjectError, StelvioValidationError


def _import_cli_module():
    with (
        patch("platformdirs.user_log_dir", return_value=str(Path.cwd() / ".tmp-test-logs")),
        patch("logging.handlers.TimedRotatingFileHandler"),
    ):
        return importlib.import_module("stelvio.cli")


def test_deploy_exits_with_locked_state_code() -> None:
    cli_module = _import_cli_module()
    runner = CliRunner()

    with (
        patch.object(cli_module, "ensure_pulumi"),
        patch.object(cli_module, "determine_env", return_value="dev"),
        patch.object(
            cli_module,
            "get_environment_confirmation_info",
            return_value=("stelvio-app", False),
        ),
        patch.object(
            cli_module,
            "run_deploy",
            side_effect=StateLockedError(
                command="deploy",
                created="2026-03-17T12:00:00+00:00",
                update_id="abc123",
                env="dev",
            ),
        ),
    ):
        result = runner.invoke(cli_module.deploy, ["dev", "--yes"])

    assert result.exit_code == int(cli_module.CliExitCode.STATE_LOCKED)
    assert "State is locked" in result.output
    assert "stlv unlock dev" in result.output


def test_outputs_exits_with_usage_code_for_missing_project() -> None:
    cli_module = _import_cli_module()
    runner = CliRunner()

    with (
        patch.object(cli_module, "ensure_pulumi"),
        patch.object(cli_module, "determine_env", return_value="dev"),
        patch.object(
            cli_module,
            "run_outputs",
            side_effect=StelvioProjectError("No Stelvio project found."),
        ),
    ):
        result = runner.invoke(cli_module.outputs, [])

    assert result.exit_code == int(cli_module.CliExitCode.USAGE_ERROR)
    assert "No Stelvio project found." in result.output


def test_outputs_exits_with_usage_code_for_invalid_environment() -> None:
    cli_module = _import_cli_module()
    runner = CliRunner()

    with (
        patch.object(cli_module, "ensure_pulumi"),
        patch.object(cli_module, "determine_env", return_value="invalid-env"),
        patch.object(
            cli_module,
            "run_outputs",
            side_effect=StelvioValidationError("Invalid environment 'invalid-env'."),
        ),
    ):
        result = runner.invoke(cli_module.outputs, [])

    assert result.exit_code == int(cli_module.CliExitCode.USAGE_ERROR)
    assert "Invalid environment 'invalid-env'." in result.output


def test_deploy_json_requires_yes_for_shared_environment() -> None:
    cli_module = _import_cli_module()
    runner = CliRunner()

    with (
        patch.object(cli_module, "ensure_pulumi"),
        patch.object(
            cli_module,
            "get_environment_confirmation_info",
            return_value=("stelvio-app", True),
        ),
    ):
        result = runner.invoke(cli_module.deploy, ["prod", "--json"])

    assert result.exit_code == int(cli_module.CliExitCode.USAGE_ERROR)
    assert '"operation": "deploy"' in result.output
    assert '"status": "failed"' in result.output
    assert '"exit_code": 2' in result.output
    assert "--json deploy to a shared environment requires --yes." in result.output


def test_deploy_json_invalid_environment_uses_validation_error() -> None:
    cli_module = _import_cli_module()
    runner = CliRunner()

    with (
        patch.object(cli_module, "ensure_pulumi"),
        patch.object(
            cli_module,
            "get_environment_confirmation_info",
            side_effect=StelvioValidationError("Invalid environment 'prod'."),
        ),
    ):
        result = runner.invoke(cli_module.deploy, ["prod", "--json"])

    assert result.exit_code == int(cli_module.CliExitCode.USAGE_ERROR)
    assert '"operation": "deploy"' in result.output
    assert '"status": "failed"' in result.output
    assert '"exit_code": 2' in result.output
    assert "Invalid environment 'prod'." in result.output


def test_destroy_json_requires_yes_to_avoid_prompt() -> None:
    cli_module = _import_cli_module()
    runner = CliRunner()

    with (
        patch.object(cli_module, "ensure_pulumi"),
        patch.object(cli_module, "determine_env", return_value="dev"),
    ):
        result = runner.invoke(cli_module.destroy, ["--json"])

    assert result.exit_code == int(cli_module.CliExitCode.USAGE_ERROR)
    assert '"operation": "destroy"' in result.output
    assert '"status": "failed"' in result.output
    assert '"exit_code": 2' in result.output
    assert "--json destroy requires --yes to avoid interactive prompts." in result.output


def test_outputs_json_usage_error_is_machine_readable() -> None:
    cli_module = _import_cli_module()
    runner = CliRunner()

    with (
        patch.object(cli_module, "ensure_pulumi"),
        patch.object(cli_module, "determine_env", return_value="dev"),
        patch.object(
            cli_module,
            "run_outputs",
            side_effect=StelvioProjectError("No Stelvio project found."),
        ),
    ):
        result = runner.invoke(cli_module.outputs, ["--json"])

    assert result.exit_code == int(cli_module.CliExitCode.USAGE_ERROR)
    assert '"operation": "outputs"' in result.output
    assert '"status": "failed"' in result.output
    assert '"exit_code": 2' in result.output
    assert "No Stelvio project found." in result.output


def test_state_list_json_usage_error_is_machine_readable() -> None:
    cli_module = _import_cli_module()
    runner = CliRunner()

    with (
        patch.object(cli_module, "ensure_pulumi"),
        patch.object(cli_module, "determine_env", return_value="dev"),
        patch.object(
            cli_module,
            "run_state_list",
            side_effect=StelvioProjectError("No Stelvio project found."),
        ),
    ):
        result = runner.invoke(cli_module.state_list, ["--json"])

    assert result.exit_code == int(cli_module.CliExitCode.USAGE_ERROR)
    assert '"operation": "state_list"' in result.output
    assert '"status": "failed"' in result.output
    assert '"exit_code": 2' in result.output
    assert "No Stelvio project found." in result.output


@pytest.mark.parametrize(
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
    command_name: str,
    args: list[str],
) -> None:
    cli_module = _import_cli_module()
    runner = CliRunner()
    command = getattr(cli_module, command_name)

    with (
        patch.object(cli_module, "ensure_pulumi"),
        patch.dict("os.environ", {"CI": "true"}, clear=False),
    ):
        result = runner.invoke(command, args)

    assert result.exit_code == int(cli_module.CliExitCode.USAGE_ERROR)
    assert (
        f"Environment is required in CI. Pass an explicit env like 'stlv {command_name} prod'."
        in result.output
    )


def test_ci_requires_explicit_environment_for_diff_json() -> None:
    cli_module = _import_cli_module()
    runner = CliRunner()

    with (
        patch.object(cli_module, "ensure_pulumi"),
        patch.dict("os.environ", {"CI": "true"}, clear=False),
    ):
        result = runner.invoke(cli_module.diff, ["--json"])

    assert result.exit_code == int(cli_module.CliExitCode.USAGE_ERROR)
    assert '"operation": "diff"' in result.output
    assert '"status": "failed"' in result.output
    assert '"exit_code": 2' in result.output
    assert "Environment is required in CI." in result.output
