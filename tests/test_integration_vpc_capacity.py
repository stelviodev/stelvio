import json
import os
import subprocess
import sys
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError
from botocore.stub import Stubber
from pytest import fixture, mark, param

from tests.integration.vpc_capacity import main

_RUNNER = Path(__file__).parent / "integration" / "run_all.sh"
_QUOTA_PARAMS = {"ServiceCode": "vpc", "QuotaCode": "L-F678F1CE"}
_PREFLIGHT_COMMAND = ["run", "python", "-m", "tests.integration.vpc_capacity"]


@fixture
def preflight_clients(monkeypatch):
    session = boto3.Session(
        aws_access_key_id="testing",
        aws_secret_access_key="testing",  # noqa: S106  # Stubber-only placeholder credentials.
        region_name="us-east-1",
    )
    clients = {name: session.client(name) for name in ("ec2", "service-quotas")}
    selected = []

    def create_session(*, profile_name, region_name):
        selected.append({"profile_name": profile_name, "region_name": region_name})
        return session

    monkeypatch.setattr(session, "client", clients.__getitem__)
    monkeypatch.setattr("tests.integration.vpc_capacity.boto3.Session", create_session)
    monkeypatch.delenv("STLV_TEST_AWS_PROFILE", raising=False)
    monkeypatch.delenv("STLV_TEST_AWS_REGION", raising=False)
    return clients["ec2"], clients["service-quotas"], selected


@mark.parametrize(
    ("quota", "existing", "workers"),
    [
        param(5, 1, 4, id="default-vpc-only"),
        param(5, 3, 2, id="two-leftovers"),
        param(5, 4, 1, id="one-slot"),
        param(20, 1, 4, id="larger-quota-capped"),
    ],
)
def test_vpc_preflight_uses_remaining_capacity(
    preflight_clients, capsys, quota, existing, workers
):
    ec2, quotas, selected = preflight_clients
    with Stubber(quotas) as quota_stub, Stubber(ec2) as ec2_stub:
        quota_stub.add_response("get_service_quota", {"Quota": {"Value": quota}}, _QUOTA_PARAMS)
        ec2_stub.add_response(
            "describe_vpcs",
            {"Vpcs": [{"VpcId": f"vpc-{index:08d}"} for index in range(existing)]},
            {},
        )

        assert main() == 0
        quota_stub.assert_no_pending_responses()
        ec2_stub.assert_no_pending_responses()

    captured = capsys.readouterr()
    assert captured.out == f"{workers}\n"
    assert captured.err == (
        f"VPC tier in us-east-1: quota {quota}, existing {existing}, using {workers} workers.\n"
    )
    assert selected == [{"profile_name": None, "region_name": "us-east-1"}]


def test_vpc_preflight_counts_all_pages_and_uses_selected_profile_and_region(
    preflight_clients, monkeypatch, capsys
):
    ec2, quotas, selected = preflight_clients
    monkeypatch.setenv("STLV_TEST_AWS_PROFILE", "integration-profile")
    monkeypatch.setenv("STLV_TEST_AWS_REGION", "eu-west-1")
    with Stubber(quotas) as quota_stub, Stubber(ec2) as ec2_stub:
        quota_stub.add_response("get_service_quota", {"Quota": {"Value": 5}}, _QUOTA_PARAMS)
        ec2_stub.add_response(
            "describe_vpcs", {"Vpcs": [{"VpcId": "vpc-00000001"}], "NextToken": "page2"}, {}
        )
        ec2_stub.add_response(
            "describe_vpcs",
            {"Vpcs": [{"VpcId": "vpc-00000002"}, {"VpcId": "vpc-00000003"}]},
            {"NextToken": "page2"},
        )

        assert main() == 0
        quota_stub.assert_no_pending_responses()
        ec2_stub.assert_no_pending_responses()

    captured = capsys.readouterr()
    assert captured.out == "2\n"
    assert captured.err == "VPC tier in eu-west-1: quota 5, existing 3, using 2 workers.\n"
    assert selected == [{"profile_name": "integration-profile", "region_name": "eu-west-1"}]


@mark.parametrize("existing", [5, 6])
def test_vpc_preflight_rejects_exhausted_capacity(preflight_clients, capsys, existing):
    ec2, quotas, _selected = preflight_clients
    with Stubber(quotas) as quota_stub, Stubber(ec2) as ec2_stub:
        quota_stub.add_response("get_service_quota", {"Quota": {"Value": 5}}, _QUOTA_PARAMS)
        ec2_stub.add_response(
            "describe_vpcs",
            {"Vpcs": [{"VpcId": f"vpc-{index:08d}"} for index in range(existing)]},
            {},
        )

        assert main() == 1
        quota_stub.assert_no_pending_responses()
        ec2_stub.assert_no_pending_responses()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        f"No VPC capacity available in us-east-1: quota 5, existing {existing}. "
        "Clean up verified orphan integration stacks or increase the regional VPC quota "
        "before rerunning. No tests were launched.\n"
    )


def test_vpc_preflight_reports_aws_permission_failure(preflight_clients, capsys):
    ec2, quotas, _selected = preflight_clients
    with Stubber(quotas) as quota_stub, Stubber(ec2) as ec2_stub:
        quota_stub.add_client_error(
            "get_service_quota",
            service_error_code="AccessDeniedException",
            service_message="Access denied",
            expected_params=_QUOTA_PARAMS,
        )

        assert main() == 1
        quota_stub.assert_no_pending_responses()
        ec2_stub.assert_no_pending_responses()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "VPC preflight failed for profile 'default' in us-east-1: "
        "An error occurred (AccessDeniedException) when calling the GetServiceQuota operation: "
        "Access denied. Check AWS credentials and access to ec2:DescribeVpcs and "
        "servicequotas:GetServiceQuota before rerunning.\n"
    )


def test_vpc_preflight_reports_credential_failure(monkeypatch, capsys):
    def raise_credentials_error(**_kwargs):
        raise BotoCoreError(error_msg="Unable to locate credentials")

    monkeypatch.setattr("tests.integration.vpc_capacity.boto3.Session", raise_credentials_error)

    assert main() == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "An unspecified error occurred" in captured.err
    assert "Check AWS credentials" in captured.err


@fixture
def fake_runner_environment(tmp_path):
    executable = tmp_path / "uv"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "args = sys.argv[1:]\n"
        "flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND\n"
        "fd = os.open(os.environ['RUNNER_TEST_LOG'], flags, 0o600)\n"
        "os.write(fd, (json.dumps(args) + '\\n').encode())\n"
        "os.close(fd)\n"
        "if args == ['run', 'python', '-m', 'tests.integration.vpc_capacity']:\n"
        "    status = int(os.environ.get('RUNNER_TEST_PREFLIGHT_STATUS', '0'))\n"
        "    if status:\n"
        "        print('No VPC capacity available.', file=sys.stderr)\n"
        "    else:\n"
        "        print(os.environ['RUNNER_TEST_WORKERS'])\n"
        "        print('VPC preflight selected 2 workers.', file=sys.stderr)\n"
        "    sys.exit(status)\n"
        "tier = next((arg for arg in args if arg.startswith('--integration')), 'unknown')\n"
        "print(f'pytest stdout for {tier}')\n"
        "print(f'pytest stderr for {tier}', file=sys.stderr)\n"
        "sys.exit(7 if os.environ.get('RUNNER_TEST_FAILED_TIER') in args else 0)\n"
    )
    executable.chmod(0o755)
    log = tmp_path / "commands.jsonl"
    environment = os.environ | {
        "PATH": f"{tmp_path}{os.pathsep}{os.defpath}",
        "RUNNER_TEST_LOG": str(log),
        "RUNNER_TEST_WORKERS": "2",
        "STLV_TEST_DNS_DOMAIN": "example.test",
        "STLV_TEST_DNS_ZONE_ID": "ZTEST",
    }
    return environment, log


@mark.parametrize("failed_tier", [None, "--integration-cf"])
def test_integration_runner_forwards_args_merges_output_and_propagates_failures(
    fake_runner_environment, failed_tier
):
    environment, log = fake_runner_environment
    if failed_tier is not None:
        environment["RUNNER_TEST_FAILED_TIER"] = failed_tier
    result = subprocess.run(  # noqa: S603  # Fixed runner with a hermetic fake uv executable.
        ["/bin/bash", str(_RUNNER), "--disable-warnings", "-k", "document"],
        cwd=_RUNNER.parents[2],
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == (0 if failed_tier is None else 1)
    assert result.stderr == "VPC preflight selected 2 workers.\n"
    for tier in ("--integration", "--integration-vpc", "--integration-cf", "--integration-dns"):
        assert f"pytest stdout for {tier}" in result.stdout
        assert f"pytest stderr for {tier}" in result.stdout

    commands = [json.loads(line) for line in log.read_text().splitlines()]
    assert commands[0] == _PREFLIGHT_COMMAND
    assert sorted(commands[1:]) == sorted(
        [
            [
                "run",
                "pytest",
                "tests/integration",
                tier,
                "-v",
                "--tb=short",
                "--disable-warnings",
                "-k",
                "document",
                "-n",
                workers,
            ]
            for tier, workers in (
                ("--integration", "12"),
                ("--integration-vpc", "2"),
                ("--integration-cf", "7"),
                ("--integration-dns", "3"),
            )
        ]
    )


def test_integration_runner_preflight_failure_launches_no_tiers(fake_runner_environment):
    environment, log = fake_runner_environment
    environment["RUNNER_TEST_PREFLIGHT_STATUS"] = "1"
    result = subprocess.run(  # noqa: S603  # Fixed runner with a hermetic fake uv executable.
        ["/bin/bash", str(_RUNNER)],
        cwd=_RUNNER.parents[2],
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 1
    assert result.stderr == "No VPC capacity available.\n"
    assert [json.loads(line) for line in log.read_text().splitlines()] == [_PREFLIGHT_COMMAND]
