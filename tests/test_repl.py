import asyncio
import json
import sys
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import Mock

import boto3
import pulumi
from click.testing import CliRunner
from pytest import fixture, mark, raises
from rich.console import Console

from stelvio import signals
from stelvio.aws.s3 import Bucket
from stelvio.context import _ContextStore
from stelvio.link import LinkConfig
from stelvio.rich_deployment_handler import RichDeploymentHandler

pytest_plugins = ["tests.test_local_execute"]


@fixture
def standard_shell(monkeypatch):
    monkeypatch.setattr(
        "stelvio._repl.import_module", Mock(side_effect=ModuleNotFoundError(name="IPython"))
    )
    interact = Mock()
    monkeypatch.setattr("stelvio._repl.code.interact", interact)
    return interact


def invoke_repl(local_cli, *arguments):
    return CliRunner().invoke(local_cli.command, ["test", "--yes", "--repl", *arguments])


def test_repl_follows_completion_and_callbacks_with_same_snapshot(
    phase_app, local_pipeline, local_cli, standard_shell
):
    phase_app.run(lambda: Bucket("my-bucket"))
    order = []
    snapshots = []

    @phase_app.local_execute
    def first(components, argv):
        assert argv == [sys.argv[0], "arg1"]
        order.append("first")
        snapshots.append(components)

    @phase_app.local_execute
    async def second(components):
        await asyncio.sleep(0)
        assert components is snapshots[0]
        order.append("second")

    phase_app.on(signals.after_deploy)(lambda event: order.append("ordinary"))
    local_pipeline.commands.RichDeploymentHandler.return_value.show_completion.side_effect = (
        lambda: order.append("completion")
    )

    def shell(**kwargs):
        order.append("repl")
        context = kwargs["local"]
        assert context == {"components": snapshots[0], "boto3": boto3}
        assert context["components"] is snapshots[0]
        assert isinstance(context["components"].my_bucket.bucket_name, str)
        assert not local_pipeline.home.file_exists("lock/demo/test.json")
        assert list((local_pipeline.root / "work").iterdir()) == []
        with raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()

    standard_shell.side_effect = shell
    result = invoke_repl(local_cli, "--", "arg1")
    assert result.exit_code == 0, result.output
    assert order == ["ordinary", "completion", "first", "second", "repl"]


@mark.parametrize("callbacks", [False, True])
def test_repl_collects_values_without_running_skipped_callbacks(
    phase_app, local_pipeline, local_cli, standard_shell, callbacks
):
    phase_app.run(lambda: Bucket("my-bucket"))
    phase_app.set_user_link_for(
        Bucket,
        lambda bucket: LinkConfig(properties={"custom": pulumi.Output.secret("secret-value")}),
    )
    callback = Mock()
    if callbacks:
        phase_app.local_execute(callback)
    result = invoke_repl(local_cli, "--skip-local-execute", "--", "ignored")
    assert result.exit_code == 0, result.output
    callback.assert_not_called()
    standard_shell.assert_called_once()
    context = standard_shell.call_args.kwargs["local"]
    assert set(context) == {"components", "boto3"}
    assert context["boto3"] is boto3
    assert asdict(context["components"]) == {"my_bucket": {"custom": "secret-value"}}
    source = (local_pipeline.root / "stlv_local.py").read_text()
    assert "custom: str" in source
    assert "secret-value" not in source
    assert "secret-value" not in result.output


def test_repl_empty_view_and_collection_isolation(
    phase_app, local_pipeline, local_cli, standard_shell
):
    phase_app.run(lambda: None)
    result = invoke_repl(local_cli)
    assert result.exit_code == 0, result.output
    assert asdict(standard_shell.call_args.kwargs["local"]["components"]) == {}
    source = local_pipeline.root / "stlv_local.py"
    assert source.exists()
    source.unlink()
    standard_shell.reset_mock()
    _ContextStore.clear()
    result = CliRunner().invoke(local_cli.command, ["test", "--yes"])
    assert result.exit_code == 0, result.output
    assert not source.exists()
    standard_shell.assert_not_called()


@mark.parametrize("mode", [[], ["--json"], ["--stream"]])
@mark.parametrize("failure", [False, True])
def test_repl_machine_output_and_post_summary_failure(  # noqa: PLR0913
    phase_app, local_pipeline, local_cli, standard_shell, monkeypatch, mode, failure
):
    phase_app.run(lambda: None)
    monkeypatch.setattr(local_pipeline.commands, "RichDeploymentHandler", RichDeploymentHandler)
    monkeypatch.setattr(local_pipeline.commands, "console", Console())
    errors = []
    phase_app.on(signals.on_error)(errors.append)
    original = ValueError("shell failed")

    def shell(**kwargs):
        print("shell-result")
        if failure:
            raise original

    standard_shell.side_effect = shell
    result = invoke_repl(local_cli, *mode)
    assert result.exit_code == (1 if failure else 0), result.output
    before_shell, after_shell = result.stdout.split("shell-result")
    assert not after_shell.strip()
    if "--json" in mode:
        summary = json.loads(before_shell)
        assert summary["status"] == "success"
        assert summary["exit_code"] == 0
    elif "--stream" in mode:
        records = [json.loads(line) for line in before_shell.splitlines()]
        assert [record["event"] for record in records] == ["start", "summary"]
        assert records[-1]["status"] == "success"
        assert records[-1]["exit_code"] == 0
    else:
        assert "Deployed in" in before_shell
    if failure:
        assert len(errors) == 1
        assert errors[0].error is original
        assert errors[0].phase == "repl"
        assert "shell failed" in result.stderr
    else:
        assert errors == []


@mark.parametrize("policy", ["fail", "continue"])
def test_callback_error_policy_controls_repl_start(phase_app, local_cli, standard_shell, policy):
    phase_app.run(lambda: None)
    phase_app.local_execute(Mock(side_effect=ValueError("callback failed")), errors=policy)
    result = invoke_repl(local_cli)
    assert result.exit_code == (1 if policy == "fail" else 0)
    assert standard_shell.call_count == (0 if policy == "fail" else 1)


@mark.parametrize("failure", ["completion", "provision", "cleanup", "callback_cancel"])
def test_unsuccessful_finalization_suppresses_repl(  # noqa: PLR0913
    phase_app, local_pipeline, local_cli, standard_shell, monkeypatch, failure
):
    phase_app.run(lambda: None)
    if failure == "completion":
        local_pipeline.commands.RichDeploymentHandler.return_value.show_completion.side_effect = (
            ValueError("render failed")
        )
    elif failure == "provision":
        monkeypatch.setattr(
            "tests.test_local_execute.PulumiStack.up", Mock(side_effect=ValueError("up failed"))
        )
    elif failure == "cleanup":
        monkeypatch.setattr(
            "stelvio.command_run.CommandRun._cleanup",
            Mock(side_effect=ValueError("cleanup failed")),
        )
    else:
        phase_app.local_execute(Mock(side_effect=KeyboardInterrupt()))
    result = invoke_repl(local_cli)
    assert result.exit_code != 0
    standard_shell.assert_not_called()


def test_escaping_repl_interruption_notifies_cancellation_once(
    phase_app, local_cli, standard_shell
):
    phase_app.run(lambda: None)
    cancelled = []
    errors = []
    phase_app.on(signals.on_cancel)(cancelled.append)
    phase_app.on(signals.on_error)(errors.append)
    standard_shell.side_effect = KeyboardInterrupt()
    result = invoke_repl(local_cli)
    assert result.exit_code != 0
    assert len(cancelled) == 1
    assert cancelled[0].phase == "repl"
    assert errors == []


@mark.parametrize("status", [None, 0, 1])
def test_repl_system_exit_is_success(phase_app, local_cli, standard_shell, status):
    phase_app.run(lambda: None)
    standard_shell.side_effect = SystemExit(status)
    result = invoke_repl(local_cli)
    assert result.exit_code == 0, result.output


def test_ipython_receives_context_without_starting_standard_shell(
    phase_app, local_cli, standard_shell, monkeypatch
):
    phase_app.run(lambda: None)
    embed = Mock()
    monkeypatch.setattr(
        "stelvio._repl.import_module", Mock(return_value=SimpleNamespace(embed=embed))
    )
    result = invoke_repl(local_cli)
    assert result.exit_code == 0, result.output
    embed.assert_called_once()
    context = embed.call_args.kwargs["user_ns"]
    assert set(context) == {"components", "boto3"}
    assert asdict(context["components"]) == {}
    assert context["boto3"] is boto3
    standard_shell.assert_not_called()


@mark.parametrize("stage", ["import", "embed"])
def test_ipython_failure_reports_and_falls_back(
    phase_app, local_cli, standard_shell, monkeypatch, stage
):
    phase_app.run(lambda: None)
    original = RuntimeError("IPython failed")
    module = (
        Mock(side_effect=original)
        if stage == "import"
        else Mock(return_value=SimpleNamespace(embed=Mock(side_effect=original)))
    )
    monkeypatch.setattr("stelvio._repl.import_module", module)
    result = invoke_repl(local_cli)
    assert result.exit_code == 0, result.output
    assert "IPython REPL failed: RuntimeError: IPython failed" in result.stderr
    standard_shell.assert_called_once()
    assert standard_shell.call_args.kwargs["banner"] == ""
    assert standard_shell.call_args.kwargs["exitmsg"] == ""


@mark.parametrize("exception", [SystemExit(1), KeyboardInterrupt()])
def test_ipython_exit_or_interruption_does_not_start_fallback(
    phase_app, local_cli, standard_shell, monkeypatch, exception
):
    phase_app.run(lambda: None)
    monkeypatch.setattr(
        "stelvio._repl.import_module",
        Mock(return_value=SimpleNamespace(embed=Mock(side_effect=exception))),
    )
    result = invoke_repl(local_cli)
    assert result.exit_code == (0 if isinstance(exception, SystemExit) else 1)
    standard_shell.assert_not_called()


def test_repl_reads_redirected_stdin_and_eof_without_tty(phase_app, local_cli, monkeypatch):
    phase_app.run(lambda: Bucket("my-bucket"))
    monkeypatch.setattr(
        "stelvio._repl.import_module", Mock(side_effect=ModuleNotFoundError(name="IPython"))
    )
    result = CliRunner().invoke(
        local_cli.command,
        ["test", "--yes", "--repl"],
        input="print('ready=', boto3.__name__, components.my_bucket.bucket_name)\n",
    )
    assert result.exit_code == 0, result.output
    assert "ready= boto3 demo-test-my-bucket-test-name" in result.stdout
    assert "IPython REPL failed" not in result.stderr


def test_repl_and_callback_share_one_collection(phase_app, local_cli, standard_shell):
    phase_app.run(lambda: Bucket("my-bucket"))
    creator = Mock(return_value=LinkConfig(properties={"custom": "value"}))
    phase_app.set_user_link_for(Bucket, creator)
    phase_app.local_execute(Mock())
    result = invoke_repl(local_cli)
    assert result.exit_code == 0, result.output
    creator.assert_called_once()
    assert standard_shell.call_args.kwargs["local"]["components"].my_bucket.custom == "value"


def test_missing_ipython_dependency_reports_fallback(
    phase_app, local_cli, standard_shell, monkeypatch
):
    phase_app.run(lambda: None)
    monkeypatch.setattr(
        "stelvio._repl.import_module",
        Mock(side_effect=ModuleNotFoundError("missing dependency", name="traitlets")),
    )
    result = invoke_repl(local_cli)
    assert result.exit_code == 0, result.output
    assert "missing dependency" in result.stderr
    standard_shell.assert_called_once()
