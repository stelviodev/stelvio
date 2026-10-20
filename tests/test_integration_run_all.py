import json
import os
import subprocess
import sys
from pathlib import Path

from pytest import fixture

_RUNNER = Path(__file__).parent / "integration" / "run_all.sh"
_REPO_ROOT = Path(__file__).parents[1]
_TIERS = ("--integration", "--integration-vpc", "--integration-cf", "--integration-dns")


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
        "tiers = ('--integration', '--integration-vpc', '--integration-cf', '--integration-dns')\n"
        "tier = next((arg for arg in args if arg in tiers), 'unknown')\n"
        "print(f'pytest stdout for {tier}')\n"
        "print(f'pytest stderr for {tier}', file=sys.stderr)\n"
        "sys.exit(0)\n"
    )
    executable.chmod(0o755)
    log = tmp_path / "commands.jsonl"
    environment = os.environ | {
        "PATH": f"{tmp_path}{os.pathsep}{os.defpath}",
        "RUNNER_TEST_LOG": str(log),
        "STLV_TEST_DNS_DOMAIN": "example.test",
        "STLV_TEST_DNS_ZONE_ID": "ZTEST",
    }
    return environment, log


def _run_runner(environment, *args):
    return subprocess.run(  # noqa: S603  # Fixed runner with a hermetic fake uv executable.
        ["/bin/bash", str(_RUNNER), *args],
        cwd=_REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def _commands(log):
    return [json.loads(line) for line in log.read_text().splitlines()]


def _expected_command(tier, workers, user_args):
    return [
        "run",
        "pytest",
        "tests/integration",
        tier,
        "-v",
        "--tb=short",
        *user_args,
        "-n",
        str(workers),
    ]


def test_integration_runner_uses_default_workers_forwards_args_and_merges_output(
    fake_runner_environment,
):
    environment, log = fake_runner_environment
    user_args = ["--disable-warnings", "-k", "document db"]

    result = _run_runner(environment, *user_args)

    assert result.returncode == 0
    assert result.stderr == ""
    for tier in _TIERS:
        assert f"pytest stdout for {tier}" in result.stdout
        assert f"pytest stderr for {tier}" in result.stdout
    commands = _commands(log)
    assert sorted(commands) == sorted(
        [
            _expected_command(tier, workers, user_args)
            for tier, workers in zip(_TIERS, (11, 4, 7, 3), strict=True)
        ]
    )
