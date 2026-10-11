import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pulumi
from pytest import fixture, mark, raises

from stelvio import signals
from stelvio._signals import _command_scope
from stelvio.app import StelvioApp
from stelvio.aws.s3 import Bucket
from stelvio.aws.topic import Topic
from stelvio.command_run import CommandRun
from stelvio.config import AwsConfig, StelvioAppConfig
from stelvio.context import _ContextStore
from tests.aws.pulumi_mocks import R
from tests.cli_test_helpers import FakeConsole, import_cli_commands_module


class MemoryHome:
    def __init__(self):
        self.files = {
            "state/demo/test.json": json.dumps(
                {"checkpoint": {"latest": {"resources": []}}}
            ).encode()
        }

    def file_exists(self, key):
        return key in self.files

    def write_file(self, key, path):
        self.files[key] = path.read_bytes()

    def read_file(self, key, path):
        if key not in self.files:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.files[key])
        return True

    def delete_file(self, key):
        del self.files[key]

    def delete_prefix(self, prefix):
        self.files = {
            key: value for key, value in self.files.items() if not key.startswith(prefix)
        }


class Stack:
    def __init__(self, program):
        self.program = program

    def up(self, **kwargs):
        asyncio.run(self.program())

    preview = up

    def destroy(self, **kwargs):
        pass

    refresh = destroy

    def outputs(self):
        return {}

    def export_stack(self):
        return SimpleNamespace(deployment={"resources": []})


@fixture
def phase_app(monkeypatch):
    monkeypatch.setattr(StelvioApp, "_StelvioApp__instance", None)
    app = StelvioApp("demo")
    app.config(
        lambda env: StelvioAppConfig(environments=["test"], aws=AwsConfig(region="us-east-1"))
    )
    monkeypatch.setattr("stelvio.command_run.import_module", Mock())
    monkeypatch.setattr("stelvio.command_run.get_project_root", Path.cwd)
    monkeypatch.setattr("stelvio.command_run.get_user_env", lambda: "alice")
    return app


@fixture
def pipeline(monkeypatch, phase_app, tmp_path):
    phase_app.run(lambda: None)
    home = MemoryHome()
    commands = import_cli_commands_module()
    monkeypatch.setattr(commands, "console", FakeConsole())
    monkeypatch.setattr(commands, "RichDeploymentHandler", Mock())
    monkeypatch.setattr(commands, "clean_stale_dependency_caches", Mock())
    monkeypatch.setattr(commands, "print_operation_header", Mock())
    monkeypatch.setattr("stelvio.command_run.AwsHome", lambda *args: home)
    monkeypatch.setattr("stelvio.command_run._init_storage", Mock(return_value="storage"))
    monkeypatch.setattr(
        "stelvio.command_run._get_or_create_passphrase", lambda *args: "passphrase"
    )
    monkeypatch.setattr("stelvio.command_run.get_dot_stelvio_dir", lambda: tmp_path)
    monkeypatch.setattr(
        "stelvio.command_run._create_stack",
        lambda *args: Stack(phase_app._get_pulumi_program_func()),
    )
    _ContextStore.clear()
    return SimpleNamespace(home=home, commands=commands, root=tmp_path)


def subscribe(app, names):
    events = []
    for name in names:
        app.on(getattr(signals, name))(events.append)
    return events


def test_deploy_phase_order_and_payloads(phase_app, pipeline):
    phases = [
        "config",
        "backend_setup",
        "lock_acquire",
        "state_load",
        "stack_setup",
        "provision",
        "app_run",
        "resource_creation",
        "state_save",
        "snapshot_create",
        "update_complete",
        "lock_release",
        "cleanup",
    ]
    events = subscribe(
        phase_app, [f"{edge}_{phase}" for phase in phases for edge in ("before", "after")]
    )
    operation = subscribe(phase_app, ["before_deploy", "after_deploy"])
    pipeline.commands.run_deploy("test")
    expected = [
        "before_config",
        "after_config",
        "before_backend_setup",
        "after_backend_setup",
        "before_lock_acquire",
        "after_lock_acquire",
        "before_state_load",
        "after_state_load",
        "before_stack_setup",
        "after_stack_setup",
        "before_provision",
        "before_app_run",
        "after_app_run",
        "before_resource_creation",
        "after_resource_creation",
        "after_provision",
        "before_state_save",
        "after_state_save",
        "before_snapshot_create",
        "after_snapshot_create",
        "before_update_complete",
        "after_update_complete",
        "before_lock_release",
        "after_lock_release",
        "before_cleanup",
        "after_cleanup",
    ]
    assert [event.signal.name for event in events] == expected
    by_name = {event.signal.name: event for event in events}
    assert all(isinstance(event, signals.PhaseEvent) for event in events)
    assert by_name["before_config"].config is None
    assert by_name["after_config"].config.aws.region == "us-east-1"
    assert by_name["after_state_load"].state_exists is True
    assert by_name["before_stack_setup"].stack_name == "organization/demo/test"
    assert by_name["after_snapshot_create"].snapshot_id == operation[-1].update_id
    assert by_name["after_update_complete"].errors == ()
    assert list(pipeline.root.iterdir()) == []


@mark.parametrize("phase", ["state_save", "update_complete", "lock_release", "cleanup"])
def test_required_actions_run_despite_rejecting_before_handler(phase_app, pipeline, phase):
    def reject(event):
        raise ValueError(f"reject {phase}")

    phase_app.on(getattr(signals, f"before_{phase}"))(reject)
    after = subscribe(phase_app, [f"after_{phase}"])
    success = subscribe(phase_app, ["after_deploy"])
    with raises(ValueError, match=f"reject {phase}"):
        pipeline.commands.run_deploy("test")
    assert len(after) == 1
    assert success == []
    assert not pipeline.home.file_exists("lock/demo/test.json")
    assert list(pipeline.root.iterdir()) == []
    updates = [
        json.loads(value)
        for key, value in pipeline.home.files.items()
        if key.startswith("update/")
    ]
    assert len(updates) == 1
    assert updates[0]["time_completed"] is not None
    assert not pipeline.home.file_exists("lock/demo/test.json")


def test_after_config_edits_apply_before_backend_and_freeze(phase_app, pipeline):
    events = []

    @phase_app.on(signals.after_config)
    async def configure(event):
        event.replace_config(replace(event.config, tags={"source": "phase"}))
        events.append(event)

    @phase_app.on(signals.after_config)
    def observe(event):
        assert event.config.tags == {"source": "phase"}

    backend = subscribe(phase_app, ["before_backend_setup"])
    pipeline.commands.run_deploy("test")
    assert backend[0].config.tags == {"source": "phase"}
    with raises(RuntimeError, match="before-operation"):
        events[0].replace_config(StelvioAppConfig())


@mark.parametrize(
    "phase",
    [
        "config",
        "backend_setup",
        "lock_acquire",
        "state_load",
        "stack_setup",
        "provision",
        "app_run",
        "resource_creation",
        "snapshot_create",
    ],
)
def test_before_phase_rejects_normal_work_and_suppresses_success(phase_app, pipeline, phase):
    original = ValueError(f"reject {phase}")

    def reject(event):
        raise original

    phase_app.on(getattr(signals, f"before_{phase}"))(reject)
    after = subscribe(phase_app, [f"after_{phase}", "after_deploy"])
    errors = subscribe(phase_app, ["on_error"])
    with raises(ValueError, match=f"reject {phase}") as raised:
        pipeline.commands.run_deploy("test")
    assert raised.value is original
    assert after == []
    assert len(errors) == 1
    assert errors[0].error is original
    assert errors[0].phase == f"before_{phase}"
    assert not pipeline.home.file_exists("lock/demo/test.json")
    assert list(pipeline.root.iterdir()) == []


def test_recovery_phase_hooks_preserve_original_failure(phase_app, pipeline, monkeypatch):
    original = ValueError("engine failed")
    monkeypatch.setattr(Stack, "up", Mock(side_effect=original))

    @phase_app.on(signals.before_state_save)
    def broken(event):
        raise RuntimeError("secondary hook failed")

    after = subscribe(phase_app, ["after_state_save", "after_update_complete", "after_cleanup"])
    errors = subscribe(phase_app, ["on_error"])
    with raises(ValueError, match="engine failed") as raised:
        pipeline.commands.run_deploy("test")
    assert raised.value is original
    assert [event.signal.name for event in after] == [
        "after_state_save",
        "after_update_complete",
        "after_cleanup",
    ]
    assert after[1].errors == ("engine failed",)
    assert len(errors) == 1
    assert errors[0].error is original
    assert errors[0].phase == "provision"


def test_infrastructure_hooks_add_and_modify_public_components(phase_app, pulumi_mocks):
    @phase_app.run
    def define():
        Bucket("files")

    @phase_app.on(signals.before_resource_creation)
    async def notify(event):
        await asyncio.sleep(0)
        bucket = next(component for component in event.components if isinstance(component, Bucket))
        bucket.notify_topic("created", events=["s3:ObjectCreated:*"], topic=Topic("events"))

    events = subscribe(phase_app, ["after_resource_creation"])
    with _command_scope("deploy", "test") as session:
        session.prepare()
        program = phase_app._get_pulumi_program_func()

        @pulumi.runtime.test
        async def execute():
            await program()

        execute()
    pulumi_mocks.assert_res_counts(
        {
            R.BUCKET: 1,
            R.TOPIC: 1,
            R.BUCKET_NOTIFICATION: 1,
            R.BUCKET_PUBLIC_ACCESS_BLOCK: 1,
            R.TOPIC_POLICY: 1,
        }
    )
    assert {component.name for component in events[0].components} == {
        "files",
        "events",
        "files-created-subscription",
    }


def test_provider_failure_after_program_keeps_provision_location(phase_app, pipeline, monkeypatch):
    original = ValueError("provider failed")

    def provision(stack, **kwargs):
        asyncio.run(stack.program())
        raise original

    monkeypatch.setattr(Stack, "up", provision)
    events = subscribe(phase_app, ["after_resource_creation", "on_error", "after_provision"])
    with raises(ValueError, match="provider failed"):
        pipeline.commands.run_deploy("test")
    assert [event.signal.name for event in events] == ["after_resource_creation", "on_error"]
    assert events[-1].phase == "provision"
    assert events[-1].handler is None


@mark.parametrize("operation", ["diff", "destroy", "refresh"])
def test_other_operations_emit_only_executed_phases(phase_app, pipeline, operation):
    events = subscribe(
        phase_app,
        ["after_provision", "after_state_save", "after_snapshot_delete", "after_lock_release"],
    )
    kwargs = {"skip_confirm": True} if operation == "destroy" else {}
    getattr(pipeline.commands, f"run_{operation}")("test", **kwargs)
    expected = ["after_provision"]
    if operation != "diff":
        expected.append("after_state_save")
    if operation == "destroy":
        expected.append("after_snapshot_delete")
    if operation != "diff":
        expected.append("after_lock_release")
    assert [event.signal.name for event in events] == expected
    assert all(event.operation == operation for event in events)


def test_inline_worker_thread_retains_async_signal_context(phase_app):
    phase_app.run(lambda: None)
    events = []

    @phase_app.on(signals.before_app_run)
    async def prepare(event):
        await asyncio.sleep(0)
        events.append(event)

    with _command_scope("dev", "test") as session:
        session.prepare()
        program = phase_app._get_pulumi_program_func()
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(lambda: asyncio.run(program())).result(timeout=5)
    assert len(events) == 1
    assert events[0].command == "dev"
    assert events[0].dev_mode is True


def test_background_checkpoint_uploads_do_not_emit_state_save(phase_app, pipeline):
    events = subscribe(phase_app, ["before_state_save", "after_state_save"])
    with _command_scope("deploy", "test") as session:
        session.prepare()
        with CommandRun("test", lock_as="deploy") as run:
            run._push_if_changed()
            assert events == []
            run.push_state()
    assert [event.signal.name for event in events] == ["before_state_save", "after_state_save"]


def test_component_edits_after_creation_preserve_existing_boundary(phase_app, pulumi_mocks):
    phase_app.run(lambda: Bucket("files"))
    rejected = []

    @phase_app.on(signals.after_resource_creation)
    def late_edit(event):
        bucket = next(component for component in event.components if isinstance(component, Bucket))
        with raises(RuntimeError, match="after resources have been created"):
            bucket.notify_topic(
                "late", events=["s3:ObjectCreated:*"], topic="arn:aws:sns:us-east-1:123:events"
            )
        rejected.append(True)

    with _command_scope("deploy", "test") as session:
        session.prepare()
        program = phase_app._get_pulumi_program_func()

        @pulumi.runtime.test
        async def execute():
            await program()

        execute()
    assert rejected == [True]
    pulumi_mocks.assert_res_counts({R.BUCKET: 1, R.BUCKET_PUBLIC_ACCESS_BLOCK: 1})


def test_cancellation_keeps_recovery_and_secondary_hook_errors(
    phase_app, pipeline, monkeypatch, capsys
):
    monkeypatch.setattr(Stack, "up", Mock(side_effect=KeyboardInterrupt))

    @phase_app.on(signals.before_state_save)
    def reject_save(event):
        raise ValueError("secondary save hook")

    events = subscribe(
        phase_app,
        [
            "after_state_save",
            "after_update_complete",
            "after_lock_release",
            "after_cleanup",
            "on_cancel",
            "on_error",
            "after_deploy",
        ],
    )
    with raises(KeyboardInterrupt):
        pipeline.commands.run_deploy("test")
    assert [event.signal.name for event in events] == [
        "after_state_save",
        "after_update_complete",
        "after_lock_release",
        "after_cleanup",
        "on_cancel",
    ]
    assert events[-1].reason == "interrupted"
    assert events[-1].phase == "provision"
    assert "secondary save hook" in capsys.readouterr().err
    assert not pipeline.home.file_exists("lock/demo/test.json")


def test_rejected_required_state_save_still_uploads_new_state(phase_app, pipeline, monkeypatch):
    revised_state = {
        "checkpoint": {
            "latest": {"resources": [{"urn": "new-resource", "type": "aws:s3/bucket:Bucket"}]}
        }
    }

    def provision(stack, **kwargs):
        run_state = next(pipeline.root.rglob("test.json"))
        run_state.write_text(json.dumps(revised_state))

    monkeypatch.setattr(Stack, "up", provision)

    @phase_app.on(signals.before_state_save)
    def reject(event):
        raise ValueError("reject save")

    with raises(ValueError, match="reject save"):
        pipeline.commands.run_deploy("test")
    assert json.loads(pipeline.home.files["state/demo/test.json"]) == revised_state


def test_dev_phase_events_identify_nested_deploy(phase_app, pipeline, monkeypatch):
    bridge = Mock()
    monkeypatch.setattr(pipeline.commands, "run_bridge_server", bridge)
    events = subscribe(
        phase_app,
        ["after_config", "after_backend_setup", "after_app_run", "after_cleanup", "after_dev"],
    )
    pipeline.commands.run_dev("test")
    assert [event.operation for event in events] == ["dev", "deploy", "deploy", "deploy", "dev"]
    assert all(event.command == "dev" and event.dev_mode for event in events)
    bridge.assert_called_once()


def test_failed_state_save_has_no_after_signal_and_finishes_other_recovery(
    phase_app, pipeline, monkeypatch
):
    original = ValueError("storage failed")
    write = pipeline.home.write_file

    def fail_state(key, path):
        if key == "state/demo/test.json":
            raise original
        write(key, path)

    monkeypatch.setattr(pipeline.home, "write_file", fail_state)
    events = subscribe(
        phase_app,
        [
            "after_state_save",
            "after_update_complete",
            "after_lock_release",
            "after_cleanup",
            "on_error",
        ],
    )
    with raises(ValueError, match="storage failed") as raised:
        pipeline.commands.run_deploy("test")
    assert raised.value is original
    assert [event.signal.name for event in events] == [
        "after_update_complete",
        "after_lock_release",
        "after_cleanup",
        "on_error",
    ]
    assert events[-1].phase == "state_save"
    assert events[0].errors == ("storage failed",)
