import asyncio
import json
import re
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from click.testing import CliRunner
from pulumi import Output
from pulumi.automation import OutputValue
from rich.console import Console

from stelvio import signals
from stelvio._signals import _command_scope, _current, _record_result
from stelvio.app import StelvioApp
from stelvio.aws.function import Function
from stelvio.bridge.local import listener
from stelvio.bridge.remote.infrastructure import AppSyncResource
from stelvio.command_run import _PRELOADED_APP_CONFIGS, CommandRun
from stelvio.config import AwsConfig, StelvioAppConfig
from stelvio.context import AppContext
from stelvio.exceptions import StelvioValidationError
from tests.cli_test_helpers import FakeCommandRun, FakeConsole, import_cli_commands_module


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setattr(StelvioApp, "_StelvioApp__instance", None)
    app = StelvioApp("demo")
    app.config(lambda env: StelvioAppConfig(environments=["test", "prod"]))
    monkeypatch.setattr("stelvio.command_run.import_module", Mock())
    monkeypatch.setattr("stelvio.command_run.get_project_root", Path.cwd)
    monkeypatch.setattr("stelvio.command_run.get_user_env", lambda: "alice")
    return app


class TracedRun(FakeCommandRun):
    def __init__(self, trace):
        super().__init__({"checkpoint": {"latest": {"resources": []}}})
        self.trace = trace
        self.saved_config = None
        self.setup_error = None
        self.cleanup_error = None

    def __enter__(self):
        self.trace.append("setup")
        self.saved_config = _PRELOADED_APP_CONFIGS.pop("test")[1]
        if self.setup_error:
            raise self.setup_error
        return self

    def __exit__(self, exc_type, error, tb):
        self.trace.extend(["unlock", "cleanup"])
        if self.cleanup_error:
            raise self.cleanup_error
        return False

    def stop_partial_push(self):
        self.trace.append("checkpoint_stop")

    def push_state(self):
        self.trace.append("state_save")

    def create_state_snapshot(self):
        self.trace.append("snapshot")

    def complete_update(self, errors=None):
        self.trace.append(("complete", errors))


@pytest.fixture
def commands(monkeypatch, app):
    module = import_cli_commands_module()
    trace = []
    run = TracedRun(trace)
    monkeypatch.setattr(module, "CommandRun", Mock(return_value=run))
    monkeypatch.setattr(module, "console", FakeConsole())
    monkeypatch.setattr(module, "RichDeploymentHandler", Mock())
    monkeypatch.setattr(module, "clean_stale_dependency_caches", Mock())
    monkeypatch.setattr(module, "print_operation_header", Mock())
    monkeypatch.setattr(module, "_show_simple_error", Mock())
    monkeypatch.setattr(module, "run_bridge_server", Mock())
    monkeypatch.setattr(module.ProviderStore, "region", lambda: "us-east-1")
    return SimpleNamespace(module=module, run=run, trace=trace)


def invoke(commands, operation):
    kwargs = {"skip_confirm": True} if operation == "destroy" else {}
    getattr(commands.module, f"run_{operation}")("test", **kwargs)


@pytest.mark.parametrize("operation", ["deploy", "diff", "destroy", "refresh"])
def test_operation_signals_surround_work_and_cleanup(app, commands, operation):
    events = []

    def receive(event):
        events.append(event)
        commands.trace.append(event.signal.name)

    app.on(getattr(signals, f"before_{operation}"))(receive)
    app.on(getattr(signals, f"after_{operation}"))(receive)
    invoke(commands, operation)

    before, after = events
    assert commands.trace[0] == f"before_{operation}"
    assert commands.trace[-3:] == ["unlock", "cleanup", f"after_{operation}"]
    assert (before.app_name, before.env, before.command, before.operation, before.dev_mode) == (
        "demo",
        "test",
        operation,
        operation,
        False,
    )
    assert isinstance(
        after,
        {
            "deploy": signals.DeployEvent,
            "diff": signals.DiffEvent,
            "destroy": signals.DestroyEvent,
            "refresh": signals.RefreshEvent,
        }[operation],
    )
    assert before.result is None
    assert after.result == signals.OperationResult()
    if operation != "diff":
        assert commands.trace.index("state_save") < commands.trace.index("unlock")
        assert commands.trace.index(("complete", None)) < commands.trace.index("unlock")
    assert _current.get() is None


def test_sync_and_async_handlers_are_awaited_in_registration_order(app, commands):
    order = []

    @app.on(signals.before_deploy)
    async def first(event):
        order.append("async start")
        await asyncio.sleep(0)
        order.append("async end")

    @app.on(signals.before_deploy)
    def second(event):
        order.append("sync")

    app.on(signals.before_deploy)(first)
    invoke(commands, "deploy")
    assert order == ["async start", "async end", "sync"]


def test_before_handlers_edit_configuration_and_options_with_late_edit_rejection(app, commands):
    retained = []

    @app.on(signals.before_diff)
    def change(event):
        retained.append(event)
        event.replace_config(replace(event.config, aws=AwsConfig(region="eu-west-1")))
        event.update_options(show_unchanged=True, compact=True)

    @app.on(signals.before_diff)
    def inspect_changes(event):
        assert event.config.aws.region == "eu-west-1"
        assert event.options == signals.OperationOptions(show_unchanged=True, compact=True)
        event.config.tags["ignored"] = "detached snapshot"

    invoke(commands, "diff")
    assert commands.run.saved_config.aws.region == "eu-west-1"
    assert commands.run.saved_config.tags == {}
    assert commands.module.RichDeploymentHandler.call_args.kwargs["compact"] is True
    assert commands.module.RichDeploymentHandler.call_args.kwargs["show_unchanged"] is True
    with pytest.raises(RuntimeError, match="before-operation"):
        retained[0].replace_config(StelvioAppConfig())
    with pytest.raises(FrozenInstanceError, match="cannot assign"):
        retained[0].env = "other"


@pytest.mark.parametrize(
    ("changes", "error_type", "message"),
    [
        ({"show_unchanged": "yes"}, TypeError, "show_unchanged must be a bool"),
        ({"compact": True}, ValueError, "compact is only supported for diff"),
        ({"json_output": True}, TypeError, "unexpected keyword argument"),
    ],
)
def test_input_validation(app, commands, changes, error_type, message):
    app.on(signals.before_deploy)(lambda event: event.update_options(**changes))
    with pytest.raises(error_type, match=message):
        invoke(commands, "deploy")
    assert commands.trace == []


@pytest.mark.parametrize("operation", ["deploy", "diff", "destroy", "refresh"])
def test_before_rejection_prevents_setup_and_after_signal(app, commands, operation):
    error = ValueError("rejected")
    errors = []
    after = Mock()

    def reject(event):
        raise error

    app.on(getattr(signals, f"before_{operation}"))(reject)
    app.on(getattr(signals, f"after_{operation}"))(after)
    app.on(signals.on_error)(errors.append)
    with pytest.raises(ValueError, match="rejected") as raised:
        invoke(commands, operation)
    assert raised.value is error
    assert errors[0].error is error
    assert errors[0].phase == f"before_{operation}"
    assert errors[0].handler.endswith("reject")
    assert len(errors) == 1
    assert commands.trace == []
    after.assert_not_called()


def test_continue_reports_to_stderr_and_runs_next_handler(app, commands, capsys):
    next_handler = Mock()
    errors = Mock()

    @app.on(signals.before_deploy, errors="continue")
    def broken(event):
        raise ValueError("notification failed")

    app.on(signals.before_deploy)(next_handler)
    app.on(signals.on_error)(errors)
    invoke(commands, "deploy")
    next_handler.assert_called_once()
    errors.assert_not_called()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "ValueError: notification failed" in captured.err


@pytest.mark.parametrize(
    "phase", ["setup", "provision", "state_save", "update_complete", "cleanup"]
)
def test_failures_notify_once_and_never_emit_success(app, commands, phase):
    primary = ValueError(phase)
    errors = []
    after = Mock()
    app.on(signals.on_error)(errors.append)
    app.on(signals.after_deploy)(after)
    if phase == "setup":
        commands.run.setup_error = primary
    elif phase == "provision":
        commands.run.stack.up = Mock(side_effect=primary)
    elif phase == "state_save":
        commands.run.push_state = Mock(side_effect=primary)
    elif phase == "update_complete":
        commands.run.complete_update = Mock(side_effect=primary)
    else:
        commands.run.cleanup_error = primary
    with pytest.raises(ValueError, match=phase) as raised:
        invoke(commands, "deploy")
    assert raised.value is primary
    assert len(errors) == 1
    assert errors[0].error is primary
    after.assert_not_called()
    if phase != "setup":
        assert commands.trace[-2:] == ["unlock", "cleanup"]
    if phase == "state_save":
        assert ("complete", ["state_save"]) in commands.trace


def test_recovery_and_error_handler_failures_preserve_original(app, commands, capsys):
    original = ValueError("provision failed")
    commands.run.stack.up = Mock(side_effect=original)
    commands.run.push_state = Mock(side_effect=OSError("save failed"))
    notified = []

    @app.on(signals.on_error)
    def broken(event):
        raise RuntimeError("error handler failed")

    app.on(signals.on_error)(notified.append)
    with pytest.raises(ValueError, match="provision failed") as raised:
        invoke(commands, "deploy")
    assert raised.value is original
    assert len(notified) == 1
    assert notified[0].error is original
    assert notified[0].phase == "provision"
    assert ("complete", ["provision failed"]) in commands.trace
    assert commands.trace[-2:] == ["unlock", "cleanup"]
    assert "save failed" in capsys.readouterr().err


@pytest.mark.parametrize("operation", ["destroy", "refresh"])
def test_no_op_has_explicit_result(app, commands, operation):
    commands.run.has_deployed = False
    events = []
    app.on(getattr(signals, f"after_{operation}"))(events.append)
    invoke(commands, operation)
    assert events[0].result == signals.OperationResult(no_op=True)
    assert commands.trace.index(("complete", None)) < commands.trace.index("unlock")


def test_destroy_confirmation_decline_prevents_backend_setup(app, commands, monkeypatch):
    cancelled = []
    after = Mock()
    app.on(signals.on_cancel)(cancelled.append)
    app.on(signals.after_destroy)(after)
    monkeypatch.setattr(commands.module, "_confirm_destroy", lambda env: False)
    commands.module.run_destroy("test")
    assert cancelled[0].reason == "confirmation_declined"
    assert commands.trace == []
    after.assert_not_called()


def test_interruption_recovers_and_emits_cancellation_only(app, commands):
    commands.run.stack.up = Mock(side_effect=KeyboardInterrupt)
    errors = Mock()
    cancelled = []
    after = Mock()
    app.on(signals.on_error)(errors)
    app.on(signals.on_cancel)(cancelled.append)
    app.on(signals.after_deploy)(after)
    with pytest.raises(KeyboardInterrupt):
        invoke(commands, "deploy")
    assert commands.trace[-2:] == ["unlock", "cleanup"]
    assert len(cancelled) == 1
    assert cancelled[0].reason == "interrupted"
    assert cancelled[0].phase == "provision"
    errors.assert_not_called()
    after.assert_not_called()


def test_configuration_failure_notifies_registered_handlers(app, commands):
    events = []
    before = Mock()
    app.on(signals.on_error)(events.append)
    app.on(signals.before_deploy)(before)
    app._config_func = Mock(side_effect=ValueError("config failed"))
    with pytest.raises(ValueError, match="config failed"):
        invoke(commands, "deploy")
    assert events[0].config is None
    assert events[0].phase == "config"
    before.assert_not_called()


def test_final_configuration_is_revalidated_before_setup(app, commands):
    app.on(signals.before_deploy)(lambda event: event.replace_config(StelvioAppConfig()))
    with pytest.raises(StelvioValidationError, match="Invalid environment"):
        invoke(commands, "deploy")
    assert commands.trace == []


def test_registration_rejects_custom_signals_and_invalid_policy(app):
    with pytest.raises(ValueError, match="built-in"):
        app.on(signals.Signal("before_deploy"))(lambda event: None)
    with pytest.raises(ValueError, match="errors must be"):
        app.on(signals.before_deploy, errors="ignore")(lambda event: None)


def test_command_run_cleanup_preserves_primary_and_attempts_both_steps(
    app, monkeypatch, capsys, tmp_path
):
    run = CommandRun("test")
    run._locked = True
    run._workdir = tmp_path
    original = ValueError("original")
    unlock = Mock(side_effect=OSError("unlock failed"))
    cleanup = Mock(side_effect=OSError("cleanup failed"))
    monkeypatch.setattr(run, "_unlock", unlock)
    monkeypatch.setattr(run, "_clean_workdir", cleanup)
    with _command_scope("deploy", "test") as session:
        session.prepared_config(app, StelvioAppConfig())
        assert run.__exit__(ValueError, original, None) is False
        session.cancelled = True
    unlock.assert_called_once()
    cleanup.assert_called_once()
    assert "cleanup failed" in capsys.readouterr().err


def test_cli_confirmation_uses_handler_configuration(app, commands, monkeypatch):
    from tests.cli_test_helpers import import_cli_module

    cli = import_cli_module()
    monkeypatch.setattr(cli, "ensure_pulumi", Mock())
    monkeypatch.setattr(cli, "determine_env", lambda *args, **kwargs: "test")
    monkeypatch.setattr(cli, "run_deploy", commands.module.run_deploy)
    app.on(signals.before_deploy)(lambda event: event.replace_config(StelvioAppConfig()))
    result = CliRunner().invoke(cli.deploy, ["test", "--yes"])
    assert result.exit_code == 2
    assert commands.trace == []


class Socket:
    def __init__(
        self, trace, ack='{"type":"subscribe_success", "id":"request-sub"}', loop_error=None
    ):
        self.trace = trace
        self.send = AsyncMock()
        self.recv = AsyncMock(return_value=ack)
        self.loop_error = loop_error
        self.close = AsyncMock(side_effect=lambda: self.trace.append("socket_closed"))

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.loop_error:
            raise self.loop_error
        raise StopAsyncIteration


@pytest.fixture
def bridge(monkeypatch, commands):
    socket = Socket(commands.trace)
    resource = AppSyncResource("api", "http", "realtime", "key")
    monkeypatch.setattr(listener, "discover_or_create_appsync", lambda *args: resource)
    monkeypatch.setattr(listener, "connect_to_appsync", AsyncMock(return_value=socket))
    monkeypatch.setattr(listener, "Console", FakeConsole)
    monkeypatch.setattr(commands.module, "run_bridge_server", listener.run_bridge_server)
    return socket


def test_dev_deploy_bridge_and_shutdown_order(app, commands, bridge):
    events = []

    async def receive(event):
        await asyncio.sleep(0)
        events.append(event)
        commands.trace.append(event.signal.name)

    names = [
        "before_dev",
        "before_deploy",
        "after_deploy",
        "before_dev_bridge_start",
        "after_dev_bridge_start",
        "before_dev_bridge_stop",
        "after_dev_bridge_stop",
        "after_dev",
    ]
    for name in names:
        app.on(getattr(signals, name))(receive)
    invoke(commands, "dev")
    assert [event.signal.name for event in events] == names
    assert all(event.command == "dev" and event.dev_mode for event in events)
    assert [event.operation for event in events] == [
        "dev",
        "deploy",
        "deploy",
        "dev",
        "dev",
        "dev",
        "dev",
        "dev",
    ]
    assert commands.trace.index("cleanup") < commands.trace.index("after_deploy")
    assert commands.trace.index("before_dev_bridge_stop") < commands.trace.index("socket_closed")
    assert commands.trace.index("socket_closed") < commands.trace.index("after_dev_bridge_stop")
    assert events[3].ready is False
    assert events[4].ready is True
    bridge.send.assert_awaited_once()
    bridge.recv.assert_awaited_once()
    bridge.close.assert_awaited_once()


@pytest.mark.parametrize("ack", ['{"type":"subscribe_error"}', '{"type":"unexpected"}'])
def test_bridge_readiness_requires_subscription_ack(app, commands, bridge, ack):
    bridge.recv.return_value = ack
    ready = Mock()
    after_dev = Mock()
    errors = []
    app.on(signals.after_dev_bridge_start)(ready)
    app.on(signals.after_dev)(after_dev)
    app.on(signals.on_error)(errors.append)
    with pytest.raises(ConnectionError, match="subscription"):
        invoke(commands, "dev")
    ready.assert_not_called()
    after_dev.assert_not_called()
    bridge.close.assert_awaited_once()
    assert len(errors) == 1
    assert errors[0].operation == "dev"


def test_dev_ctrl_c_runs_shutdown_handlers_and_only_cancellation(app, commands, bridge):
    bridge.loop_error = asyncio.CancelledError()
    events = []
    app.on(signals.before_dev_bridge_stop)(events.append)
    app.on(signals.after_dev_bridge_stop)(events.append)
    app.on(signals.on_cancel)(events.append)
    errors = Mock()
    after_dev = Mock()
    app.on(signals.on_error)(errors)
    app.on(signals.after_dev)(after_dev)
    with pytest.raises(asyncio.CancelledError):
        invoke(commands, "dev")
    assert [event.signal.name for event in events] == [
        "before_dev_bridge_stop",
        "after_dev_bridge_stop",
        "on_cancel",
    ]
    assert events[-1].phase == "dev_bridge"
    bridge.close.assert_awaited_once()
    errors.assert_not_called()
    after_dev.assert_not_called()


def test_shutdown_hook_cannot_prevent_socket_cleanup(app, commands, bridge):
    events = []

    @app.on(signals.before_dev_bridge_stop)
    async def reject(event):
        raise ValueError("shutdown hook failed")

    app.on(signals.on_error)(events.append)
    with pytest.raises(ValueError, match="shutdown hook failed"):
        invoke(commands, "dev")
    bridge.close.assert_awaited_once()
    assert len(events) == 1
    assert events[0].phase == "before_dev_bridge_stop"


@pytest.mark.parametrize("operation", ["deploy", "dev"])
def test_nested_deployment_failure_notifies_once(app, commands, operation):
    events = []
    commands.run.stack.up = Mock(side_effect=ValueError("deployment failed"))
    app.on(signals.on_error)(events.append)
    with pytest.raises(ValueError, match="deployment failed"):
        invoke(commands, operation)
    assert len(events) == 1
    assert events[0].command == operation
    assert events[0].operation == "deploy"
    commands.module.run_bridge_server.assert_not_called()


def test_after_failure_suppresses_success_summary(app, commands):
    @app.on(signals.after_deploy)
    def broken(event):
        raise ValueError("after hook failed")

    with pytest.raises(ValueError, match="after hook failed"):
        commands.module.run_deploy("test", json_output=True)
    handler = commands.module.RichDeploymentHandler.return_value
    assert handler.build_json_summary.call_count == 1
    assert handler.build_json_summary.call_args.kwargs["status"] == "failed"
    assert commands.trace[-2:] == ["unlock", "cleanup"]


def test_cli_declined_confirmation_emits_cancel_after_before_hooks(app, commands, monkeypatch):
    from tests.cli_test_helpers import import_cli_module

    cli = import_cli_module()
    monkeypatch.setattr(cli, "ensure_pulumi", Mock())
    monkeypatch.setattr(cli, "determine_env", lambda *args, **kwargs: "test")
    monkeypatch.setattr(cli, "run_deploy", commands.module.run_deploy)
    events = []
    app.on(signals.before_deploy)(events.append)
    app.on(signals.on_cancel)(events.append)
    result = CliRunner().invoke(cli.deploy, ["test"], input="n\n")
    assert result.exit_code == 0
    assert [event.signal.name for event in events] == ["before_deploy", "on_cancel"]
    assert events[1].reason == "confirmation_declined"
    assert commands.trace == []


def test_customization_snapshots_preserve_pulumi_inputs(app, commands):
    value = Output.from_input(256)
    config = StelvioAppConfig(
        environments=["test"],
        customize={Function: {"function": {"memory_size": value}}},
    )
    app._config_func = lambda env: config

    @app.on(signals.before_deploy)
    def inspect(event):
        snapshot = event.config
        assert snapshot.customize[Function]["function"]["memory_size"] is value
        snapshot.customize[Function]["function"]["memory_size"] = 512
        assert event.config.customize[Function]["function"]["memory_size"] is value

    invoke(commands, "deploy")
    assert commands.run.saved_config.customize[Function]["function"]["memory_size"] is value


def test_readiness_ignores_heartbeats_and_foreign_subscription_acks(app, commands, bridge):
    bridge.recv.side_effect = [
        '{"type":"ka"}',
        '{"type":"subscribe_success", "id":"other"}',
        '{"type":"subscribe_success", "id":"request-sub"}',
    ]
    ready = []
    app.on(signals.after_dev_bridge_start)(ready.append)
    invoke(commands, "dev")
    assert len(ready) == 1
    assert ready[0].ready is True
    assert bridge.recv.await_count == 3


def test_failed_connection_handshake_closes_socket(app, commands, monkeypatch):
    socket = Socket(commands.trace, ack='{"type":"connection_error"}')
    monkeypatch.setattr(listener.websockets, "connect", AsyncMock(return_value=socket))
    monkeypatch.setattr(
        listener,
        "discover_or_create_appsync",
        lambda *args: AppSyncResource(
            "api",
            "http",
            "realtime",
            "key",
        ),
    )
    monkeypatch.setattr(commands.module, "run_bridge_server", listener.run_bridge_server)
    events = []
    app.on(signals.on_error)(events.append)
    with pytest.raises(ConnectionError, match="Expected connection_ack"):
        invoke(commands, "dev")
    socket.close.assert_awaited_once()
    assert len(events) == 1
    assert events[0].phase == "before_dev_bridge_start"


@pytest.mark.parametrize("mode", ["--json", "--stream"])
def test_after_failure_emits_one_machine_terminal_result(app, commands, monkeypatch, mode):
    from stelvio.rich_deployment_handler import RichDeploymentHandler
    from tests.cli_test_helpers import import_cli_module

    cli = import_cli_module()
    monkeypatch.setattr(cli, "ensure_pulumi", Mock())
    monkeypatch.setattr(cli, "determine_env", lambda *args, **kwargs: "test")
    monkeypatch.setattr(cli, "run_deploy", commands.module.run_deploy)
    monkeypatch.setattr(commands.module, "RichDeploymentHandler", RichDeploymentHandler)
    monkeypatch.setattr(commands.module, "console", Console())

    @app.on(signals.after_deploy)
    def broken(event):
        raise ValueError("after hook failed")

    result = CliRunner().invoke(cli.deploy, ["test", "--yes", mode])
    assert result.exit_code == 1
    if mode == "--json":
        payload = json.loads(result.stdout)
        assert payload["status"] == "failed"
        assert payload["exit_code"] == 1
    else:
        payloads = [json.loads(line) for line in result.stdout.splitlines()]
        assert [payload["event"] for payload in payloads] == ["start", "summary"]
        assert payloads[-1]["status"] == "failed"


def test_guide_python_examples_execute(app, commands, monkeypatch):
    monkeypatch.setattr(StelvioApp, "_StelvioApp__instance", None)
    monkeypatch.setattr("stelvio.command_run.get_user_env", lambda: "test")
    guide = Path("docs/docs/concepts/signals.md").read_text()
    namespace = {}
    for index, code in enumerate(re.findall(r"```python\n(.*?)```", guide, re.DOTALL)):
        exec(compile(code, f"signals.md example {index}", "exec"), namespace)  # noqa: S102
    invoke(commands, "deploy")
    assert commands.run.saved_config.aws.region == "eu-west-1"


@pytest.fixture
def real_run(app, monkeypatch, tmp_path):
    home = Mock()
    home.file_exists.return_value = False
    home.read_file.return_value = False
    ctx = AppContext(name="demo", env="test", aws=AwsConfig(), home="aws")
    monkeypatch.setattr("stelvio.command_run._setup_app_home_storage", lambda *args: (home, ctx))
    monkeypatch.setattr(
        "stelvio.command_run._get_or_create_passphrase", lambda *args: "passphrase"
    )
    monkeypatch.setattr("stelvio.command_run.get_dot_stelvio_dir", lambda: tmp_path)
    return CommandRun("test", lock_as="deploy", state_only=True), home, tmp_path


def complete_run(run, outputs=None):
    with _command_scope("deploy", "test") as session:
        session.prepare()
        with run:
            run.complete_update()
            _record_result(outputs=outputs)


def test_audit_creation_failure_releases_uploaded_lock_and_removes_workdir(
    app,
    real_run,
    monkeypatch,
):
    run, home, root = real_run
    original = ValueError("audit creation failed")
    monkeypatch.setattr(run, "_create_update", Mock(side_effect=original))
    errors = []
    success = Mock()
    app.on(signals.on_error)(errors.append)
    app.on(signals.after_deploy)(success)
    with pytest.raises(ValueError, match="audit creation failed") as raised:
        complete_run(run)
    assert raised.value is original
    home.write_file.assert_called_once()
    assert home.write_file.call_args.args[0] == "lock/demo/test.json"
    home.delete_file.assert_called_once_with("lock/demo/test.json")
    assert list(root.iterdir()) == []
    assert len(errors) == 1
    assert errors[0].error is original
    success.assert_not_called()


def test_unlock_failure_cleans_workdir_and_suppresses_success(app, real_run):
    run, home, root = real_run
    original = OSError("unlock failed")
    home.delete_file.side_effect = original
    errors = []
    success = Mock()
    app.on(signals.on_error)(errors.append)
    app.on(signals.after_deploy)(success)
    with pytest.raises(OSError, match="unlock failed") as raised:
        complete_run(run)
    assert raised.value is original
    assert list(root.iterdir()) == []
    assert len(errors) == 1
    assert errors[0].error is original
    assert errors[0].phase == "lock_release"
    success.assert_not_called()


def test_generated_update_id_and_outputs_survive_cleanup(app, real_run):
    run, home, root = real_run
    events = []
    app.on(signals.after_deploy)(events.append)
    complete_run(run, outputs={"user_defined": {"endpoint": "https://example.com"}})
    event = events[0]
    assert re.fullmatch(r"\d{14}-[0-9a-f]{8}", event.update_id)
    assert event.outputs == {"user_defined": {"endpoint": "https://example.com"}}
    assert list(root.iterdir()) == []
    home.delete_file.assert_called_once_with("lock/demo/test.json")


def test_after_deploy_exposes_resolved_exports(app, commands):
    commands.run.stack._outputs = {"endpoint": OutputValue("https://example.com", False)}
    events = []
    app.on(signals.after_deploy)(events.append)
    invoke(commands, "deploy")
    assert events[0].outputs == {"user_defined": {"endpoint": "https://example.com"}}
