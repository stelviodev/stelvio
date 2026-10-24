"""Disposable private API/Lambda target for actual tunnel CLI acceptance."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import time
from ipaddress import ip_address
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener

from botocore.exceptions import ClientError

from stelvio.stack_outputs import read_network_manifest

from .assert_tunnel import (
    capture_proof_lambda_log_groups,
    delete_proof_lambda_log_groups,
    validate_proof_lambda_function_names,
)
from .cli_session import FAULT_IO, STARTUP_TIMEOUT, StandaloneCliSession


class PrivateApi:
    """Keep an exact, tagged API receipt even when application deployment fails."""

    def __init__(self, sdk, app: str, account: str, region: str, evidence: Path):
        self.client = sdk.client("apigateway", config=FAULT_IO)
        self.app, self.account, self.region = app, account, region
        self.evidence = evidence
        self.api_id = None

    def create(self):
        response = self.client.create_rest_api(
            name=self.app,
            endpointConfiguration={"types": ["PRIVATE"]},
            tags={"stelvio:app": self.app, "stelvio:env": "proof"},
        )
        self.api_id = response["id"]
        self.evidence.write_text(
            json.dumps(
                {
                    "api_id": self.api_id,
                    "app": self.app,
                    "account": self.account,
                    "region": self.region,
                }
            )
        )

    @property
    def hostname(self):
        return f"{self.api_id}.execute-api.{self.region}.amazonaws.com"

    @property
    def url(self):
        return f"https://{self.hostname}/proof/"

    def configure(self, endpoint: str, function_arn: str):
        policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": "*",
                    "Action": "execute-api:Invoke",
                    "Resource": (
                        f"arn:aws:execute-api:{self.region}:{self.account}:{self.api_id}/*"
                    ),
                    "Condition": {"StringEquals": {"aws:SourceVpce": endpoint}},
                }
            ],
        }
        self.client.update_rest_api(
            restApiId=self.api_id,
            patchOperations=[
                {"op": "replace", "path": "/policy", "value": json.dumps(policy)},
                {"op": "add", "path": "/endpointConfiguration/vpcEndpointIds", "value": endpoint},
            ],
        )
        root = self.client.get_resources(restApiId=self.api_id)["items"]
        root_id = next(item["id"] for item in root if item["path"] == "/")
        self.client.put_method(
            restApiId=self.api_id, resourceId=root_id, httpMethod="POST", authorizationType="NONE"
        )
        self.client.put_integration(
            restApiId=self.api_id,
            resourceId=root_id,
            httpMethod="POST",
            type="AWS_PROXY",
            integrationHttpMethod="POST",
            uri=f"arn:aws:apigateway:{self.region}:lambda:path/2015-03-31/functions/{function_arn}/invocations",
        )
        self.client.create_deployment(restApiId=self.api_id, stageName="proof")

    def destroy(self):
        if self.api_id is None:
            return
        try:
            current = self.client.get_rest_api(restApiId=self.api_id)
        except self.client.exceptions.NotFoundException:
            return
        if (
            current.get("name") != self.app
            or current.get("tags", {}).get("stelvio:app") != self.app
            or current.get("tags", {}).get("stelvio:env") != "proof"
        ):
            raise RuntimeError(
                f"Refuse to delete foreign API {self.api_id}; retain {self.evidence}"
            )
        self.client.delete_rest_api(restApiId=self.api_id)
        wait_until(self.absent, timeout=120, description=f"API {self.api_id} absence")

    def absent(self):
        try:
            self.client.get_rest_api(restApiId=self.api_id)
        except self.client.exceptions.NotFoundException:
            return True
        return False


def wait_until(check, *, timeout: float, description: str, interval: float = 1):
    """Retry a bounded predicate; unexpected API errors remain failures."""
    deadline = time.monotonic() + timeout
    while True:
        result = check()
        if result:
            return result
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"Timed out waiting for {description}")
        time.sleep(min(interval, remaining))


def assert_echo(response: dict, *, app: str, nonce: str, payload: str):
    assert response == {
        "backend": app,
        "nonce": nonce,
        "sha256": hashlib.sha256(payload.encode()).hexdigest(),
    }, response


def private_request(url: str, nonce: str, payload: str, *, timeout: float = 10):
    """Use host DNS, a fresh connection, default TLS validation, and no HTTP proxy."""
    if urlparse(url).scheme != "https":
        raise ValueError("Private proof requests require HTTPS")
    request = Request(  # noqa: S310 - HTTPS scheme checked above
        url,
        json.dumps({"nonce": nonce, "payload": payload}).encode(),
        {"Content-Type": "application/json", "Connection": "close"},
        method="POST",
    )
    with build_opener(ProxyHandler({})).open(request, timeout=timeout) as response:
        assert response.status == 200
        return json.loads(response.read(65537))


class LambdaTunnelSession(StandaloneCliSession):
    """Reuse CLI/host lifecycle checks, without the DocumentDB fixture setup."""

    def __init__(self, project: Path):
        # The base's plain-app setup avoids its optional pymongo prerequisite.
        super().__init__(project, no_vpc=True)
        self.no_vpc = False
        self.api = PrivateApi(
            self.aws, self.app, self.account, self.region, self.project / "private-api.json"
        )
        self.endpoint_ids = []
        self.timings = {}

    def _write_project(self):
        self.project.mkdir(parents=True)
        (self.project / "handlers").mkdir()
        probe = Path(__file__).parent / "handlers" / "tunnel_lambda_probe.py"
        (self.project / "handlers" / "probe.py").write_text(probe.read_text())
        (self.project / ".stelvio").mkdir()
        (self.project / ".stelvio" / "userenv").write_text(self.environment_name)
        self.environment = dict(os.environ)
        self.environment.pop("CI", None)
        self.environment.update(
            {
                "AWS_PROFILE": self.profile,
                "AWS_DEFAULT_PROFILE": self.profile,
                "AWS_REGION": self.region,
                "AWS_DEFAULT_REGION": self.region,
                "STLV_TEST_AWS_PROFILE": self.profile,
                "STLV_TEST_AWS_REGION": self.region,
                "STLV_TEST_TUNNEL_INVOCATIONS": str(self.invocation_path),
                "STLV_TEST_TUNNEL_MARKER": self.app,
            }
        )

    def deploy(self):
        started = time.monotonic()
        self.api.create()
        template = (Path(__file__).parent / "handlers" / "tunnel_lambda_app.py.txt").read_text()
        settings = {
            "app": self.app,
            "env": self.environment_name,
            "region": self.region,
            "account": self.account,
            "api_id": self.api.api_id,
            "api_url": self.api.url,
            "hostname": self.api.hostname,
        }
        (self.project / "stlv_app.py").write_text(
            template.replace("SETTINGS_LITERAL", repr(settings))
        )
        self._deployed = True
        self.command("deploy", self.environment_name, "--yes", timeout=STARTUP_TIMEOUT)
        self._outputs = json.loads(self.command("outputs", self.environment_name, "--json"))[
            "user_defined"
        ]
        self.endpoint_ids = [self._outputs["endpoint_id"]]
        self._proof_lambda_function_names = validate_proof_lambda_function_names(
            app=self.app,
            env=self.environment_name,
            function_names=(self._outputs["function1_name"], self._outputs["backend_name"]),
        )
        state = self.state()
        self._state_hash = hashlib.sha256(state).hexdigest()
        self._manifest = read_network_manifest(json.loads(state))
        assert len(self.manifest.enabled_vpcs) == 1
        assert not self.manifest.resources
        self._initialize_proof_host_trust()
        self.api.configure(self._outputs["endpoint_id"], self._outputs["backend_arn"])
        self.timings["deploy_seconds"] = time.monotonic() - started
        self.write_evidence()

    def write_evidence(self):
        (self.project / "lambda-acceptance.json").write_text(
            json.dumps(
                {
                    "app": self.app,
                    "account": self.account,
                    "region": self.region,
                    "api_id": self.api.api_id,
                    "endpoint_ids": self.endpoint_ids,
                    "timings": self.timings,
                },
                indent=2,
            )
        )

    def check_private(self, nonce: str):
        addresses = {
            ip_address(item[4][0])
            for item in socket.getaddrinfo(
                self.api.hostname, 443, socket.AF_INET, socket.SOCK_STREAM
            )
        }
        ec2 = self.aws.client("ec2", config=FAULT_IO)
        endpoint = ec2.describe_vpc_endpoints(VpcEndpointIds=self.endpoint_ids)["VpcEndpoints"][0]
        assert endpoint["VpcId"] == self.manifest.enabled_vpcs[0].vpc_id
        interfaces = ec2.describe_network_interfaces(
            NetworkInterfaceIds=endpoint["NetworkInterfaceIds"]
        )
        endpoint_addresses = {
            ip_address(item["PrivateIpAddress"]) for item in interfaces["NetworkInterfaces"]
        }
        assert addresses, "The API did not resolve to IPv4 addresses"
        assert addresses <= endpoint_addresses, addresses
        private = self.manifest.enabled_vpcs[0].private_subnets
        assert all(any(address in subnet.cidr for subnet in private) for address in addresses)
        expected_interface = self._route_to(private[0].cidr.network_address + 1)["interface"]
        assert expected_interface.startswith("utun")
        for address in addresses:
            assert self._route_to(address)["interface"] == expected_interface
        payload = "private-lambda-payload:" + "0123456789abcdef" * 1024
        response = private_request(self.api.url, nonce, payload)
        assert_echo(response, app=self.app, nonce=nonce, payload=payload)
        return response

    def assert_private_unavailable(self):
        try:
            response = private_request(self.api.url, "outside-tunnel", "blocked", timeout=5)
        except (URLError, TimeoutError, OSError):
            return
        raise AssertionError(f"Private API succeeded without the tunnel: {response}")

    def resolver_ports(self):
        ports = {}
        for path in Path("/etc/resolver").glob("stelvio.*"):
            entries = dict(
                line.split(maxsplit=1)
                for line in path.read_text().splitlines()
                if len(line.split(maxsplit=1)) == 2
            )
            if entries.get("domain") == self.api.hostname and "port" in entries:
                ports[self.api.hostname] = int(entries["port"])
        return ports

    def assert_bastions_running(self):
        identifier = self.manifest.enabled_vpcs[0].access.instance_id
        reservations = self.aws.client("ec2", config=FAULT_IO).describe_instances(
            InstanceIds=[identifier]
        )["Reservations"]
        observed = [
            (item["InstanceId"], item["State"]["Name"])
            for group in reservations
            for item in group["Instances"]
        ]
        assert observed == [(identifier, "running")]

    def check_dev(self, nonce: str):
        status, response = self.invoke_function(1, marker=nonce)
        assert status == 200, response
        assert response["local"] is True
        assert response["pid"] in {item["pid"] for item in self.invocations()}
        assert_echo(response["echo"], app=self.app, nonce=nonce, payload="dev-private-payload")
        self.check_private(nonce + "-host")

    def wait_private(self, nonce: str):
        def probe():
            try:
                return self.check_private(nonce)
            except (URLError, TimeoutError, OSError):
                return None

        return wait_until(probe, timeout=90, description="private HTTPS recovery")

    def _capture_dev_lambda_log_groups(self):
        try:
            groups = capture_proof_lambda_log_groups(
                sdk=self.aws,
                app=self.app,
                env=self.environment_name,
                function_names=self._proof_lambda_function_names,
            )
        except Exception as error:
            return (), [error]
        else:
            return groups, []

    def _remove_dev_lambda_log_groups(self, names):
        try:
            delete_proof_lambda_log_groups(
                sdk=self.aws,
                app=self.app,
                env=self.environment_name,
                function_names=self._proof_lambda_function_names,
                log_group_names=names,
            )
        except Exception as error:
            return [error]
        else:
            return []

    def _assert_aws_resources_absent(self):
        super()._assert_aws_resources_absent()
        if self.endpoint_ids:
            ec2 = self.aws.client("ec2", config=FAULT_IO)

            def endpoints_absent():
                try:
                    values = ec2.describe_vpc_endpoints(VpcEndpointIds=self.endpoint_ids)[
                        "VpcEndpoints"
                    ]
                except ClientError as error:
                    if error.response["Error"]["Code"] == "InvalidVpcEndpointId.NotFound":
                        return True
                    raise
                return not values

            wait_until(endpoints_absent, timeout=120, description="VPC endpoint absence")

    def destroy(self):
        started = time.monotonic()
        errors = []
        # Remove the private API first. Even partial app deployments retain their
        # own cleanup and independent audit; one failure must not skip the other.
        for cleanup in (self.api.destroy, super().destroy):
            try:
                cleanup()
            except Exception as error:
                errors.append(error)
        self.timings["teardown_seconds"] = time.monotonic() - started
        self.write_evidence()
        if errors:
            raise ExceptionGroup(f"Lambda proof cleanup incomplete; retain {self.project}", errors)
