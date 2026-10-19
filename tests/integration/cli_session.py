"""Disposable deployed application used only by standalone tunnel acceptance."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import boto3
import pytest

from stelvio.stack_outputs import read_network_manifest
from stelvio.tunnel.helper_client import NativeHelper
from stelvio.tunnel.installation import INSTALLED_HELPER

STARTUP_TIMEOUT = 900
STOP_TIMEOUT = 90
_SSM_REASON = re.compile(r"stelvio/[0-9a-f]{16}/[0-9a-f]{32}\Z")


class StandaloneCliSession:
    """Own a regular Stelvio deployment and standalone CLI processes for one test."""

    def __init__(self, project: Path) -> None:
        self.project = project
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
        if not INSTALLED_HELPER.is_file():
            pytest.skip("Install the approved native helper before standalone acceptance")
        baseline = NativeHelper().inspect()
        if baseline.owned or baseline.uncertain or baseline.mutating or baseline.units:
            pytest.skip("Standalone acceptance requires an idle native helper and host")
        if importlib.util.find_spec("pymongo") is None:
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
        self._write_project()

    def _write_project(self) -> None:
        self.project.mkdir(parents=True, exist_ok=True)
        (self.project / "handlers").mkdir()
        (self.project / "handlers" / "probe.py").write_text(
            "def handler(event, context):\n    return {'ok': True}\n"
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
    for number, octet in ((1, 252), (2, 253)):
        vpc = Vpc(f'net{number}', az=2, bastion=True, customize=relocate(octet))
        database = DocumentDb(f'docdb{number}', vpc=vpc, instances=2 if number == 1 else 1)
        function = Function(
            f'client{number}', handler='handlers/probe.handler', vpc=vpc,
            links=[database], requirements=['pymongo'],
        )
        export_output(f'vpc{number}_id', vpc.resources.vpc.id)
        export_output(f'bastion{number}_id', vpc.resources.bastion.id)
        export_output(f'cluster{number}_id', database.resources.cluster.id)
        export_output(f'function{number}_name', function.resources.function.name)
""".replace("APP_NAME", repr(self.app))
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

    def command(self, *args: str, timeout: int = 3600) -> str:
        result = subprocess.run(  # noqa: S603 - selected local Stelvio CLI for proof app ownership
            [str(self.cli), *args],
            cwd=self.project,
            env=self.environment,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if result.returncode:
            raise RuntimeError(
                f"CLI {args} failed ({result.returncode}); retained project {self.project}: "
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
        assert len(self._manifest.enabled_vpcs) == 2
        assert len(self._manifest.resources) == 2

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
        if self.process is not None and self.process.poll() is None:
            raise RuntimeError("Standalone command is already running")
        self._log_file = self.log_path.open("wb")
        self.process = subprocess.Popen(  # noqa: S603 - selected local Stelvio CLI, explicit project cwd
            [str(self.cli), "tunnel", "start", self.environment_name],
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
                raise RuntimeError(f"Standalone CLI exited before readiness: {output[-5000:]}")
            if "Network READY" in output:
                return
            time.sleep(1)
        raise TimeoutError(f"Standalone network did not become ready: {self.log_path}")

    def check_database(self, cluster_id: str, timeout: float = 30) -> dict:
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
    marker = uuid.uuid4().hex
    collection = mongo.stelvio_standalone.acceptance
    collection.replace_one({"_id": marker}, {"_id": marker, "marker": marker}, upsert=True)
    assert collection.find_one({"_id": marker})["marker"] == marker
    expected = set()
    for member in cluster["DBClusterMembers"]:
        instance = client.describe_db_instances(
            DBInstanceIdentifier=member["DBInstanceIdentifier"])["DBInstances"][0]
        expected.add(instance["Endpoint"]["Address"])
    discovered = {host for host, _ in mongo.nodes}
    assert expected <= discovered, (expected, discovered)
    print(json.dumps({"tls": True, "read_write": True,
        "replica_set": mongo.topology_description.replica_set_name,
        "members": sorted(discovered)}))
finally:
    mongo.close()
"""
        result = subprocess.run(  # noqa: S603 - fixed local verification script
            [sys.executable, "-I", "-c", script, cluster_id, str(ca_file)],
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
            inspection = NativeHelper().inspect()
            unit = next((item for item in inspection.units if item.unit == unit_id), None)
            if (
                unit is not None
                and unit.status.name.lower() == status_name
                and (after == 0 or unit.generation > after)
            ):
                return unit
            time.sleep(1)
        raise TimeoutError(f"VPC unit {identity} did not reach {status_name}")

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
            state = helper.inspect()
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
        state = NativeHelper().inspect()
        if state.owned or state.uncertain or state.mutating or state.units:
            raise RuntimeError(
                f"Retaining proof deployment {self.app}/{self.environment_name}: "
                "native helper cleanup is not certified; preserve evidence and recover first"
            )
        self.command("destroy", self.environment_name, "--yes", timeout=STARTUP_TIMEOUT)
        if self._outputs:
            self._assert_aws_resources_absent()

    def _assert_aws_resources_absent(self) -> None:
        ec2 = self.aws.client("ec2")
        instance_ids = [self._outputs[f"bastion{number}_id"] for number in (1, 2)]
        reservations = ec2.describe_instances(
            Filters=[{"Name": "instance-id", "Values": instance_ids}]
        )["Reservations"]
        assert not any(reservation["Instances"] for reservation in reservations)
        for number in (1, 2):
            cluster_id = self._outputs[f"cluster{number}_id"]
            try:
                self.aws.client("docdb").describe_db_clusters(DBClusterIdentifier=cluster_id)
            except self.aws.client("docdb").exceptions.DBClusterNotFoundFault:
                pass
            else:
                raise AssertionError(f"Proof DocumentDB cluster remains: {cluster_id}")
            function_name = self._outputs[f"function{number}_name"]
            try:
                self.aws.client("lambda").get_function(FunctionName=function_name)
            except self.aws.client("lambda").exceptions.ResourceNotFoundException:
                pass
            else:
                raise AssertionError(f"Proof Lambda remains: {function_name}")
            vpc_id = self._outputs[f"vpc{number}_id"]
            assert not ec2.describe_vpcs(VpcIds=[vpc_id])["Vpcs"]
