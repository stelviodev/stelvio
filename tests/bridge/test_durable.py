"""Drive Function's bridge seam through real subprocesses and the AWS durable SDK."""

import asyncio
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

import pulumi
from pytest import fixture, mark, param

from stelvio.aws.function import Function
from stelvio.aws.function.function import temporary_environment
from stelvio.link import Link

pytestmark = mark.usefixtures("project_cwd", "pulumi_mocks")
BASE_ARN = "arn:aws:lambda:us-east-1:123456789012:function:worker:1/durable-execution"

HANDLER = """import os
from pathlib import Path
from aws_durable_execution_sdk_python import durable_execution, durable_step
from aws_durable_execution_sdk_python.config import Duration
from stlv_resources import Resources
import helper

@durable_step
def record(ctx):
    with Path(os.environ["RECORD_FILE"]).open("a") as file:
        file.write(helper.VALUE + "\\n")
    return helper.VALUE

@durable_execution
def main(event, context):
    first = context.step(record(), name="record")
    context.wait(Duration.from_seconds(60), name="pause")
    return {"first": first, "current": helper.VALUE, "linked": Resources.settings.value}
"""


@fixture
def durable_backend(monkeypatch, tmp_path):  # noqa: C901
    """A controlled Lambda API endpoint, not an SDK substitute."""
    operations = {}
    requests = []
    errors = {}
    statuses = {}
    credentials = tmp_path / "aws-credentials"
    credentials.write_text(
        "[default]\naws_access_key_id = testing\naws_secret_access_key = testing\n"
    )
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(credentials))
    config = tmp_path / "aws-config"
    config.write_text("[default]\nregion = us-east-1\n")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(config))

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            arn = unquote(
                self.path.split("/durable-executions/", 1)[1].rsplit("/checkpoint", 1)[0]
            )
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            error = errors.get(arn)
            if error:
                self.send_response(error[0])
                self.send_header("Content-Type", "application/json")
                self.send_header("x-amzn-ErrorType", error[1])
                self.end_headers()
                self.wfile.write(json.dumps({"Message": "checkpoint unavailable"}).encode())
                return
            stored = operations.setdefault(arn, {})
            for update in body["Updates"]:
                operation = stored.setdefault(
                    update["Id"],
                    {
                        "Id": update["Id"],
                        "Type": update["Type"],
                    },
                )
                operation["Status"] = {
                    "START": "STARTED",
                    "SUCCEED": "SUCCEEDED",
                    "FAIL": "FAILED",
                }.get(update["Action"], "PENDING")
                if "Name" in update:
                    operation["Name"] = update["Name"]
                for key in ("ParentId", "SubType"):
                    if key in update:
                        operation[key] = update[key]
                if update["Type"] == "STEP" and "Payload" in update:
                    operation["StepDetails"] = {"Result": update["Payload"], "Attempt": 1}
                if update["Type"] == "WAIT":
                    operation["WaitDetails"] = {
                        "ScheduledEndTimestamp": int(time.time() * 1000) + 60000
                    }
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            updated = [dict(stored[update["Id"]]) for update in body["Updates"]]
            for operation in updated:
                if "WaitDetails" in operation:
                    operation["WaitDetails"] = {"ScheduledEndTimestamp": time.time() + 60}
            self.wfile.write(
                json.dumps(
                    {"CheckpointToken": "next-token", "NewExecutionState": {"Operations": updated}}
                ).encode()
            )

        def do_GET(self):
            arn = unquote(self.path.split("/durable-executions/", 1)[1].split("?", 1)[0])
            if arn.endswith("/state"):
                stored = json.loads(
                    json.dumps(list(operations[arn.removesuffix("/state")].values()))
                )
                for operation in stored:
                    if "WaitDetails" in operation:
                        operation["WaitDetails"]["ScheduledEndTimestamp"] /= 1000
                response = {"Operations": stored, "CheckpointToken": "next-token"}
            else:
                response = {"Status": statuses.get(arn, "RUNNING")}
            denied = errors.get(arn)
            self.send_response(denied[0] if denied else 200)
            self.send_header("Content-Type", "application/json")
            if denied:
                self.send_header("x-amzn-ErrorType", denied[1])
                response = {"Message": "status unavailable"}
            self.end_headers()
            self.wfile.write(json.dumps(response).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("AWS_ENDPOINT_URL_LAMBDA", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    try:
        yield operations, requests, errors, statuses
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def envelope(function, arn, *, operations=(), deadline_seconds=30):
    return {
        "event": json.dumps(
            {
                "endpointId": function._dev_endpoint_id,
                "event": {
                    "DurableExecutionArn": arn,
                    "CheckpointToken": "token",
                    "UpdatedOperationIds": ["execution"],
                    "InitialExecutionState": {
                        "Operations": [
                            {
                                "Id": "execution",
                                "Type": "EXECUTION",
                                "Status": "STARTED",
                                "ExecutionDetails": {"InputPayload": "{}"},
                            },
                            *operations,
                        ]
                    },
                },
                "context": {
                    "invoke_id": "req-1",
                    "client_context": None,
                    "cognito_identity": None,
                    "invoked_function_arn": "arn:aws:lambda:us-east-1:123456789012:"
                    "function:worker:1",
                    "epoch_deadline_time_in_ms": int((time.time() + deadline_seconds) * 1000),
                },
            }
        )
    }


def write_handler(project, *, value="v1", source=HANDLER):
    folder = project / "durable"
    folder.mkdir(exist_ok=True)
    (folder / "handler.py").write_text(source)
    (folder / "helper.py").write_text(f"VALUE = {value!r}\n")


def make_function(project, *, source=HANDLER):
    write_handler(project, source=source)
    return Function(
        "worker",
        handler="durable::handler.main",
        runtime="python3.14",
        durable={"execution_timeout": 900},
        environment={"RECORD_FILE": str(project / "records.txt")},
        links=[Link("settings", properties={"value": "pinned"}, permissions=[])],
    )


@pulumi.runtime.test
def test_worker_environment_excludes_concurrent_ordinary_handler(project_cwd, durable_backend):
    function = make_function(project_cwd)
    (project_cwd / "durable/helper.py").write_text(
        'import os\nVALUE = os.environ.get("ORDINARY_ONLY", "isolated")\n'
    )
    request = envelope(function, BASE_ARN + "/isolation/id")
    request["_local_environment"] = os.environ.copy()

    async def check():
        with temporary_environment({"ORDINARY_ONLY": "leaked"}, []):
            result = await function.handle_bridge_event(request)
        assert result.error_result is None
        assert result.success_result == {"Status": "PENDING"}
        assert (project_cwd / "records.txt").read_text() == "isolated\n"

    return check()


@pulumi.runtime.test
def test_real_sdk_suspends_and_replays_pinned_source(project_cwd, durable_backend):
    function = make_function(project_cwd)
    operations, requests, _, _ = durable_backend
    arn = BASE_ARN + "/first/id"

    async def check():
        first = await function.handle_bridge_event(envelope(function, arn))
        assert first.error_result is None
        assert first.success_result == {"Status": "PENDING"}
        assert (project_cwd / "records.txt").read_text().splitlines() == ["v1"]
        write_handler(project_cwd, value="v2")
        stored = list(operations[arn].values())
        for operation in stored:
            if operation["Type"] == "WAIT":
                operation["Status"] = "SUCCEEDED"
        replay = await function.handle_bridge_event(envelope(function, arn, operations=stored))
        assert replay.error_result is None
        assert replay.success_result["Status"] == "SUCCEEDED"
        assert json.loads(replay.success_result["Result"]) == {
            "first": "v1",
            "current": "v1",
            "linked": "pinned",
        }
        assert (project_cwd / "records.txt").read_text().splitlines() == ["v1"]
        assert requests

    return check()


@pulumi.runtime.test
def test_concurrent_executions_keep_separate_source(project_cwd, durable_backend):
    function = make_function(project_cwd)
    operations, _, _, _ = durable_backend
    old = BASE_ARN + "/old/id"
    new = BASE_ARN + "/new/id"

    async def check():
        assert (
            await function.handle_bridge_event(envelope(function, old))
        ).durable_status == "PENDING"
        write_handler(project_cwd, value="v2")
        assert (
            await function.handle_bridge_event(envelope(function, new))
        ).durable_status == "PENDING"
        for stored in operations.values():
            for operation in stored.values():
                if operation["Type"] == "WAIT":
                    operation["Status"] = "SUCCEEDED"
        results = await asyncio.gather(
            *[
                function.handle_bridge_event(
                    envelope(function, arn, operations=list(operations[arn].values()))
                )
                for arn in (old, new)
            ]
        )
        assert [json.loads(result.success_result["Result"])["current"] for result in results] == [
            "v1",
            "v2",
        ]
        assert (project_cwd / "records.txt").read_text().splitlines() == ["v1", "v2"]

    return check()


@pulumi.runtime.test
def test_missing_replay_snapshot_fails_without_running_current_code(project_cwd):
    function = make_function(project_cwd)

    async def check():
        result = await function.handle_bridge_event(
            envelope(
                function,
                BASE_ARN + "/lost/id",
                operations=[
                    {"Id": "old-step", "Type": "STEP", "Status": "SUCCEEDED"},
                ],
            )
        )
        assert result.success_result is None
        assert str(result.error_result).startswith("Source snapshot missing for durable replay")
        assert not (project_cwd / "records.txt").exists()

    return check()


@pulumi.runtime.test
def test_durable_worker_deadline(project_cwd, durable_backend):
    function = make_function(project_cwd)

    async def check():
        result = await function.handle_bridge_event(
            envelope(function, BASE_ARN + "/late/id", deadline_seconds=0)
        )
        assert isinstance(result.error_result, TimeoutError)
        assert result.success_result is None

    return check()


@pulumi.runtime.test
def test_real_sdk_reports_checkpoint_authorization_failure(project_cwd, durable_backend):
    function = make_function(project_cwd)
    _, _, errors, _ = durable_backend
    arn = BASE_ARN + "/denied/id"
    errors[arn] = (403, "AccessDeniedException")

    async def check():
        result = await function.handle_bridge_event(envelope(function, arn))
        assert result.error_result is None
        assert result.durable_status == "FAILED"
        assert result.success_result["Error"]["ErrorType"] == (
            "aws_durable_execution_sdk_python.exceptions.CheckpointError"
        )

    return check()


@pulumi.runtime.test
def test_real_sdk_retryable_checkpoint_error_is_an_invocation_error(
    project_cwd, durable_backend, monkeypatch
):
    function = make_function(project_cwd)
    _, _, errors, _ = durable_backend
    arn = BASE_ARN + "/retry/id"
    errors[arn] = (500, "ServiceException")
    monkeypatch.setenv("AWS_MAX_ATTEMPTS", "1")

    async def check():
        result = await function.handle_bridge_event(envelope(function, arn))
        assert result.success_result is None
        assert str(result.error_result).startswith(
            "CheckpointError: An error occurred (ServiceException)"
        )

    return check()


@pulumi.runtime.test
def test_running_worker_is_killed_at_deadline(project_cwd, durable_backend):
    function = make_function(
        project_cwd,
        source="""import os
import time
from pathlib import Path
def main(event, context):
    marker = Path(os.environ["RECORD_FILE"])
    marker.with_suffix(".started").touch()
    time.sleep(3)
    marker.touch()
    return {"Status": "SUCCEEDED"}
""",
    )

    async def check():
        result = await function.handle_bridge_event(
            envelope(function, BASE_ARN + "/slow/id", deadline_seconds=5)
        )
        assert isinstance(result.error_result, TimeoutError)
        assert (project_cwd / "records.started").exists()
        await asyncio.sleep(3.5)
        assert not (project_cwd / "records.txt").exists()

    return check()


@pulumi.runtime.test
def test_missing_local_credentials_report_required_permissions(
    project_cwd, durable_backend, monkeypatch, tmp_path
):
    function = make_function(project_cwd)
    credentials = tmp_path / "empty-credentials"
    credentials.touch()
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(credentials))
    monkeypatch.delenv("AWS_ACCESS_KEY_ID")
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY")

    async def check():
        result = await function.handle_bridge_event(
            envelope(function, BASE_ARN + "/credentials/id")
        )
        assert result.success_result is None
        assert str(result.error_result).splitlines()[0] == (
            "CheckpointError: Unable to locate credentials"
            " Local durable execution uses your configured AWS credentials. "
            "Refresh them and grant lambda:CheckpointDurableExecution and "
            "lambda:GetDurableExecutionState on this function's qualified execution ARNs."
        )

    return check()


@mark.parametrize(
    ("status", "retained"),
    [
        param("RUNNING", True, id="pending"),
        param("SUCCEEDED", False, id="confirmed-terminal"),
        param("DENIED", True, id="no-cleanup-permission"),
    ],
)
@pulumi.runtime.test
def test_snapshots_removed_only_after_service_confirmation(
    project_cwd, durable_backend, status, retained
):
    function = make_function(
        project_cwd,
        source="""from aws_durable_execution_sdk_python import durable_execution
@durable_execution
def main(event, context):
    return "done"
""",
    )
    _, _, errors, statuses = durable_backend
    arn = BASE_ARN + "/finished/id"

    async def check():
        result = await function.handle_bridge_event(envelope(function, arn))
        assert result.durable_status == "SUCCEEDED"
        root = project_cwd / ".stelvio/durable-dev"
        manifests = list(root.glob("*/manifest.json"))
        assert len(manifests) == 1
        if status == "DENIED":
            errors[arn] = (403, "AccessDeniedException")
        else:
            statuses[arn] = status
        next_result = await function.handle_bridge_event(envelope(function, BASE_ARN + "/next/id"))
        assert next_result.durable_status == "SUCCEEDED"
        from stelvio.bridge.local.durable import _cleanup_snapshots

        _cleanup_snapshots(next_result.durable_cleanup, threading.Event())
        assert manifests[0].exists() is retained

    return check()


@pulumi.runtime.test
def test_linked_assets_and_paginated_replay_use_original_snapshot(project_cwd, durable_backend):
    asset = project_cwd / "asset.txt"
    asset.write_text("original")
    source = HANDLER.replace(
        '"linked": Resources.settings.value',
        '"linked": Path(Resources.settings.value).read_text()',
    )
    write_handler(project_cwd, source=source)
    function = Function(
        "worker",
        handler="durable::handler.main",
        runtime="python3.14",
        durable={"execution_timeout": 900},
        environment={"RECORD_FILE": str(project_cwd / "records.txt")},
        links=[
            Link(
                "settings",
                properties={"value": str(asset)},
                permissions=[],
                _files={"data/asset.txt": asset},
            )
        ],
    )
    operations, _, _, _ = durable_backend
    arn = BASE_ARN + "/assets/id"

    async def check():
        assert (
            await function.handle_bridge_event(envelope(function, arn))
        ).durable_status == "PENDING"
        asset.write_text("edited")
        for operation in operations[arn].values():
            if operation["Type"] == "WAIT":
                operation["Status"] = "SUCCEEDED"
        request = envelope(function, arn)
        message = json.loads(request["event"])
        message["event"]["InitialExecutionState"]["NextMarker"] = "page-two"
        message["event"]["UpdatedOperationIds"] = list(operations[arn])
        request["event"] = json.dumps(message)
        result = await function.handle_bridge_event(request)
        assert result.durable_status == "SUCCEEDED"
        assert json.loads(result.success_result["Result"])["linked"] == "original"

    return check()


def test_worker_reports_missing_local_sdk(monkeypatch, tmp_path):
    """The worker entrypoint can report this even when the handler cannot be imported."""
    import builtins
    import io

    from stelvio.bridge.local.durable_worker import main

    original_import = builtins.__import__

    def without_sdk(name, *args, **kwargs):
        if name == "aws_durable_execution_sdk_python":
            raise ModuleNotFoundError(name=name)
        return original_import(name, *args, **kwargs)

    output = tmp_path / "result.json"
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"result_path": str(output)})))
    monkeypatch.setattr(builtins, "__import__", without_sdk)
    main()
    response = json.loads(output.read_text())
    assert response["success"] is False
    assert response["errorType"] == "RuntimeError"
    assert response["error"] == (
        "Local durable execution requires aws-durable-execution-sdk-python. "
        "Install it in the Python environment running stlv dev and declare it "
        "in the function's requirements."
    )


@pulumi.runtime.test
def test_replay_preserves_changed_removed_and_absent_variables(project_cwd, durable_backend):
    source = HANDLER.replace(
        '"linked": Resources.settings.value',
        '"changed": os.getenv("CHANGED"), "removed": os.getenv("REMOVED"), '
        '"added": os.getenv("ADDED", "absent")',
    )
    function = make_function(project_cwd, source=source)
    function.config.environment.update(CHANGED="original", REMOVED="original")
    operations, _, _, _ = durable_backend
    arn = BASE_ARN + "/environment/id"

    async def check():
        assert (
            await function.handle_bridge_event(envelope(function, arn))
        ).durable_status == "PENDING"
        function.config.environment.update(CHANGED="edited", ADDED="new")
        del function.config.environment["REMOVED"]
        for operation in operations[arn].values():
            if operation["Type"] == "WAIT":
                operation["Status"] = "SUCCEEDED"
        result = await function.handle_bridge_event(
            envelope(function, arn, operations=list(operations[arn].values()))
        )
        value = json.loads(result.success_result["Result"])
        assert (value["changed"], value["removed"], value["added"]) == (
            "original",
            "original",
            "absent",
        )
        new_arn = BASE_ARN + "/environment-new/id"
        await function.handle_bridge_event(envelope(function, new_arn))
        for operation in operations[new_arn].values():
            if operation["Type"] == "WAIT":
                operation["Status"] = "SUCCEEDED"
        result = await function.handle_bridge_event(
            envelope(function, new_arn, operations=list(operations[new_arn].values()))
        )
        value = json.loads(result.success_result["Result"])
        assert (value["changed"], value["removed"], value["added"]) == ("edited", None, "new")

    return check()


def test_cleanup_isolated_coalesced_and_stopped(monkeypatch, tmp_path):
    from stelvio.bridge.local import durable
    from stelvio.bridge.local.dtos import DurableCleanupRequest

    entered = threading.Event()
    release = threading.Event()
    calls = []

    def scan(request, stop):
        calls.append(request)
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr(durable, "_cleanup_snapshots", scan)

    async def check():
        manager = durable.DurableCleanupManager()
        request = DurableCleanupRequest(tmp_path, None, "us-east-1", BASE_ARN)
        try:
            manager.submit(request)
            while not entered.is_set():
                await asyncio.sleep(0.001)
            for _ in range(32):
                manager.submit(request)
            assert len(manager._pending) == 1
            assert (
                await asyncio.wait_for(
                    asyncio.get_running_loop().run_in_executor(None, lambda: "ordinary"), 1
                )
                == "ordinary"
            )
            closing = asyncio.create_task(manager.close())
            await asyncio.sleep(0)
            release.set()
            await closing
            assert calls == [request]
        finally:
            release.set()
            await manager.close()

    asyncio.run(check())


def test_cleanup_filters_region_and_continues_after_errors(monkeypatch, tmp_path):
    from unittest.mock import Mock

    from stelvio.bridge.local.dtos import DurableCleanupRequest
    from stelvio.bridge.local.durable import _cleanup_snapshots

    arns = {
        "current": BASE_ARN + "/current/id",
        "done": BASE_ARN + "/done/id",
        "running": BASE_ARN + "/running/id",
        "denied": BASE_ARN + "/denied/id",
        "other": BASE_ARN.replace("us-east-1", "eu-west-1") + "/other/id",
    }
    for name, arn in arns.items():
        folder = tmp_path / name
        folder.mkdir()
        (folder / "manifest.json").write_text(json.dumps({"execution_arn": arn}))
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "manifest.json").write_text("invalid")
    client = Mock()

    def status(*, DurableExecutionArn):  # noqa: N803
        if DurableExecutionArn == arns["denied"]:
            raise PermissionError
        return {"Status": "SUCCEEDED" if DurableExecutionArn == arns["done"] else "RUNNING"}

    client.get_durable_execution.side_effect = status
    session = Mock()
    session.return_value.client.return_value = client
    monkeypatch.setattr("stelvio.bridge.local.durable.boto3.Session", session)
    _cleanup_snapshots(
        DurableCleanupRequest(tmp_path, "selected", "us-east-1", arns["current"]),
        threading.Event(),
    )
    session.assert_called_once_with(profile_name="selected", region_name="us-east-1")
    assert not (tmp_path / "done").exists()
    assert all(
        (tmp_path / name).exists() for name in ("current", "running", "denied", "other", "bad")
    )
    assert client.get_durable_execution.call_count == 3
    client.close.assert_called_once()


def test_listener_publishes_before_cleanup(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    from stelvio.bridge.local import listener
    from stelvio.bridge.local.dtos import BridgeInvocationResult, DurableCleanupRequest

    request = DurableCleanupRequest(tmp_path, None, "us-east-1", BASE_ARN)
    result = BridgeInvocationResult(
        {"Status": "SUCCEEDED"},
        None,
        BASE_ARN,
        "DURABLE",
        0,
        -1,
        durable_cleanup=request,
    )
    handler = SimpleNamespace(
        _dev_endpoint_id="worker", handle_bridge_event=AsyncMock(return_value=result)
    )
    manager = Mock()

    async def publish(*args):
        manager.submit.assert_not_called()

    monkeypatch.setattr(listener.WebsocketHandlers, "all", lambda: [handler])
    monkeypatch.setattr(listener, "publish", publish)
    monkeypatch.setattr(listener, "log_invocation", lambda result: None)
    asyncio.run(
        listener._handle_data_message(
            {"event": json.dumps({"endpointId": "worker"})},
            None,
            "key",
            "app",
            "dev",
            cleanup=manager,
        )
    )
    manager.submit.assert_called_once_with(request)
