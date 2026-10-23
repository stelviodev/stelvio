"""Durable executions resume in AWS and survive alias changes and local restarts."""

import json
import time

from pytest import mark

from stelvio.aws.dynamo_db import DynamoTable
from stelvio.aws.function import Function

from .assert_helpers import _boto3_session
from .export_helpers import export_dynamo_table, export_function
from .test_dev_mode import _BridgeRunner

pytestmark = mark.integration
_POLL_INTERVAL = 1
_EXECUTION_WAIT = 180
_BRIDGE_SUBSCRIBE_WAIT = 8


def _infra(revision):
    results = DynamoTable("results", fields={"pk": "S"}, partition_key="pk")
    fn = Function(
        "worker",
        handler="handlers/durable.main",
        runtime="python3.14",
        durable={"execution_timeout": 300, "retention_period": 1},
        requirements=["aws-durable-execution-sdk-python==2.0.1"],
        environment={"REVISION": revision},
        links=[results],
    )
    export_function(fn)
    export_dynamo_table(results)


def _start(target, *, key, wait):
    response = (
        _boto3_session()
        .client("lambda")
        .invoke(
            FunctionName=target,
            InvocationType="Event",
            DurableExecutionName=key,
            Payload=json.dumps({"key": key, "wait": wait}).encode(),
        )
    )
    assert response["StatusCode"] == 202
    return response["DurableExecutionArn"]


def _wait(arn, *, status="SUCCEEDED"):
    client = _boto3_session().client("lambda")
    deadline = time.monotonic() + _EXECUTION_WAIT
    response = None
    while time.monotonic() < deadline:
        response = client.get_durable_execution(DurableExecutionArn=arn)
        if response["Status"] == status:
            return response
        if response["Status"] in ("FAILED", "STOPPED", "TIMED_OUT"):
            break
        time.sleep(_POLL_INTERVAL)
    raise AssertionError(f"Durable execution did not reach {status}: {response}")


def _assert_once(table_name, key):
    item = (
        _boto3_session()
        .resource("dynamodb")
        .Table(table_name)
        .get_item(
            Key={"pk": key},
            ConsistentRead=True,
        )["Item"]
    )
    assert item == {"pk": key, "runs": 1}


def _wait_for_suspension(arn):
    client = _boto3_session().client("lambda")
    deadline = time.monotonic() + _EXECUTION_WAIT
    while time.monotonic() < deadline:
        events = []
        marker = None
        while True:
            page = client.get_durable_execution_history(
                DurableExecutionArn=arn, **({"Marker": marker} if marker else {})
            )
            events.extend(page["Events"])
            marker = page.get("NextMarker")
            if not marker:
                break
        types = {event["EventType"] for event in events}
        execution = client.get_durable_execution(DurableExecutionArn=arn)
        assert execution["Status"] == "RUNNING", execution
        if {"StepSucceeded", "WaitStarted", "InvocationCompleted"} <= types:
            assert "WaitSucceeded" not in types
            return
        time.sleep(_POLL_INTERVAL)
    raise AssertionError(f"Durable execution never suspended: {arn}")


def test_durable_step_wait_resume(stelvio_env, project_dir):
    outputs = stelvio_env.deploy(lambda: _infra("v1"))
    arn = _start(outputs["function_worker_invocation_arn"], key="resume", wait=2)
    execution = _wait(arn)
    assert json.loads(execution["Result"]) == {"revision": "v1", "current_revision": "v1"}
    _assert_once(outputs["dynamotable_results_name"], "resume")


def test_durable_pending_execution_survives_alias_update(stelvio_env, project_dir):
    outputs = stelvio_env.deploy(lambda: _infra("v1"))
    old = _start(outputs["function_worker_invocation_arn"], key="old", wait=90)
    _wait_for_suspension(old)
    outputs = stelvio_env.deploy(lambda: _infra("v2"))
    assert (
        _boto3_session().client("lambda").get_durable_execution(DurableExecutionArn=old)["Status"]
        == "RUNNING"
    )
    new = _start(outputs["function_worker_invocation_arn"], key="new", wait=1)
    assert json.loads(_wait(new)["Result"]) == {"revision": "v2", "current_revision": "v2"}
    assert json.loads(_wait(old)["Result"]) == {"revision": "v1", "current_revision": "v1"}
    _assert_once(outputs["dynamotable_results_name"], "old")
    _assert_once(outputs["dynamotable_results_name"], "new")


def test_durable_dev_replays_pinned_source_after_restart(stelvio_env, project_dir):
    outputs = stelvio_env.deploy(lambda: _infra("v1"), dev_mode=True)
    bridge = _BridgeRunner(
        region=stelvio_env.aws_region,
        profile=stelvio_env.aws_profile,
        app_name=f"stlv-{stelvio_env.run_id}",
        env="test",
    )
    bridge.start()
    try:
        time.sleep(_BRIDGE_SUBSCRIBE_WAIT)
        arn = _start(outputs["function_worker_invocation_arn"], key="local", wait=30)
        _wait_for_suspension(arn)
        bridge.stop()
        handler = project_dir / "handlers/durable.py"
        handler.write_text(handler.read_text().replace('os.environ["REVISION"]', '"edited"'))
        bridge.start()
        execution = _wait(arn)
        assert json.loads(execution["Result"]) == {"revision": "v1", "current_revision": "v1"}
        _assert_once(outputs["dynamotable_results_name"], "local")
    finally:
        bridge.stop()
