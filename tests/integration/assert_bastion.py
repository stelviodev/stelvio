"""Boto3 checks for bastion connectivity and DocumentDB access."""

import json
import shlex
import time
from pathlib import Path
from uuid import uuid4

from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from stelvio.network import SSH_USER

from .assert_helpers import _boto3_session
from .assert_vpc import get_security_group

# Allow the agent ten minutes to register after initial boot and IAM propagation.
_SSM_ONLINE_TIMEOUT = 600
_SSM_ONLINE_INTERVAL = 10
_PROBE_TIMEOUT = 600
_PROBE_INTERVAL = 2
_PROBE_IO = Config(connect_timeout=5, read_timeout=10, retries={"total_max_attempts": 1})


def assert_bastion_online(instance_id: str) -> None:
    """Wait for the deployed image, IAM permissions and network path to reach SSM."""
    ssm = _boto3_session().client("ssm")
    deadline = time.monotonic() + _SSM_ONLINE_TIMEOUT
    status = "not registered"
    while time.monotonic() < deadline:
        instances = ssm.describe_instance_information(
            Filters=[{"Key": "InstanceIds", "Values": [instance_id]}]
        )["InstanceInformationList"]
        status = instances[0]["PingStatus"] if instances else "not registered"
        if status == "Online":
            return
        time.sleep(_SSM_ONLINE_INTERVAL)
    raise AssertionError(
        f"Bastion '{instance_id}' did not become Online in SSM within "
        f"{_SSM_ONLINE_TIMEOUT}s; last status: {status}. Check the SSM Agent logs, "
        "instance IAM role and outbound connectivity to the SSM endpoints."
    )


def _probe_commands(probe_id: str) -> list[str]:
    source = Path(__file__).with_name("bastion_probe.py").read_text()
    delimiter = f"STLV_BASTION_PROBE_{probe_id}"
    return [
        f"python3 - {shlex.quote(probe_id)} {shlex.quote(SSH_USER)} <<'{delimiter}'\n"
        f"{source}\n{delimiter}"
    ]


def _probe_failure(instance_id: str, command_id: str, invocation: dict) -> AssertionError:
    stdout = invocation.get("StandardOutputContent", "")
    stderr = invocation.get("StandardErrorContent", "")
    stage = "unknown"
    try:
        proof = json.loads(stdout)
        if isinstance(proof, dict) and isinstance(proof.get("stage"), str):
            stage = proof["stage"][:80]
    except (TypeError, ValueError):
        pass
    details = f"stdout: {stdout}\nstderr: {stderr}"[:4000]
    return AssertionError(
        f"Bastion '{instance_id}' bootstrap command '{command_id}' failed at stage "
        f"'{stage}': status={invocation.get('Status')!r}, "
        f"response_code={invocation.get('ResponseCode')!r}. "
        f"Check cloud-init and sshd logs on the test instance.\n{details}"
    )


def _observe_probe(ssm, instance_id: str, command_id: str) -> dict | None:
    try:
        return ssm.get_command_invocation(CommandId=command_id, InstanceId=instance_id)
    except ClientError as error:
        if error.response["Error"]["Code"] == "InvocationDoesNotExist":
            return None
        raise AssertionError(
            f"Bastion '{instance_id}' bootstrap command '{command_id}' could not "
            f"be observed: {str(error)[:2000]}. Check caller Run Command permissions."
        ) from error
    except BotoCoreError as error:
        raise AssertionError(
            f"Bastion '{instance_id}' bootstrap command '{command_id}' could not "
            f"be observed: {str(error)[:2000]}. Check SSM connectivity."
        ) from error


def _wait_for_probe(ssm, instance_id: str, command_id: str) -> dict:
    deadline = time.monotonic() + _PROBE_TIMEOUT
    last_status = "not visible"
    while time.monotonic() < deadline:
        invocation = _observe_probe(ssm, instance_id, command_id)
        last_status = invocation.get("Status") if invocation is not None else "not visible"
        if invocation is None or invocation.get("Status") in {
            "Pending",
            "InProgress",
            "Delayed",
            "Cancelling",
        }:
            time.sleep(_PROBE_INTERVAL)
            continue
        if invocation.get("Status") in {"Success", "Failed", "Cancelled", "TimedOut"}:
            return invocation
        raise _probe_failure(instance_id, command_id, invocation)
    raise AssertionError(
        f"Bastion '{instance_id}' bootstrap command '{command_id}' exceeded "
        f"{_PROBE_TIMEOUT}s; last status: {last_status}. "
        "Check SSM delivery, cloud-init and sshd logs."
    )


def _assert_probe_result(instance_id: str, command_id: str, probe_id: str, invocation: dict):
    if invocation.get("Status") != "Success" or invocation.get("ResponseCode") != 0:
        raise _probe_failure(instance_id, command_id, invocation)
    try:
        proof = json.loads(invocation.get("StandardOutputContent", ""))
    except (TypeError, ValueError):
        raise _probe_failure(instance_id, command_id, invocation) from None
    if proof != {"probe_id": probe_id, "status": "ok", "stage": "complete"}:
        raise _probe_failure(instance_id, command_id, invocation)


def assert_bastion_bootstrap(instance_id: str) -> None:
    """Prove boot completion, loopback SSH and nologin-compatible forwarding in AWS."""
    ssm = _boto3_session().client("ssm", config=_PROBE_IO)
    probe_id = uuid4().hex
    try:
        result = ssm.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            Comment=f"stelvio-bastion-probe/{probe_id}",
            Parameters={"commands": _probe_commands(probe_id), "executionTimeout": ["480"]},
            TimeoutSeconds=60,
        )
    except (BotoCoreError, ClientError) as error:
        raise AssertionError(
            f"Bastion '{instance_id}' bootstrap probe submission failed: {str(error)[:2000]}. "
            "Check caller Run Command permissions and SSM connectivity. "
            "Submission was not retried; the command may still be running. "
            "Test stack teardown removes the instance."
        ) from error
    command_id = result["Command"]["CommandId"]
    try:
        invocation = _wait_for_probe(ssm, instance_id, command_id)
    except BaseException as error:
        try:
            ssm.cancel_command(CommandId=command_id, InstanceIds=[instance_id])
        except (BotoCoreError, ClientError) as cancel_error:
            error.add_note(
                f"Could not cancel bootstrap command '{command_id}': "
                f"{str(cancel_error)[:1000]}. Test stack teardown removes the instance."
            )
        raise
    _assert_probe_result(instance_id, command_id, probe_id, invocation)


def assert_document_db_ingress_from_bastion(
    security_group_id: str,
    *,
    bastion_security_group_id: str,
    app_security_group_id: str,
    port: int,
) -> None:
    """Assert DocumentDB admits only its app and bastion groups on the service port."""
    group = get_security_group(security_group_id)
    actual_sources = set()
    for permission in group["IpPermissions"]:
        assert permission["IpProtocol"] == "tcp"
        assert permission["FromPort"] == port
        assert permission["ToPort"] == port
        assert permission.get("IpRanges", []) == []
        assert permission.get("Ipv6Ranges", []) == []
        actual_sources.update(pair["GroupId"] for pair in permission.get("UserIdGroupPairs", []))
    assert actual_sources == {bastion_security_group_id, app_security_group_id}
