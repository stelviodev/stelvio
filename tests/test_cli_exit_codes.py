import importlib
from pathlib import Path
from unittest.mock import patch

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
