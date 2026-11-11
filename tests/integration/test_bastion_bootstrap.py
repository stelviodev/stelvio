"""Exercise the live bootstrap probe's SSM boundary without AWS or real delays."""

import json
import os
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import ANY, Mock, call

from botocore.exceptions import ClientError, ReadTimeoutError
from pytest import fixture, mark, param, raises

from stelvio.network import SSH_USER

from . import assert_bastion

INSTANCE_ID = "i-0123456789abcdef0"
COMMAND_ID = "11111111-2222-4333-8444-555555555555"
PROBE_ID = "e" * 32
PROOF = {"probe_id": PROBE_ID, "status": "ok", "stage": "complete"}


def _invocation(*, status="Success", code=0, output=None, error=""):
    return {
        "CommandId": COMMAND_ID,
        "InstanceId": INSTANCE_ID,
        "Status": status,
        "StatusDetails": status,
        "ResponseCode": code,
        "StandardOutputContent": json.dumps(PROOF) if output is None else output,
        "StandardErrorContent": error,
    }


def _aws_error(code, operation):
    return ClientError({"Error": {"Code": code, "Message": "probe test error"}}, operation)


@mark.skipif(os.name != "posix", reason="Run Command probe packaging uses a POSIX shell")
def test_probe_packaging_passes_identity_and_ssh_user_to_standalone_program(monkeypatch, tmp_path):
    source = "import json, sys\nprint(json.dumps(sys.argv[1:]))\n"
    monkeypatch.setattr(assert_bastion.Path, "read_text", lambda self: source)
    commands = assert_bastion._probe_commands(PROBE_ID)
    (tmp_path / "python3").symlink_to(sys.executable)

    result = subprocess.run(  # noqa: S603  # fixed shell and test-owned probe source
        ["/bin/sh", "-c", commands[0]],
        cwd=tmp_path,
        env=os.environ | {"PATH": f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}"},
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )

    assert json.loads(result.stdout) == [PROBE_ID, SSH_USER]


@fixture
def bootstrap_probe(monkeypatch):
    clock = SimpleNamespace(elapsed=0, delays=[])

    def sleep(seconds):
        clock.elapsed += seconds
        clock.delays.append(seconds)

    monkeypatch.setattr(
        assert_bastion,
        "time",
        SimpleNamespace(monotonic=lambda: clock.elapsed, sleep=sleep),
    )
    monkeypatch.setattr(assert_bastion, "uuid4", lambda: SimpleNamespace(hex=PROBE_ID))
    monkeypatch.setattr(assert_bastion, "_probe_commands", lambda probe_id: ["python3 -c 'pass'"])
    client = Mock(spec=["send_command", "get_command_invocation", "cancel_command"])
    client.send_command.return_value = {"Command": {"CommandId": COMMAND_ID}}
    client.get_command_invocation.return_value = _invocation()
    session = SimpleNamespace(client=Mock(return_value=client))
    monkeypatch.setattr(assert_bastion, "_boto3_session", lambda: session)
    return SimpleNamespace(client=client, session=session, clock=clock)


def test_bootstrap_probe_targets_one_instance_with_bounded_ssm_calls(bootstrap_probe):
    assert_bastion.assert_bastion_bootstrap(INSTANCE_ID)

    bootstrap_probe.session.client.assert_called_once_with("ssm", config=ANY)
    config = bootstrap_probe.session.client.call_args.kwargs["config"]
    assert config.connect_timeout == 5
    assert config.read_timeout == 10
    assert config.retries == {"total_max_attempts": 1}
    bootstrap_probe.client.send_command.assert_called_once_with(
        InstanceIds=[INSTANCE_ID],
        DocumentName="AWS-RunShellScript",
        Comment=f"stelvio-bastion-probe/{PROBE_ID}",
        Parameters={"commands": [ANY], "executionTimeout": ["480"]},
        TimeoutSeconds=60,
    )
    script = bootstrap_probe.client.send_command.call_args.kwargs["Parameters"]["commands"][0]
    assert isinstance(script, str)
    assert script
    bootstrap_probe.client.get_command_invocation.assert_called_once_with(
        CommandId=COMMAND_ID, InstanceId=INSTANCE_ID
    )
    bootstrap_probe.client.cancel_command.assert_not_called()


def test_bootstrap_probe_waits_for_visibility_and_in_progress_states(bootstrap_probe):
    bootstrap_probe.client.get_command_invocation.side_effect = [
        _aws_error("InvocationDoesNotExist", "GetCommandInvocation"),
        *(
            _invocation(status=status)
            for status in ("Pending", "InProgress", "Delayed", "Cancelling")
        ),
        _invocation(),
    ]

    assert_bastion.assert_bastion_bootstrap(INSTANCE_ID)

    assert (
        bootstrap_probe.client.get_command_invocation.call_args_list
        == [call(CommandId=COMMAND_ID, InstanceId=INSTANCE_ID)] * 6
    )
    assert bootstrap_probe.clock.delays == [2] * 5
    bootstrap_probe.client.cancel_command.assert_not_called()


@mark.parametrize("status", ["Failed", "Cancelled", "TimedOut"])
def test_bootstrap_command_failure_names_instance_command_and_stage(bootstrap_probe, status):
    bootstrap_probe.client.get_command_invocation.return_value = _invocation(
        status=status,
        code=1,
        output=json.dumps({"probe_id": PROBE_ID, "status": "failed", "stage": "ssh-forwarding"}),
        error="forwarding test failed",
    )

    with raises(AssertionError) as failure:
        assert_bastion.assert_bastion_bootstrap(INSTANCE_ID)

    message = str(failure.value)
    assert INSTANCE_ID in message
    assert COMMAND_ID in message
    assert status in message
    assert "ssh-forwarding" in message


def test_success_status_with_nonzero_exit_code_is_rejected(bootstrap_probe):
    bootstrap_probe.client.get_command_invocation.return_value = _invocation(code=7)

    with raises(AssertionError) as failure:
        assert_bastion.assert_bastion_bootstrap(INSTANCE_ID)

    message = str(failure.value)
    assert INSTANCE_ID in message
    assert COMMAND_ID in message
    assert "7" in message


@mark.parametrize(
    "output",
    [
        param("not JSON", id="malformed-json"),
        param("[]", id="not-an-object"),
        param(json.dumps(PROOF | {"probe_id": "another-probe"}), id="wrong-probe"),
        param(json.dumps(PROOF | {"status": "failed"}), id="wrong-status"),
        param(json.dumps(PROOF | {"stage": "ssh-forwarding"}), id="incomplete-stage"),
        param(json.dumps({"probe_id": PROBE_ID, "status": "ok"}), id="missing-stage"),
        param(json.dumps(PROOF | {"extra": "unexpected"}), id="extra-field"),
    ],
)
def test_bootstrap_probe_requires_exact_current_success_proof(bootstrap_probe, output):
    bootstrap_probe.client.get_command_invocation.return_value = _invocation(output=output)

    with raises(AssertionError) as failure:
        assert_bastion.assert_bastion_bootstrap(INSTANCE_ID)

    assert INSTANCE_ID in str(failure.value)
    assert COMMAND_ID in str(failure.value)


@mark.parametrize("output", ["x" * 100_000, json.dumps({"stage": "x" * 100_000})])
def test_bootstrap_probe_bounds_failure_diagnostics(bootstrap_probe, output):
    bootstrap_probe.client.get_command_invocation.return_value = _invocation(
        status="Failed", code=1, output=output, error="y" * 100_000
    )

    with raises(AssertionError) as failure:
        assert_bastion.assert_bastion_bootstrap(INSTANCE_ID)

    message = str(failure.value)
    assert INSTANCE_ID in message
    assert COMMAND_ID in message
    assert len(message) <= 8192


def test_bootstrap_submission_permission_failure_has_no_retry_or_unknown_cancellation(
    bootstrap_probe,
):
    bootstrap_probe.client.send_command.side_effect = _aws_error(
        "AccessDeniedException", "SendCommand"
    )

    with raises(AssertionError) as failure:
        assert_bastion.assert_bastion_bootstrap(INSTANCE_ID)

    assert INSTANCE_ID in str(failure.value)
    assert "AccessDeniedException" in str(failure.value)
    bootstrap_probe.client.send_command.assert_called_once()
    bootstrap_probe.client.get_command_invocation.assert_not_called()
    bootstrap_probe.client.cancel_command.assert_not_called()


def test_bootstrap_query_permission_failure_cancels_only_the_owned_command(bootstrap_probe):
    bootstrap_probe.client.get_command_invocation.side_effect = _aws_error(
        "AccessDeniedException", "GetCommandInvocation"
    )

    with raises(AssertionError) as failure:
        assert_bastion.assert_bastion_bootstrap(INSTANCE_ID)

    message = str(failure.value)
    assert INSTANCE_ID in message
    assert COMMAND_ID in message
    assert "AccessDeniedException" in message
    bootstrap_probe.client.cancel_command.assert_called_once_with(
        CommandId=COMMAND_ID, InstanceIds=[INSTANCE_ID]
    )


def test_bootstrap_timeout_cancels_only_the_owned_command(bootstrap_probe):
    bootstrap_probe.client.get_command_invocation.return_value = _invocation(status="Pending")

    with raises(AssertionError) as failure:
        assert_bastion.assert_bastion_bootstrap(INSTANCE_ID)

    message = str(failure.value)
    assert INSTANCE_ID in message
    assert COMMAND_ID in message
    assert "Pending" in message
    assert 600 <= bootstrap_probe.clock.elapsed <= 602
    bootstrap_probe.client.cancel_command.assert_called_once_with(
        CommandId=COMMAND_ID, InstanceIds=[INSTANCE_ID]
    )


def test_bootstrap_query_error_survives_best_effort_cancellation_failure(bootstrap_probe):
    bootstrap_probe.client.get_command_invocation.side_effect = ReadTimeoutError(
        endpoint_url="https://ssm.example.invalid/query"
    )
    bootstrap_probe.client.cancel_command.side_effect = _aws_error(
        "AccessDeniedException", "CancelCommand"
    )

    with raises(AssertionError) as failure:
        assert_bastion.assert_bastion_bootstrap(INSTANCE_ID)

    message = str(failure.value)
    assert INSTANCE_ID in message
    assert COMMAND_ID in message
    assert "ssm.example.invalid/query" in message
    bootstrap_probe.client.cancel_command.assert_called_once_with(
        CommandId=COMMAND_ID, InstanceIds=[INSTANCE_ID]
    )


def test_ambiguous_bootstrap_submission_timeout_is_never_retried(bootstrap_probe):
    bootstrap_probe.client.send_command.side_effect = ReadTimeoutError(
        endpoint_url="https://ssm.example.invalid"
    )

    with raises(AssertionError) as failure:
        assert_bastion.assert_bastion_bootstrap(INSTANCE_ID)

    assert INSTANCE_ID in str(failure.value)
    bootstrap_probe.client.send_command.assert_called_once()
    bootstrap_probe.client.get_command_invocation.assert_not_called()
    bootstrap_probe.client.cancel_command.assert_not_called()
