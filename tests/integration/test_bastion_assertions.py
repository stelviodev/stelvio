"""Exercise the live SSM wait without AWS calls or real delays."""

from types import SimpleNamespace
from unittest.mock import Mock, call

from pytest import fixture, mark, raises

from . import assert_bastion


@fixture
def ssm_wait(monkeypatch):
    elapsed = 0

    def sleep(seconds):
        nonlocal elapsed
        elapsed += seconds

    monkeypatch.setattr(
        assert_bastion, "time", SimpleNamespace(monotonic=lambda: elapsed, sleep=sleep)
    )
    client = Mock()
    monkeypatch.setattr(
        assert_bastion, "_boto3_session", lambda: SimpleNamespace(client=lambda service: client)
    )
    return client


def test_bastion_waits_for_ssm_online(ssm_wait):
    ssm_wait.describe_instance_information.side_effect = [
        {"InstanceInformationList": []},
        {"InstanceInformationList": [{"PingStatus": "ConnectionLost"}]},
        {"InstanceInformationList": [{"PingStatus": "Online"}]},
    ]

    assert_bastion.assert_bastion_online("i-bastion")

    assert (
        ssm_wait.describe_instance_information.call_args_list
        == [call(Filters=[{"Key": "InstanceIds", "Values": ["i-bastion"]}])] * 3
    )


@mark.parametrize(
    ("instances", "status"),
    [([], "not registered"), ([{"PingStatus": "ConnectionLost"}], "ConnectionLost")],
)
def test_bastion_ssm_timeout_names_instance_status_and_fix(ssm_wait, instances, status):
    ssm_wait.describe_instance_information.return_value = {"InstanceInformationList": instances}

    with raises(AssertionError) as error:
        assert_bastion.assert_bastion_online("i-bastion")

    assert str(error.value) == (
        "Bastion 'i-bastion' did not become Online in SSM within 600s; "
        f"last status: {status}. Check the SSM Agent logs, "
        "instance IAM role and outbound connectivity to the SSM endpoints."
    )
