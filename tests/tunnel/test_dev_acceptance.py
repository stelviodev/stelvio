"""Automated proof commands retain CLI safety and require no interactive input."""

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from click.testing import CliRunner

from tests.integration import cli_session


@pytest.mark.parametrize(
    ("method", "arguments"),
    [
        ("start_dev", ["dev", "proof", "--yes"]),
        ("start", ["tunnel", "start", "proof"]),
    ],
)
def test_acceptance_subprocess_uses_captured_environment_and_closed_stdin(
    monkeypatch, tmp_path, method, arguments
):
    session = object.__new__(cli_session.StandaloneCliSession)
    session.process = None
    session.cli = Path("/test/stlv")
    session.project = tmp_path
    session.environment_name = "proof"
    session.log_path = tmp_path / "session.log"
    session.environment = {
        "AWS_PROFILE": "captured-profile",
        "STLV_TRAFORO_DEV_BINARY": "/test/local/traforo",
    }
    monkeypatch.setenv("STLV_TRAFORO_DEV_BINARY", "/handler/modified/traforo")
    process = SimpleNamespace(poll=lambda: None)
    popen = Mock(return_value=process)
    monkeypatch.setattr(cli_session.subprocess, "Popen", popen)
    try:
        getattr(session, method)()
        popen.assert_called_once_with(
            ["/test/stlv", *arguments],
            cwd=tmp_path,
            env=session.environment,
            stdin=subprocess.DEVNULL,
            stdout=session._log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        assert session.process is process
        assert session.environment["STLV_TRAFORO_DEV_BINARY"] == "/test/local/traforo"
        assert session.environment["AWS_PROFILE"] == "captured-profile"
    finally:
        session._log_file.close()


@pytest.mark.parametrize(
    "scenario",
    [
        (["proof", "--yes"], "n\n", True, False),
        (["proof"], "y\n", True, True),
        (["proof"], "n\n", False, True),
    ],
)
def test_real_dev_cli_preserves_shared_environment_confirmation(cli, monkeypatch, scenario):
    arguments, answer, runs, prompt = scenario
    monkeypatch.setattr(cli, "determine_env", lambda *_args, **_kwargs: "proof")
    cli.get_environment_confirmation_info.return_value = ("proof-app", True)
    result = CliRunner().invoke(cli.dev, arguments, input=answer)
    assert result.exit_code == 0, result.output
    assert ("Deploy to proof?" in result.output) is prompt
    if runs:
        cli.run_dev.assert_called_once_with("proof", show_unchanged=False)
    else:
        cli.run_dev.assert_not_called()
