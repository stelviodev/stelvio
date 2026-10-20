"""Disposable deployment used by standalone tunnel and dev-mode acceptance."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shlex
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from ipaddress import IPv4Network
from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING
from uuid import uuid4
from urllib.error import HTTPError
from urllib.parse import urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen

import boto3
import pytest
from botocore.config import Config

from stelvio.stack_outputs import read_network_manifest
from stelvio.tunnel.credentials import AWS_IO
from stelvio.tunnel.helper_client import NativeHelper
from stelvio.tunnel.host_trust import HostIdentity, fingerprint, locked_host_trust
from stelvio.tunnel.installation import INSTALLED_HELPER

from .assert_tunnel import (
    AbsenceAuditOptions,
    assert_application_absent,
    capture_proof_volume_ids,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

STARTUP_TIMEOUT = 900
DESTROY_TIMEOUT = 3600
STOP_TIMEOUT = 90
FAULT_IO = Config(connect_timeout=5, read_timeout=10, retries={"total_max_attempts": 1})
_SSM_REASON = re.compile(r"stelvio/[0-9a-f]{16}/[0-9a-f]{32}\Z")


class StandaloneCliSession:
    """Own a regular Stelvio deployment and tunnel/dev CLI process for one test."""

    def __init__(
        self, project: Path, *, require_helper: bool = True, no_vpc: bool = False
    ) -> None:
        self.project = project
        self.no_vpc = no_vpc
        self.profile = os.environ.get("STLV_TEST_AWS_PROFILE")
        self.expected_account = os.environ.get("STLV_TEST_TUNNEL_AWS_ACCOUNT")
        if not self.profile or not self.expected_account:
            pytest.skip(
                "Set STLV_TEST_AWS_PROFILE and STLV_TEST_TUNNEL_AWS_ACCOUNT for tunnel acceptance"
            )
        self.region = os.environ.get("STLV_TEST_AWS_REGION", "us-east-1")
        self.environment_name = "tunnel-proof"
        self.app = f"stlv-tunnel-{os.urandom(4).hex()}"
        self.aws = boto3.Session(profile_name=self.profile, region_name=self.region)
        self.account = self.aws.client("sts").get_caller_identity()["Account"]
        if self.account != self.expected_account:
            pytest.fail(
                "Configured AWS credentials do not match STLV_TEST_TUNNEL_AWS_ACCOUNT; "
                "no proof resources were created"
            )
        if require_helper and not INSTALLED_HELPER.is_file():
            pytest.skip("Install the approved native helper before standalone acceptance")
        if require_helper:
            baseline = NativeHelper().status()
            if baseline.owned or baseline.uncertain or baseline.mutating or baseline.units:
                pytest.skip("Standalone acceptance requires an idle native helper and host")
        if not no_vpc and importlib.util.find_spec("pymongo") is None:
            pytest.skip("Install pymongo in the acceptance Python environment")

        bootstrap = self.aws.client("ssm").get_parameter(Name="/stlv/bootstrap")
        self.bucket = json.loads(bootstrap["Parameter"]["Value"])["state"]
        self.cli = Path(sys.executable).with_name("stlv")
        if not self.cli.is_file():
            raise RuntimeError(f"Stelvio CLI executable was not found: {self.cli}")
        self.process: subprocess.Popen | None = None
        self.log_path = self.project / "tunnel-start.log"
        self._log_file = None
        self._deployed = False
        self._state_hash: str | None = None
        self._outputs: dict[str, object] = {}
        self._manifest = None
        self._proof_trust_targets: list[HostIdentity] = []
        self._fault_command_uncertain: str | None = None
        self._mode = "standalone"
        self.invocation_path = self.project / "handler-imports.jsonl"
        self._write_project()

    def _write_project(self) -> None:
        self.project.mkdir(parents=True, exist_ok=True)
        (self.project / "handlers").mkdir()
        source_probe = Path(__file__).parent / "handlers" / "tunnel_dev_probe.py"
        (self.project / "handlers" / "probe.py").write_text(source_probe.read_text())
        (self.project / "handlers" / "plain.py").write_text(
            "import json, os\n"
            "def handler(event, context):\n"
            "    return {'statusCode': 200, 'headers': {'Content-Type': 'application/json'}, "
            "'body': json.dumps({'plain': True, "
            "'marker': os.environ.get('STLV_TEST_TUNNEL_MARKER'), "
            "'query_marker': (event.get('queryStringParameters') or {}).get('marker'), "
            "'pid': os.getpid()})}\n"
        )
        source = """
import os
from stelvio import export_output
from stelvio.app import StelvioApp
from stelvio.aws.document_db import DocumentDb
from stelvio.aws.function import Function
from stelvio.aws.vpc import Vpc
from stelvio.config import AwsConfig, StelvioAppConfig

app = StelvioApp(APP_NAME)

@app.config
def config(env):
    return StelvioAppConfig(
        aws=AwsConfig(profile=os.environ['STLV_TEST_AWS_PROFILE'],
                      region=os.environ['STLV_TEST_AWS_REGION']),
        environments=['tunnel-proof'],
    )

def relocate(octet):
    def adjust(props):
        return props | {'cidr_block': props['cidr_block'].replace('10.0.', f'10.{octet}.')}
    return {key: adjust for key in ('vpc', 'public_subnet', 'private_subnet', 'isolated_subnet')}

@app.run
def run():
"""
        if self.no_vpc:
            source += """
    function = Function('plain', handler='handlers/plain.handler', url='public')
    export_output('function1_url', function.url)
"""
        else:
            source += """
    for number, octet, bastion in ((1, 252, True), (2, 253, {})):
        vpc = Vpc(f'net{number}', az=2, bastion=bastion, customize=relocate(octet))
        database = DocumentDb(f'docdb{number}', vpc=vpc, replicas=1 if number == 1 else 0)
        function = Function(
            f'client{number}', handler='handlers/probe.handler', vpc=vpc,
            links=[database], requirements=['pymongo'], url='public',
        )
        export_output(f'vpc{number}_id', vpc.resources.vpc.id)
        export_output(f'bastion{number}_id', vpc.resources.bastion.id)
        export_output(f'cluster{number}_id', database.resources.cluster.id)
        export_output(f'function{number}_name', function.resources.function.name)
        export_output(f'function{number}_url', function.url)
    disabled = Vpc('net3', az=2, bastion=False, customize=relocate(254))
    disabled_function = Function(
        'disabled-client', handler='handlers/plain.handler', vpc=disabled, url='public'
    )
    plain_function = Function('plain', handler='handlers/plain.handler', url='public')
    export_output('vpc3_id', disabled.resources.vpc.id)
    export_output('function3_url', disabled_function.url)
    export_output('function4_url', plain_function.url)
"""
        source = source.replace("APP_NAME", repr(self.app))
        (self.project / "stlv_app.py").write_text(source)
        (self.project / ".stelvio").mkdir()
        (self.project / ".stelvio" / "userenv").write_text(self.environment_name)
        self.environment = dict(os.environ)
        self.environment.pop("CI", None)
        self.environment["AWS_PROFILE"] = self.profile
        self.environment["AWS_DEFAULT_PROFILE"] = self.profile
        self.environment["AWS_REGION"] = self.region
        self.environment["AWS_DEFAULT_REGION"] = self.region
        self.environment["STLV_TEST_AWS_PROFILE"] = self.profile
        self.environment["STLV_TEST_AWS_REGION"] = self.region
        self.environment["STLV_TEST_TUNNEL_INVOCATIONS"] = str(self.invocation_path)
        self.environment["STLV_TEST_TUNNEL_MARKER"] = self.app

    def _command_evidence(self, directory: Path, metadata: dict, stdout, stderr) -> None:
        def publish(name, value):
            descriptor = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w") as stream:
                stream.write(value)

        def text(value):
            return (
                value.decode("utf-8", errors="replace")
                if isinstance(value, bytes)
                else value or ""
            )

        publish("stdout.log", text(stdout))
        publish("stderr.log", text(stderr))
        publish("command.json", json.dumps(metadata, indent=2))

    def _command_failure_context(self, directory: Path, args: tuple[str, ...]) -> str:
        detail = (
            f"Proof {self.app}/{self.environment_name}; project {self.project}; "
            f"command evidence {directory} (stdout.log, stderr.log, command.json)."
        )
        if args[0] == "destroy":
            invocation = shlex.join([str(self.cli), "destroy", self.environment_name, "--yes"])
            unlock = shlex.join([str(self.cli), "unlock", self.environment_name])
            detail += (
                f" From {shlex.quote(str(self.project))}, finish cleanup with: {invocation}."
                " If state is locked, first verify no deployment operation or Pulumi process"
                f" remains, then run {unlock} and retry destroy."
                " Preserve ownership evidence and independently verify AWS absence."
            )
        return detail

    def command(self, *args: str, timeout: int = 3600) -> str:
        directory = Path(tempfile.mkdtemp(prefix=f"cli-{args[0]}-", dir=self.project))
        metadata = {"arguments": list(args), "timeout_seconds": timeout, "timed_out": False}
        try:
            result = subprocess.run(  # noqa: S603 - selected local CLI and proof-owned project
                [str(self.cli), *args],
                cwd=self.project,
                env=self.environment,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            metadata.update(timed_out=True, exit_status=None)
            self._command_evidence(directory, metadata, error.stdout, error.stderr)
            error.add_note(self._command_failure_context(directory, args))
            raise
        metadata["exit_status"] = result.returncode
        self._command_evidence(directory, metadata, result.stdout, result.stderr)
        if result.returncode:
            raise RuntimeError(
                f"CLI {args} failed ({result.returncode}); "
                f"{self._command_failure_context(directory, args)} "
                f"{result.stdout[-3000:]}\n{result.stderr[-3000:]}"
            )
        return result.stdout

    def deploy(self) -> None:
        self._deployed = True
        self.command("deploy", self.environment_name, "--yes", timeout=STARTUP_TIMEOUT)
        outputs = json.loads(self.command("outputs", self.environment_name, "--json"))
        self._outputs = outputs["user_defined"]
        state = self.state()
        self._state_hash = hashlib.sha256(state).hexdigest()
        self._manifest = read_network_manifest(json.loads(state))
        assert len(self._manifest.enabled_vpcs) == (0 if self.no_vpc else 2)
        assert len(self._manifest.used_vpcs) == (0 if self.no_vpc else 3)
        assert len(self._manifest.resources) == (0 if self.no_vpc else 2)
        self._initialize_proof_host_trust()

    def _initialize_proof_host_trust(self) -> None:
        """Claim only initially absent pins for newly deployed proof instances."""
        self._proof_trust_targets = []
        partition = self.aws.get_partition_for_region(self.region)
        for network in self.manifest.enabled_vpcs:
            target = HostIdentity(
                partition, network.account, network.region, network.access.instance_id
            )
            with locked_host_trust(target, Event()) as entry:
                if entry.key is not None:
                    raise RuntimeError(
                        "Proof instance already has SSH trust; retain it for inspection"
                    )
            self._proof_trust_targets.append(target)

    def host_pins(self) -> dict[str, str]:
        pins = {}
        for target in self._proof_trust_targets:
            with locked_host_trust(target, Event()) as entry:
                if entry.key is None:
                    raise AssertionError(f"Ready proof instance has no SSH pin: {target.label}")
                pins[target.label] = entry.key
        return pins

    def _clear_proof_host_trust(self) -> None:
        # Only after AWS absence is certified. Never remove a pre-existing pin
        # or unlink stable per-identity locks, which could split concurrent users.
        for target in getattr(self, "_proof_trust_targets", ()):
            with locked_host_trust(target, Event()) as entry:
                if entry.key is not None:
                    entry.reset(fingerprint(entry.key))

    def state(self) -> bytes:
        response = self.aws.client("s3").get_object(
            Bucket=self.bucket,
            ExpectedBucketOwner=self.account,
            Key=f"state/{self.app}/{self.environment_name}.json",
        )
        with response["Body"] as body:
            return body.read()

    @property
    def manifest(self):
        return self._manifest

    def start(self) -> None:
        self._start(["tunnel", "start", self.environment_name], mode="standalone")

    def start_dev(self) -> None:
        self._start(["dev", self.environment_name], mode="dev")

    def _start(self, arguments: list[str], *, mode: str) -> None:
        if self.process is not None and self.process.poll() is None:
            raise RuntimeError("Tunnel or dev command is already running")
        self._mode = mode
        self._log_file = self.log_path.open("wb")
        self.process = subprocess.Popen(  # noqa: S603 - selected local Stelvio CLI, explicit project cwd
            [str(self.cli), *arguments],
            cwd=self.project,
            env=self.environment,
            stdout=self._log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    def log(self) -> str:
        return self.log_path.read_text(errors="replace") if self.log_path.exists() else ""

    def wait_ready(self, timeout: float = STARTUP_TIMEOUT) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            output = self.log()
            if self.process is None or self.process.poll() is not None:
                raise RuntimeError(f"Tunnel or dev CLI exited before readiness: {output[-5000:]}")
            bridge_ready = self._mode != "dev" or "local dev server connected to AppSync" in output
            if "Network READY" in output and bridge_ready:
                return
            time.sleep(1)
        raise TimeoutError(f"Tunnel or dev network did not become ready: {self.log_path}")

    def invoke_function(
        self, number: int, timeout: float = 60, *, marker: str | None = None
    ) -> tuple[int, dict | str]:
        url = self._outputs[f"function{number}_url"]
        if urlparse(url).scheme != "https":
            raise ValueError("Proof Function URL must use HTTPS")
        if marker is not None:
            parsed = urlparse(url)
            query = parsed.query
            encoded_marker = urlencode({"marker": marker})
            query = f"{query}&{encoded_marker}" if query else encoded_marker
            url = urlunparse(parsed._replace(query=query))
        request = Request(url, method="GET")  # noqa: S310 - URL was verified as HTTPS
        try:
            with urlopen(request, timeout=timeout) as response:  # noqa: S310 - proof-app URL
                status = response.status
                body = response.read().decode()
        except HTTPError as error:
            status = error.code
            body = error.read().decode(errors="replace")
        try:
            return status, json.loads(body)
        except json.JSONDecodeError:
            return status, body

    def invocations(self) -> list[dict]:
        if not self.invocation_path.exists():
            return []
        return [json.loads(line) for line in self.invocation_path.read_text().splitlines()]

    def _route_to(self, address) -> dict[str, str]:
        from .macos_host import run

        output = run("/sbin/route", "-n", "get", "-inet", str(address)).stdout
        fields = {}
        for line in output.splitlines():
            key, separator, value = line.strip().partition(":")
            if separator and key in {"destination", "mask", "interface"}:
                assert key not in fields, f"Ambiguous route lookup: {output}"
                fields[key] = value.strip()
        with (self.project / "standalone-routes.jsonl").open("a") as evidence:
            evidence.write(json.dumps({"address": str(address), "output": output}) + "\n")
        assert fields.get("interface"), f"No route interface: {output}"
        return fields

    def assert_selected_routes(self) -> None:
        """Prove exact subnet routes and exclusion of public/unused/resolver space."""
        interfaces = set()
        for network in self.manifest.enabled_vpcs:
            assert network.route_cidrs, "Proof VPC has no private or isolated subnets"
            unit_interfaces = set()
            for cidr in network.route_cidrs:
                route = self._route_to(cidr.network_address + 1)
                assert "destination" in route, route
                assert "mask" in route, route
                assert IPv4Network(f"{route['destination']}/{route['mask']}") == cidr, route
                assert route["interface"].startswith("utun"), route
                unit_interfaces.add(route["interface"])
            assert len(unit_interfaces) == 1, unit_interfaces
            assert not (unit_interfaces & interfaces), "VPCs share a routed interface"
            interfaces.update(unit_interfaces)

        for network in self.manifest.enabled_vpcs:
            excluded = {network.cidrs[0].network_address + 2}
            excluded.update(subnet.cidr.network_address + 1 for subnet in network.public_subnets)
            for block in network.cidrs:
                unused = block.broadcast_address - 1
                assert not any(unused in subnet.cidr for subnet in network.subnets), (
                    "Proof must leave unused address space for route exclusion checks"
                )
                excluded.add(unused)
            for address in excluded:
                route = self._route_to(address)
                assert route["interface"] not in interfaces, (
                    f"Excluded VPC address {address} uses a tunnel route: {route}"
                )

    def wait_host_key_refusal(self, instance_id: str) -> str:
        assert self.process is not None
        code = self.process.wait(timeout=STOP_TIMEOUT)
        output = self.log()
        assert code == 1, output
        assert "cause=ssh-host-key" in output
        assert instance_id in output
        assert "stlv tunnel trust reset" in output
        assert "Network READY" not in output
        return output

    def run_ssm_commands(
        self, instance_id: str, commands: list[str], *, timeout: float = 120
    ) -> str:
        """Run a bounded fault command only on a verified proof-owned bastion."""
        if getattr(self, "_fault_command_uncertain", None) is not None:
            raise RuntimeError("Previous SSM fault command is uncertain; do not issue another")
        if not 30 <= timeout <= 600:
            raise ValueError("SSM fault command timeout must be between 30 and 600 seconds")
        networks = [
            vpc for vpc in self.manifest.enabled_vpcs if vpc.access.instance_id == instance_id
        ]
        if len(networks) != 1:
            raise RuntimeError("Refuse SSM fault command outside the proof deployment")
        reservations = self.aws.client("ec2", config=AWS_IO).describe_instances(
            InstanceIds=[instance_id]
        )["Reservations"]
        instances = [
            (reservation.get("OwnerId"), node)
            for reservation in reservations
            for node in reservation["Instances"]
        ]
        if len(instances) != 1:
            raise RuntimeError("Proof bastion identity is ambiguous")
        owner, node = instances[0]
        tags = {tag["Key"]: tag["Value"] for tag in node.get("Tags", [])}
        if (
            owner != self.account
            or node.get("InstanceId") != instance_id
            or node.get("VpcId") != networks[0].vpc_id
            or tags.get("stelvio:app") != self.app
            or tags.get("stelvio:env") != self.environment_name
            or node.get("State", {}).get("Name") != "running"
        ):
            raise RuntimeError("Proof bastion ownership changed; fault command refused")
        ssm = self.aws.client("ssm", config=FAULT_IO)
        comment = f"stelvio-test/{self.app}/{uuid4().hex}"
        self._fault_command_uncertain = comment
        result = ssm.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            Comment=comment,
            Parameters={
                "commands": ["set -eu\n" + "\n".join(commands)],
                "executionTimeout": [str(int(timeout))],
            },
            TimeoutSeconds=min(int(timeout), 600),
        )
        command_id = result["Command"]["CommandId"]
        self._fault_command_uncertain = command_id
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                invocation = ssm.get_command_invocation(
                    CommandId=command_id, InstanceId=instance_id
                )
            except ssm.exceptions.InvocationDoesNotExist:
                time.sleep(1)
                continue
            status = invocation.get("Status")
            if status == "Success":
                self._fault_command_uncertain = None
                return invocation.get("StandardOutputContent", "")
            if status in {"Cancelled", "TimedOut", "Failed"}:
                self._fault_command_uncertain = None
                raise RuntimeError(f"Proof-owned SSM command ended with {status}")
            time.sleep(1)
        raise TimeoutError(f"Proof-owned SSM command {command_id} exceeded {timeout}s")

    @contextmanager
    def rotated_host_key(self, instance_id: str) -> Iterator[str]:
        nonce = uuid4().hex
        backup = shlex.quote(f"/run/stelvio-test-key-{nonce}")
        setup = [
            f"backup={backup}",
            "key=/etc/ssh/ssh_host_ed25519_key",
            'test ! -e "$backup"',
            'mkdir -m 0700 "$backup"',
            f'printf "%s\\n" {nonce} > "$backup/owner"',
            'cp -p "$key" "$backup/original"',
            'cp -p "$key.pub" "$backup/original.pub"',
            'ssh-keygen -q -t ed25519 -N "" -f "$backup/changed"',
            'install -m 0600 "$backup/changed" "$key"',
            'install -m 0644 "$backup/changed.pub" "$key.pub"',
            "/usr/sbin/sshd -t",
            "systemctl restart sshd",
            'cat "$key.pub"',
        ]
        restore = [
            f"backup={backup}",
            "key=/etc/ssh/ssh_host_ed25519_key",
            'if test -d "$backup"; then',
            f'  test "$(cat "$backup/owner")" = {nonce}',
            '  if test -f "$backup/original" && test -f "$backup/original.pub"; then',
            '    cp -p "$backup/original" "$key"',
            '    cp -p "$backup/original.pub" "$key.pub"',
            "    /usr/sbin/sshd -t",
            "    systemctl restart sshd",
            "  fi",
            '  rm -rf -- "$backup"',
            "fi",
        ]
        try:
            yield " ".join(self.run_ssm_commands(instance_id, setup).split()[:2])
        finally:
            if getattr(self, "_fault_command_uncertain", None) is not None:
                raise RuntimeError(
                    f"SSM fault command outcome is uncertain ({self._fault_command_uncertain}); "
                    "do not race host-key restoration. "
                    "Retain the command evidence and destroy the proof-owned bastion."
                )
            self.run_ssm_commands(instance_id, restore)

    def check_database(
        self,
        cluster_id: str,
        timeout: float = 30,
        *,
        marker: str | None = None,
        marker_expected: bool = True,
    ) -> dict:
        ca_file = self.project / ".stelvio" / "aws" / "documentdb" / "global-bundle.pem"
        script = """
import boto3, json, sys, uuid
from pymongo import MongoClient
session = boto3.Session()
client = session.client("docdb")
cluster = client.describe_db_clusters(DBClusterIdentifier=sys.argv[1])["DBClusters"][0]
secret = session.client("secretsmanager").get_secret_value(
    SecretId=cluster["MasterUserSecret"]["SecretArn"])
password = json.loads(secret["SecretString"])["password"]
mongo = MongoClient(cluster["Endpoint"], cluster["Port"],
    username=cluster["MasterUsername"], password=password, tls=True,
    tlsCAFile=sys.argv[2], replicaSet="rs0", retryWrites=False,
    serverSelectionTimeoutMS=20000)
try:
    assert mongo.admin.command("ping")["ok"] == 1
    marker = sys.argv[3] if len(sys.argv) > 3 else None
    collection = mongo.stelvio_dev.acceptance
    if marker is None:
        marker = uuid.uuid4().hex
        collection.replace_one({"_id": marker}, {"_id": marker}, upsert=True)
        assert collection.find_one({"_id": marker})["_id"] == marker
        marker_present = True
    else:
        marker_present = collection.find_one({"_id": marker}) is not None
        assert marker_present == (sys.argv[4] == "present")
    expected = set()
    for member in cluster["DBClusterMembers"]:
        instance = client.describe_db_instances(
            DBInstanceIdentifier=member["DBInstanceIdentifier"])["DBInstances"][0]
        expected.add(instance["Endpoint"]["Address"])
    discovered = {host for host, _ in mongo.nodes}
    assert expected <= discovered, (expected, discovered)
    print(json.dumps({"tls": True, "read_write": True,
        "replica_set": mongo.topology_description.replica_set_name,
        "members": sorted(discovered), "marker_present": marker_present}))
finally:
    mongo.close()
"""
        result = subprocess.run(  # noqa: S603 - fixed local verification script
            [
                sys.executable,
                "-I",
                "-c",
                script,
                cluster_id,
                str(ca_file),
                *(
                    [marker, "present" if marker_expected else "absent"]
                    if marker is not None
                    else []
                ),
            ],
            cwd=self.project,
            env=self.environment,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if result.returncode:
            raise RuntimeError(
                f"DocumentDB proof failed for {cluster_id}: {result.stderr[-2000:]}"
            )
        return json.loads(result.stdout)

    def wait_database(self, cluster_id: str, timeout: float = 600) -> dict:
        deadline = time.monotonic() + timeout
        last_error = None
        while time.monotonic() < deadline:
            try:
                return self.check_database(cluster_id, timeout=30)
            except (RuntimeError, subprocess.TimeoutExpired) as error:
                last_error = error
                time.sleep(2)
        raise TimeoutError(f"DocumentDB did not recover for {cluster_id}: {last_error}")

    def wait_unit(self, identity: str, status_name: str, *, after: int = 0, timeout: float = 90):
        unit_id = hashlib.sha256(identity.encode()).hexdigest()[:8]
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = NativeHelper().status()
            unit = next((item for item in state.units if item.unit == unit_id), None)
            if (
                unit is not None
                and unit.status.name.lower() == status_name
                and (after == 0 or unit.generation > after)
            ):
                return unit
            time.sleep(1)
        raise TimeoutError(f"VPC unit {identity} did not reach {status_name}")

    def wait_network_ready(self, *, after: int = 0, timeout: float = STARTUP_TIMEOUT) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            output = self.log()
            if "Network READY" in output[after:]:
                return
            if self.process is None or self.process.poll() is not None:
                raise RuntimeError(f"Dev CLI exited before network readiness: {output[-5000:]}")
            time.sleep(1)
        raise TimeoutError("Dev session did not report fresh Network READY status")

    def wait_worker_cause(
        self, identity: str, cause: str, *, state: str = "failed", timeout: float = 180
    ) -> None:
        deadline = time.monotonic() + timeout
        needle = f"{identity}: {state} "
        while time.monotonic() < deadline:
            if self.process is None or self.process.poll() is not None:
                raise RuntimeError(
                    f"Dev CLI exited before {identity} reported {cause}: {self.log()}"
                )
            if any(
                line.lstrip().startswith(needle) and f"cause={cause}" in line
                for line in self.log().splitlines()
            ):
                return
            time.sleep(1)
        raise TimeoutError(f"VPC {identity} did not report {state} cause={cause}")

    def terminate_transport(self, instance_id: str) -> None:
        owned = [item for item in self.active_ssm_sessions() if item.get("Target") == instance_id]
        assert len(owned) == 1, f"Expected one owned transport to {instance_id}, got {owned}"
        self.aws.client("ssm").terminate_session(SessionId=owned[0]["SessionId"])

    @staticmethod
    def dns_rcode(hostname: str, port: int) -> int:
        transaction_id = int.from_bytes(os.urandom(2), "big")
        labels = hostname.rstrip(".").split(".")
        question = (
            b"".join(bytes([len(label)]) + label.encode("ascii") for label in labels) + b"\0"
        )
        query = struct.pack("!HHHHHH", transaction_id, 0x0100, 1, 0, 0, 0) + question
        query += struct.pack("!HH", 1, 1)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
            client.settimeout(2)
            client.sendto(query, ("127.0.0.1", port))
            response, _address = client.recvfrom(4096)
        response_id, flags = struct.unpack("!HH", response[:4])
        assert response_id == transaction_id
        return flags & 0x000F

    def wait_dns_rcode(self, hostname: str, port: int, expected: int, timeout: float = 45) -> None:
        deadline = time.monotonic() + timeout
        last_error = None
        while time.monotonic() < deadline:
            try:
                if self.dns_rcode(hostname, port) == expected:
                    return
            except (OSError, AssertionError) as error:
                last_error = error
            time.sleep(1)
        raise TimeoutError(f"Scoped DNS did not return rcode={expected}: {last_error}")

    def wait_function_marker(self, number: int, marker: str, *, timeout: float = 90) -> dict:
        deadline = time.monotonic() + timeout
        last_response = None
        while time.monotonic() < deadline:
            status, response = self.invoke_function(number, timeout=20, marker=marker)
            last_response = (status, response)
            if status == 200 and isinstance(response, dict) and response.get("marker") == marker:
                return response
            time.sleep(1)
        raise TimeoutError(f"Function {number} did not return marker {marker}: {last_response}")

    def terminate(self, signum: int = signal.SIGINT) -> int:
        if self.process is None:
            raise RuntimeError("Standalone command was not started")
        if self.process.poll() is None:
            self.process.send_signal(signum)
        try:
            return self.process.wait(timeout=STOP_TIMEOUT)
        finally:
            if self._log_file is not None:
                self._log_file.close()
                self._log_file = None

    def active_ssm_sessions(self) -> list[dict]:
        ssm = self.aws.client("ssm")
        values = []
        for vpc in self.manifest.enabled_vpcs:
            sessions = ssm.describe_sessions(
                State="Active", Filters=[{"key": "Target", "value": vpc.access.instance_id}]
            )["Sessions"]
            owned = [item for item in sessions if _SSM_REASON.fullmatch(item.get("Reason", ""))]
            assert len(owned) <= 1
            values.extend(owned)
        return values

    def assert_sessions_terminated(self, receipts: list[dict], timeout: float = 60) -> None:
        ssm = self.aws.client("ssm")
        expected = {item["SessionId"] for item in receipts}
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            active = {
                item["SessionId"]
                for vpc in self.manifest.enabled_vpcs
                for item in ssm.describe_sessions(
                    State="Active",
                    Filters=[{"key": "Target", "value": vpc.access.instance_id}],
                )["Sessions"]
                if item["SessionId"] in expected
            }
            histories = {
                session_id: ssm.describe_sessions(
                    State="History", Filters=[{"key": "SessionId", "value": session_id}]
                )["Sessions"]
                for session_id in expected
            }
            if not active and all(
                len(values) == 1 and values[0]["Status"] == "Terminated"
                for values in histories.values()
            ):
                return
            time.sleep(1)
        raise AssertionError(
            f"Owned SSM sessions were not certified terminated: {sorted(expected)}"
        )

    def resolver_ports(self) -> dict[str, int]:
        domains = {
            hostname for resource in self.manifest.resources for hostname in resource.hostnames
        }
        ports = {}
        directory = Path("/etc/resolver")
        for path in directory.glob("stelvio.*"):
            lines = path.read_text().splitlines()
            domain = next(
                (line.split(maxsplit=1)[1] for line in lines if line.startswith("domain ")), None
            )
            port = next(
                (int(line.split(maxsplit=1)[1]) for line in lines if line.startswith("port ")),
                None,
            )
            if domain in domains and port is not None:
                ports[domain] = port
        return ports

    def wait_for_helper_cleanup(self, timeout: float = 60) -> None:
        helper = NativeHelper()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = helper.status()
            if not state.owned and not state.uncertain and not state.mutating and not state.units:
                return
            time.sleep(1)
        raise AssertionError("Native helper still owns or is uncertain about host state")

    def assert_bastions_running(self) -> None:
        ids = [self._outputs[f"bastion{number}_id"] for number in (1, 2)]
        instances = self.aws.client("ec2").describe_instances(InstanceIds=ids)["Reservations"]
        observed = {
            instance["InstanceId"]: instance["State"]["Name"]
            for reservation in instances
            for instance in reservation["Instances"]
        }
        assert observed == dict.fromkeys(ids, "running")

    def assert_state_unchanged(self) -> None:
        assert hashlib.sha256(self.state()).hexdigest() == self._state_hash

    def destroy(self) -> None:
        if not self._deployed:
            return
        if self.process is not None and self.process.poll() is None:
            self.terminate()
        if INSTALLED_HELPER.exists():
            state = NativeHelper().status()
            if state.owned or state.uncertain or state.mutating or state.units:
                raise RuntimeError(
                    f"Retaining proof deployment {self.app}/{self.environment_name}: "
                    "native helper cleanup is not certified; preserve evidence and recover first"
                )
        elif not self.no_vpc:
            raise RuntimeError("Retaining VPC proof deployment because the helper is unavailable")
        volume_capture_error = None
        try:
            bastion_ids = [
                self._outputs[key]
                for key in ("bastion1_id", "bastion2_id")
                if self._outputs.get(key)
            ]
            self._bastion_volume_ids = capture_proof_volume_ids(
                sdk=self.aws, app=self.app, env=self.environment_name, instance_ids=bastion_ids
            )
        except Exception as error:
            # Missing outputs or an inventory error must not prevent cleanup of
            # a partially deployed proof app. The post-destroy audit still checks
            # all tagged and name-owned resources, then reports the volume gap.
            self._bastion_volume_ids = ()
            volume_capture_error = error
        errors = []
        if volume_capture_error is not None:
            error = RuntimeError("Bastion block-device absence could not be certified")
            error.__cause__ = volume_capture_error
            errors.append(error)
        try:
            self.command("destroy", self.environment_name, "--yes", timeout=DESTROY_TIMEOUT)
        except Exception as error:
            errors.append(error)
        try:
            self._assert_aws_resources_absent()
        except Exception as error:
            errors.append(error)
        if len(errors) > 1:
            raise ExceptionGroup("Proof cleanup incomplete", errors)
        if errors:
            raise errors[0]
        self._clear_proof_host_trust()

    def _assert_aws_resources_absent(self) -> None:
        assert_application_absent(
            self.app,
            self.environment_name,
            sdk=self.aws,
            options=AbsenceAuditOptions(volume_ids=getattr(self, "_bastion_volume_ids", ())),
        )
