"""Real CLI owner with durable logs/state and bounded, explicit teardown."""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import time
from base64 import urlsafe_b64encode
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from botocore.exceptions import ClientError

from stelvio.stack_outputs import read_network_manifest
from stelvio.tunnel.access_state import AccessIntent
from stelvio.tunnel.credentials import AWS_IO
from stelvio.tunnel.helper_client import HelperError, NativeHelper
from stelvio.tunnel.processes import ProcessIdentity, identity

from .assert_helpers import _boto3_session
from .assert_tunnel import assert_access_absent, assert_application_absent
from .tunnel_cleanup import purge_test_metadata

REPO = Path(__file__).resolve().parents[2]
EXAMPLE = REPO / "spikes/vpc-tunnel-app"
STARTUP_SECONDS = 1200
SHUTDOWN_SECONDS = 700
DESTROY_SECONDS = 3600  # Provider subnet deletion allows 45 minutes, plus DB/network teardown.


class CliSession:
    def __init__(self, policy: str = "True") -> None:
        self.run_id = uuid4().hex[:6]
        self.app = f"stlv-{self.run_id}"
        self.env = "test"
        self.marker = str(uuid4())
        self.project = REPO / "spikes/dev-vpc-v1/build/p6" / self.run_id
        self.project.mkdir(parents=True)
        shutil.copytree(EXAMPLE / "functions", self.project / "functions")
        source = (EXAMPLE / "stlv_app.py").read_text()
        assert source.count('StelvioApp("vpc-tunnel")') == 1
        assert source.count("aws=AwsConfig(") == 1
        source = source.replace('StelvioApp("vpc-tunnel")', f'StelvioApp("{self.app}")')
        source = source.replace("aws=AwsConfig(", "aws=" + self.provider_source()[:-1] + ",")
        if zones := self.availability_zones():
            source = source.replace('Vpc("net", ', f'Vpc("net", az={zones!r}, ')
        if policy == "omitted":
            source = source.replace(", bastion=True", "")
        elif policy != "multi":
            source = source.replace("bastion=True", f"bastion={policy}")
        (self.project / "stlv_app.py").write_text(source)
        handler = self.project / "functions/ping.py"
        handler.write_text(
            handler.read_text().replace(
                '"ok": bool(ping_ok and read_back and host_matches),',
                '"ok": bool(ping_ok and read_back and host_matches),\n'
                '        "local_marker": __import__("os").environ.get("STLV_P6_MARKER"),\n'
                '        "pid": __import__("os").getpid(),\n'
                '        "uid": __import__("os").geteuid(),\n'
                '        "replica_set": client.topology_description.replica_set_name,',
            )
        )
        marker_source = (
            '    proof_path = __import__("os").environ["STLV_P6_INVOCATIONS"]\n'
            '    with __import__("pathlib").Path(proof_path).open("a") as proof:\n'
            '        proof.write(json.dumps({"id": getattr(_context, "aws_request_id", None), '
            '"pid": __import__("os").getpid()}) + "\\n")\n'
        )
        handler.write_text(
            handler.read_text().replace(
                "def handler(_event: object, _context: object) -> dict[str, object]:\n",
                "def handler(_event: object, _context: object) -> dict[str, object]:\n"
                + marker_source,
            )
        )
        (self.project / ".stelvio").mkdir()
        (self.project / ".stelvio/userenv").write_text(self.env)
        self.environment = dict(os.environ)
        self.environment.pop("CI", None)
        self.environment.pop("AWS_DEFAULT_PROFILE", None)
        self.environment.update(
            AWS_PROFILE=os.environ["STLV_TEST_AWS_PROFILE"],
            AWS_DEFAULT_REGION=os.environ.get("STLV_TEST_AWS_REGION", "us-east-1"),
            AWS_REGION=os.environ.get("STLV_TEST_AWS_REGION", "us-east-1"),
            STLV_P6_MARKER=self.marker,
            STLV_P6_INVOCATIONS=str(self.project / "invocations.jsonl"),
            PYTHONUNBUFFERED="1",
            COLUMNS="240",
        )
        self.cli = Path(
            os.environ.get("STLV_TEST_TUNNEL_PYTHON", str(EXAMPLE / ".venv/bin/python"))
        ).with_name("stlv")
        self.process = None
        self.started = None
        self.log = self.project / "dev.log"
        self.sdk = _boto3_session()
        bootstrap = self.sdk.client("ssm", config=AWS_IO).get_parameter(Name="/stlv/bootstrap")
        self.bucket = json.loads(bootstrap["Parameter"]["Value"])["state"]
        self.account = self.sdk.client("sts", config=AWS_IO).get_caller_identity()["Account"]
        self._record()

    @staticmethod
    def provider_source() -> str:
        profile = os.environ["STLV_TEST_AWS_PROFILE"]
        region = os.environ.get("STLV_TEST_AWS_REGION", "us-east-1")
        return f"AwsConfig(profile={profile!r}, region={region!r})"

    @staticmethod
    def availability_zones() -> list[str] | None:
        value = os.environ.get("STLV_TEST_TUNNEL_AZS")
        if value is None:
            return None
        zones = value.split(",")
        region = os.environ.get("STLV_TEST_AWS_REGION", "us-east-1")
        if len(zones) != len(set(zones)) or not all(
            re.fullmatch(re.escape(region) + "[a-z]", zone) for zone in zones
        ):
            raise ValueError("STLV_TEST_TUNNEL_AZS must list unique AZ names in the proof region")
        return zones

    def _record(self) -> None:
        (self.project / "ownership.json").write_text(
            json.dumps(
                {
                    "app": self.app,
                    "env": self.env,
                    "bucket": self.bucket,
                    "account": self.account,
                    "region": self.sdk.region_name,
                    "profile": os.environ["STLV_TEST_AWS_PROFILE"],
                    "pid": self.process.pid if self.process else None,
                    "project": str(self.project),
                    "started": self.started,
                },
                indent=2,
            )
        )

    def evidence(self, phase: str, value: dict) -> None:
        path = self.project / "evidence.jsonl"
        with path.open("a") as file:
            file.write(json.dumps({"phase": phase, **value}) + "\n")

    def invocations(self) -> list[dict]:
        path = self.project / "invocations.jsonl"
        return (
            [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        )

    def command(self, *args: str, timeout: int = STARTUP_SECONDS) -> str:
        path = self.project / ("-".join(args[:2]) + ".log")
        with path.open("wb") as log:
            process = subprocess.Popen(  # noqa: S603 - selected test venv's real CLI
                [str(self.cli), *args],
                cwd=self.project,
                env=self.environment,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGINT)
                try:
                    process.wait(timeout=SHUTDOWN_SECONDS)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=10)
                raise
        result = path.read_text()
        if process.returncode:
            raise RuntimeError(
                f"CLI {args} failed ({process.returncode}); retained log {path}: {result[-5000:]}"
            )
        return result

    def start(self) -> None:
        if self.log.exists():
            assert self.process is None or self.process.poll() is not None
            self.log.rename(self.project / f"dev-{uuid4().hex}.log")
        self.started = time.time()
        with self.log.open("wb") as log:
            self.process = subprocess.Popen(  # noqa: S603 - selected test venv's real CLI
                [str(self.cli), "dev"],
                cwd=self.project,
                env=self.environment,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        self._record()

    def wait_ready(self) -> None:
        deadline = time.monotonic() + STARTUP_SECONDS
        while time.monotonic() < deadline:
            output = self.log.read_text()
            if self.process.poll() is not None:
                raise RuntimeError(f"Dev stopped; retained log {self.log}: {output[-6000:]}")
            if "Network READY" in output and "local dev server connected to AppSync" in output:
                self.capture_ownership()
                return
            if "failed attempt=" in output:
                raise RuntimeError(
                    f"Dev network failed; retained log {self.log}: {output[-6000:]}"
                )
            time.sleep(1)
        raise TimeoutError(f"Dev readiness expired; retained log {self.log}")

    def capture_ownership(self) -> None:
        """Keep resolved identities from state/journal, never parse deployment prose."""
        intents = self.access_intents()
        manifest = self.manifest()
        transports = self.capture_transports(manifest, intents)
        path = self.project / "resolved-ownership.json"
        if path.exists():
            path.rename(self.project / f"resolved-ownership-{uuid4().hex}.json")
        path.write_text(
            json.dumps(
                {
                    "vpcs": [v.vpc_id for v in manifest.used_vpcs],
                    "intents": intents,
                    "transports": transports,
                },
                indent=2,
            )
        )

    def capture_transports(self, manifest, intents: list[dict]) -> list[dict]:
        """Capture receipts for unique fixture targets, including persistent-only sessions."""
        ec2 = self.sdk.client("ec2", config=AWS_IO)
        expected = {sha256(i["session"].encode()).hexdigest()[:16] for i in intents}
        transports = []
        for vpc in manifest.enabled_vpcs:
            filters = [
                {"Name": "vpc-id", "Values": [vpc.vpc_id]},
                {"Name": "instance-state-name", "Values": ["running"]},
            ]
            instances = [
                i
                for r in ec2.describe_instances(Filters=filters)["Reservations"]
                for i in r["Instances"]
            ]
            if vpc.access:
                instances = [i for i in instances if i["InstanceId"] == vpc.access.instance_id]
            else:
                instances = [
                    i
                    for i in instances
                    if any(
                        t["Key"] == "stlv:tunnel-session"
                        and sha256(t["Value"].encode()).hexdigest()[:16] in expected
                        for t in i.get("Tags", [])
                    )
                ]
            assert len(instances) == 1
            if vpc.access:
                tags = {t["Key"]: t["Value"] for t in instances[0]["Tags"]}
                assert tags["stelvio:app"] == self.app
                assert tags["stelvio:env"] == self.env
            transports.append(self.transport_receipt(instances[0]["InstanceId"], expected))
        assert len({r["owner_hash"] for r in transports}) == 1
        return transports

    def transport_receipt(self, instance: str, expected: set[str]) -> dict:
        ssm = self.sdk.client("ssm", config=AWS_IO)
        receipts = [
            s
            for p in ssm.get_paginator("describe_sessions").paginate(
                State="Active", Filters=[{"key": "Target", "value": instance}]
            )
            for s in p["Sessions"]
        ]
        # Ambiguous, foreign-principal or pre-existing sessions fail before effects.
        assert len(receipts) == 1
        receipt = receipts[0]
        principal = self.sdk.client("sts", config=AWS_IO).get_caller_identity()["Arn"]
        assert receipt["Owner"] == principal
        assert receipt["StartDate"].timestamp() >= self.started
        match = re.fullmatch(r"stelvio/([0-9a-f]{16})/[0-9a-f]{32}", receipt["Reason"])
        assert match
        if expected:
            assert match[1] in expected
        return {
            "instance": instance,
            "session_id": receipt["SessionId"],
            "reason": receipt["Reason"],
            "owner_hash": match[1],
        }

    def access_intents(self) -> list[dict]:
        """Discover only this fixture's durable access namespace, even before READY."""
        app_segment = urlsafe_b64encode(self.app.encode()).decode().rstrip("=")
        env_segment = urlsafe_b64encode(self.env.encode()).decode().rstrip("=")
        prefix = f"tunnel/{app_segment}/{env_segment}/"
        s3 = self.sdk.client("s3", config=AWS_IO)
        intents = []
        for page in s3.get_paginator("list_objects_v2").paginate(
            Bucket=self.bucket,
            ExpectedBucketOwner=self.account,
            Prefix=prefix,
        ):
            for item in page.get("Contents", []):
                if item["Key"].endswith(("/intent.json", "/metadata-cleanup.json")):
                    response = s3.get_object(
                        Bucket=self.bucket, ExpectedBucketOwner=self.account, Key=item["Key"]
                    )
                    with response["Body"] as body:
                        saved = json.loads(body.read())
                    value = saved.get("intent", saved)
                    intent = AccessIntent.from_dict(value)
                    assert intent.app == self.app
                    assert intent.environment == self.env
                    assert intent.account == self.account
                    assert intent.region == self.sdk.region_name
                    assert intent.owner_uid == os.geteuid()
                    assert item["Key"].startswith(intent.prefix)
                    intents.append(value)
        return intents

    def transport_owner(self) -> str:
        saved = json.loads((self.project / "resolved-ownership.json").read_text())
        owners = {i["owner_hash"] for i in saved["transports"]}
        assert len(owners) == 1
        return owners.pop()

    def state(self) -> dict:
        response = self.sdk.client("s3", config=AWS_IO).get_object(
            Bucket=self.bucket,
            ExpectedBucketOwner=self.account,
            Key=f"state/{self.app}/{self.env}.json",
        )
        with response["Body"] as body:
            return json.loads(body.read())

    def manifest(self):
        return read_network_manifest(self.state())

    def url(self) -> str:
        urls = self.urls()
        if len(urls) != 1:
            raise ValueError(f"Expected one example Function URL: {urls}")
        return next(iter(urls.values()))

    def urls(self) -> dict[str, str]:
        outputs = json.loads(self.command("outputs", "--json"))
        pending = list(outputs.get("components", []))
        urls = {}
        while pending:
            component = pending.pop()
            pending.extend(component.get("components", []))
            if component["type"] == "Function" and component.get("outputs", {}).get("url"):
                urls[component["name"]] = component["outputs"]["url"]
        return urls

    def make_multi_vpc(self) -> None:
        """Two distinct-CIDR databases and a non-VPC handler in the same CLI owner."""
        source = """from stelvio.app import StelvioApp
from stelvio.config import StelvioAppConfig, AwsConfig
from stelvio.aws.vpc import Vpc, NatConfig, BastionConfig
from stelvio.aws.document_db import DocumentDb
from stelvio.aws.function import Function
app = StelvioApp(APP_NAME)
@app.config
def configuration(env):
    return StelvioAppConfig(aws=AwsConfig())
def relocate(octet):
    def adjust(props):
        return props | {"cidr_block": props["cidr_block"].replace("10.0.", f"10.{octet}.")}
    return {k: adjust for k in ("vpc", "public_subnet", "private_subnet", "isolated_subnet")}
@app.run
def run():
    for number, policy in ((1, None), (2, BastionConfig())):
        vpc = Vpc(f"net{number}", nat=NatConfig(type="managed", single=True),
                  bastion=policy, customize=relocate(252+number))
        db = DocumentDb(f"docdb{number}", vpc=vpc)
        Function(f"api{number}", handler=f"functions/ping{number}.handler",
                 requirements=["pymongo"], url="public", vpc=vpc, links=[db], memory=256)
    Function("plain", handler="functions/plain.handler", url="public")
    opted = Vpc("opted-net", bastion=False, customize=relocate(255))
    Function("opted", handler="functions/plain.handler", url="public", vpc=opted)
"""
        self.private_domain = f"p6-{self.run_id}.internal"
        source = source.replace(
            "BastionConfig()", f"BastionConfig(dns_domains=[{self.private_domain!r}])"
        )
        source = source.replace("AwsConfig()", self.provider_source())
        if zones := self.availability_zones():
            source = source.replace(
                'Vpc(f"net{number}", ', f'Vpc(f"net{{number}}", az={zones!r}, '
            )
            source = source.replace('Vpc("opted-net", ', f'Vpc("opted-net", az={zones!r}, ')
        (self.project / "stlv_app.py").write_text(source.replace("APP_NAME", repr(self.app)))
        original = (self.project / "functions/ping.py").read_text()
        for number in (1, 2):
            marker = f"{self.run_id}-database-{number}"
            (self.project / f"functions/ping{number}.py").write_text(
                original.replace("Resources.docdb", f"Resources.docdb{number}")
                .replace('_MARKER = "ok"', f"_MARKER = {marker!r}")
                .replace(
                    '"read_back": read_back,', '"read_back": read_back, "data_marker": _MARKER,'
                )
            )
        (self.project / "functions/plain.py").write_text(
            "import os, json\n"
            "def handler(event, context):\n"
            "    return {'statusCode':200, 'body': json.dumps({'plain':True, "
            "'local_marker':os.environ['STLV_P6_MARKER'], 'pid':os.getpid()})}\n"
        )

    def private_zone(self, vpc_id: str, hostname: str) -> str:
        zone_path = self.project / "private-zone.json"
        reference = f"{self.app}-{self.env}-private-zone"
        zone_path.write_text(
            json.dumps({"reference": reference, "domain": self.private_domain, "vpc": vpc_id})
        )
        route53 = self.sdk.client("route53", config=AWS_IO)
        response = route53.create_hosted_zone(
            Name=self.private_domain,
            CallerReference=reference,
            VPC={"VPCRegion": self.sdk.region_name, "VPCId": vpc_id},
            HostedZoneConfig={"PrivateZone": True},
        )
        zone = response["HostedZone"]["Id"]
        zone_path.write_text(
            json.dumps(
                {"reference": reference, "domain": self.private_domain, "vpc": vpc_id, "id": zone}
            )
        )
        route53.get_waiter("resource_record_sets_changed").wait(
            Id=response["ChangeInfo"]["Id"], WaiterConfig={"Delay": 2, "MaxAttempts": 90}
        )
        return self.private_alias("db", hostname)

    def private_alias(self, label: str, hostname: str) -> str:
        saved = json.loads((self.project / "private-zone.json").read_text())
        route53 = self.sdk.client("route53", config=AWS_IO)
        zone = saved["id"]
        name = label + "." + self.private_domain
        change = route53.change_resource_record_sets(
            HostedZoneId=zone,
            ChangeBatch={
                "Changes": [
                    {
                        "Action": "CREATE",
                        "ResourceRecordSet": {
                            "Name": name,
                            "Type": "CNAME",
                            "TTL": 30,
                            "ResourceRecords": [{"Value": hostname}],
                        },
                    }
                ]
            },
        )
        route53.get_waiter("resource_record_sets_changed").wait(
            Id=change["ChangeInfo"]["Id"], WaiterConfig={"Delay": 2, "MaxAttempts": 90}
        )
        return name

    def remove_private_zone(self) -> None:
        path = self.project / "private-zone.json"
        if not path.exists():
            return
        saved = json.loads(path.read_text())
        route53 = self.sdk.client("route53", config=AWS_IO)
        zones = [
            z
            for page in route53.get_paginator("list_hosted_zones").paginate()
            for z in page["HostedZones"]
            if z["CallerReference"] == saved["reference"]
        ]
        for zone in zones:
            assert zone["Name"] == saved["domain"] + "."
            assert zone["Config"]["PrivateZone"] is True
            records = [
                r
                for page in route53.get_paginator("list_resource_record_sets").paginate(
                    HostedZoneId=zone["Id"]
                )
                for r in page["ResourceRecordSets"]
                if r["Type"] not in {"NS", "SOA"}
            ]
            if records:
                route53.change_resource_record_sets(
                    HostedZoneId=zone["Id"],
                    ChangeBatch={
                        "Changes": [{"Action": "DELETE", "ResourceRecordSet": r} for r in records]
                    },
                )
            route53.delete_hosted_zone(Id=zone["Id"])

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGINT)
            try:
                self.process.wait(timeout=SHUTDOWN_SECONDS)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=10)
                raise RuntimeError(
                    f"Dev shutdown incomplete; retained ownership {self.project}"
                ) from None
        if self.process:
            self._record()

    def crash_and_recover(self) -> None:
        """Crash only our CLI; EOF watchdog stops transport before AWS recovery."""
        os.kill(self.process.pid, signal.SIGKILL)
        self.process.wait(timeout=10)
        self.recover_access()
        self.evidence("crash-recovered", {"complete": True})

    def recover_access(self) -> None:
        """Wait for EOF cleanup; never steal a live creator's durable claim."""
        self.wait_host_empty()
        intents = self.access_intents()
        if intents:
            (self.project / f"resolved-ownership-recovery-{uuid4().hex}.json").write_text(
                json.dumps({"intents": intents, "transports": []})
            )
        for saved in intents:
            self.wait_creator_dead(AccessIntent.from_dict(saved))
        owners = sorted({(i["session"], i["account"], i["region"]) for i in intents})
        failures = []
        for owner, account, region in owners:
            try:
                self.command(
                    "tunnel",
                    "recover",
                    "--bucket",
                    self.bucket,
                    "--home-account",
                    self.account,
                    "--home-region",
                    self.sdk.region_name,
                    "--home-profile",
                    self.environment["AWS_PROFILE"],
                    "--account",
                    account,
                    "--region",
                    region,
                    "--profile",
                    self.environment["AWS_PROFILE"],
                    "--app",
                    self.app,
                    "--env",
                    self.env,
                    "--session",
                    owner,
                )
            except Exception as error:
                failures.append(error)
        if failures:
            raise ExceptionGroup(f"Retained access ownership {self.project}", failures)

    def wait_host_empty(self) -> None:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                host = NativeHelper().inspect()
            except (HelperError, BrokenPipeError):
                time.sleep(1)
                continue
            if not host.owned and not host.uncertain and not host.units:
                break
            time.sleep(1)
        else:
            raise TimeoutError("EOF host cleanup not certified")

    def wait_creator_dead(self, intent: AccessIntent) -> None:
        s3 = self.sdk.client("s3", config=AWS_IO)
        response = s3.list_objects_v2(
            Bucket=self.bucket, ExpectedBucketOwner=self.account, Prefix=intent.prefix
        )
        if not any(i["Key"] == intent.prefix + "claim.json" for i in response.get("Contents", [])):
            return
        try:
            response = s3.get_object(
                Bucket=self.bucket,
                ExpectedBucketOwner=self.account,
                Key=intent.prefix + "claim.json",
            )
        except ClientError as error:
            if error.response["Error"]["Code"] == "NoSuchKey":
                # Valid disposal can remove the claim after listing. Recovery
                # and complete AWS absence checks still run in the caller.
                return
            raise
        with response["Body"] as body:
            creator = ProcessIdentity(**json.loads(body.read())["creator"])
        # The EOF SDK watchdog may need its 600+30s production disposal bound.
        deadline = time.monotonic() + SHUTDOWN_SECONDS
        while True:
            actual = identity(creator.pid)
            if actual is None or not creator.same_process(actual) or actual.status == 5:
                return
            if time.monotonic() >= deadline:
                raise RuntimeError(f"Creator still live; retain ownership {self.project}")
            time.sleep(1)

    def destroy(self) -> None:
        failures = []
        for action in (
            self.stop,
            self.recover_access,
            self.remove_private_zone,
            lambda: self.command("destroy", "--yes", "--json", timeout=DESTROY_SECONDS),
        ):
            try:
                action()
            except Exception as error:
                failures.append(error)
        if failures:
            raise ExceptionGroup(f"Cleanup incomplete; retain ownership {self.project}", failures)
        inventory = NativeHelper().inspect()
        assert not inventory.owned
        assert not inventory.uncertain
        assert not inventory.units
        app_segment = urlsafe_b64encode(self.app.encode()).decode().rstrip("=")
        env_segment = urlsafe_b64encode(self.env.encode()).decode().rstrip("=")
        s3 = self.sdk.client("s3", config=AWS_IO)
        pages = s3.get_paginator("list_object_versions").paginate(
            Bucket=self.bucket,
            ExpectedBucketOwner=self.account,
            Prefix=f"tunnel/{app_segment}/{env_segment}/",
        )
        assert not any(p.get("Versions") or p.get("DeleteMarkers") for p in pages)
        assert_application_absent(self.app, self.env, include_lambda_logs=False)
        sessions, owner_hashes = set(), set()
        for path in self.project.glob("resolved-ownership*.json"):
            saved = json.loads(path.read_text())
            sessions.update(i["session"] for i in saved["intents"])
            owner_hashes.update(i["owner_hash"] for i in saved["transports"])
        owner_hashes.update(sha256(s.encode()).hexdigest()[:16] for s in sessions)
        assert_access_absent(self.sdk, sessions, owner_hashes)
        self.evidence(
            "cleanup",
            purge_test_metadata(
                self.sdk, app=self.app, env=self.env, bucket=self.bucket, account=self.account
            ),
        )
