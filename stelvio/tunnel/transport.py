"""Authenticated, nonroot SSH-over-SSM/EIC with a session-long server pin."""

from __future__ import annotations

import base64
import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import suppress
from hashlib import sha256
from pathlib import Path
from threading import RLock
from typing import TYPE_CHECKING
from uuid import uuid4

from botocore.exceptions import ClientError

from stelvio.tunnel.bastion import SSH_USER, identity_document
from stelvio.tunnel.credentials import AWS_IO
from stelvio.tunnel.policy import BastionPolicy
from stelvio.tunnel.processes import identity
from stelvio.tunnel.socks import SocksConnector, TransportInterruptedError

if TYPE_CHECKING:
    from collections.abc import Callable
    from threading import Event

    import boto3

    from stelvio.tunnel.manifest import VpcNetwork

HOST_KEY_BYTES = 51
HOST_KEY_PREFIX = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20"
SSH_PORT = 22
ONLINE_SECONDS = 300
COMMAND_SECONDS = 120
HASH_LENGTH = 64
ZOMBIE = 5


class TransportRetryError(ConnectionError):
    """Transient AWS/SSM/SSH connectivity; retry with revalidation."""


class TransportAuthorizationError(RuntimeError):
    """Authorization or deployed ownership was revoked; do not reconnect."""


class ServerIdentityError(TransportAuthorizationError):
    """An authenticated bootstrap returned a different host key."""


def classify_aws(error: ClientError) -> Exception:
    code = error.response.get("Error", {}).get("Code", "")
    if code in {
        "AccessDenied",
        "AccessDeniedException",
        "UnauthorizedOperation",
        "InvalidClientTokenId",
        "ExpiredToken",
        "ExpiredTokenException",
        "UnrecognizedClientException",
    }:
        return TransportAuthorizationError("VPC transport AWS authorization is unavailable")
    return TransportRetryError("VPC transport AWS operation is temporarily unavailable")


def parse_host_key(output: str) -> str:
    try:
        key, ready = output.strip().splitlines()
        kind, encoded, *_ = key.split()
        wire = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as error:
        raise ServerIdentityError("SSM returned a malformed server identity") from error
    if (
        ready != "stelvio-tunnel-ready"
        or kind != "ssh-ed25519"
        or len(wire) != HOST_KEY_BYTES
        or not wire.startswith(HOST_KEY_PREFIX)
    ):
        raise ServerIdentityError("SSM did not return the owned ready Ed25519 server")
    return f"{kind} {encoded}"


class SshTransport:
    def __init__(  # noqa: PLR0913 - explicit identity, provider and ownership boundaries
        self,
        network: VpcNetwork,
        session: boto3.Session,
        stop: Event,
        *,
        ownership: dict[str, str] | None = None,
        record: Callable[[str, dict], None] | None = None,
        owner_id: str | None = None,
    ) -> None:
        self.network = network
        self.session = session
        self.stop = stop
        self.ownership = ownership
        self.record = record
        self.owner_id = owner_id or network.identity
        self._receipt: str | None = None
        self._reason: str | None = None
        self.pin: str | None = None
        self.process: subprocess.Popen | None = None
        self._process_identity = None
        self._document_version: str | None = None
        self._document_hash: str | None = None
        self._stopping = RLock()
        self.session_id: str | None = None
        self._terminated = False
        self._temporary: tempfile.TemporaryDirectory | None = None
        self.ssm = session.client("ssm", config=AWS_IO)
        self.ec2 = session.client("ec2", config=AWS_IO)

    def _pause(self, seconds: float) -> None:
        if self.stop.wait(seconds):
            raise TransportInterruptedError("VPC transport attempt cancelled")

    def _owned(self, tags: dict[str, str]) -> bool:
        if self.network.policy == BastionPolicy.TEMPORARY:
            return tags.get("stlv:tunnel-session") == self.network.access.owner
        return (
            self.network.access.owner == self.network.identity
            and bool(self.ownership)
            and all(tags.get(key) == value for key, value in self.ownership.items())
        )

    def validate_owner(self) -> None:  # noqa: C901 - independent owned-resource boundaries
        access = self.network.access
        if access is None or self.session.region_name != self.network.region:
            raise TransportAuthorizationError("VPC transport is missing its resolved access owner")
        if (
            self.session.client("sts", config=AWS_IO).get_caller_identity()["Account"]
            != self.network.account
        ):
            raise TransportAuthorizationError("VPC transport AWS account changed")
        instances = [
            node
            for reservation in self.ec2.describe_instances(InstanceIds=[access.instance_id])[
                "Reservations"
            ]
            for node in reservation["Instances"]
        ]
        if len(instances) != 1:
            raise TransportAuthorizationError("VPC access instance identity is ambiguous")
        node = instances[0]
        tags = {tag["Key"]: tag["Value"] for tag in node.get("Tags", [])}
        if (
            node.get("VpcId") != self.network.vpc_id
            or node.get("Placement", {}).get("AvailabilityZone") != access.availability_zone
            or access.security_group_id not in {g["GroupId"] for g in node["SecurityGroups"]}
            or not self._owned(tags)
        ):
            raise TransportAuthorizationError("VPC access instance ownership changed")
        if node.get("State", {}).get("Name") != "running":
            raise TransportRetryError("VPC access instance is not running")
        groups = self.ec2.describe_security_groups(
            GroupIds=[group["GroupId"] for group in node["SecurityGroups"]]
        )["SecurityGroups"]
        if len(groups) != len(node["SecurityGroups"]):
            raise TransportAuthorizationError("VPC access security group inventory is incomplete")
        for group in groups:
            if (
                group.get("VpcId") != self.network.vpc_id
                or group.get("OwnerId") != self.network.account
            ):
                raise TransportAuthorizationError("VPC access security group ownership changed")
            for rule in group.get("IpPermissions", []):
                if rule["IpProtocol"] == "-1" or (
                    rule["IpProtocol"] in ("tcp", "6")
                    and rule["FromPort"] <= SSH_PORT <= rule["ToPort"]
                ):
                    raise TransportAuthorizationError(
                        "VPC access admits inbound SSH; redeploy before dev"
                    )
        document = self.ssm.get_document(Name=access.identity_document, DocumentVersion="$DEFAULT")
        tags = {
            tag["Key"]: tag["Value"]
            for tag in self.ssm.list_tags_for_resource(
                ResourceType="Document", ResourceId=access.identity_document
            )["TagList"]
        }
        if json.loads(document["Content"]) != json.loads(identity_document()) or not self._owned(
            tags
        ):
            raise TransportAuthorizationError("VPC identity document ownership/content changed")
        version = document["DocumentVersion"]
        verified = self.ssm.describe_document(
            Name=access.identity_document, DocumentVersion=version
        )["Document"]
        if (
            verified.get("DocumentVersion") != version
            or verified.get("HashType") != "Sha256"
            or len(verified.get("Hash", "")) != HASH_LENGTH
            or sha256(document["Content"].encode()).hexdigest() != verified.get("Hash")
        ):
            raise TransportAuthorizationError("VPC identity document version/hash is unavailable")
        self._document_version, self._document_hash = version, verified["Hash"]

    def _bootstrap(self) -> str:
        self.validate_owner()
        access = self.network.access
        deadline = time.monotonic() + ONLINE_SECONDS
        while time.monotonic() < deadline:
            self._pause(0)
            information = self.ssm.describe_instance_information(
                Filters=[{"Key": "InstanceIds", "Values": [access.instance_id]}]
            )["InstanceInformationList"]
            if len(information) == 1 and information[0].get("PingStatus") == "Online":
                break
            self._pause(1)
        else:
            raise TransportRetryError("VPC access SSM agent did not become online")
        command = self.ssm.send_command(
            InstanceIds=[access.instance_id],
            DocumentName=access.identity_document,
            DocumentVersion=self._document_version,
            DocumentHash=self._document_hash,
            DocumentHashType="Sha256",
            TimeoutSeconds=90,
        )["Command"]["CommandId"]
        deadline = time.monotonic() + COMMAND_SECONDS
        while time.monotonic() < deadline:
            self._pause(0)
            try:
                result = self.ssm.get_command_invocation(
                    CommandId=command, InstanceId=access.instance_id
                )
            except self.ssm.exceptions.InvocationDoesNotExist:
                self._pause(1)
                continue
            if result["Status"] == "Success":
                return parse_host_key(result["StandardOutputContent"])
            if result["Status"] not in ("Pending", "InProgress", "Delayed"):
                raise TransportRetryError("VPC host bootstrap command did not succeed")
            self._pause(1)
        raise TransportRetryError("VPC host bootstrap exceeded its budget")

    def start(self) -> SocksConnector:
        if not os.geteuid() or os.getuid() != os.geteuid():
            raise TransportAuthorizationError(
                "VPC transport must run as the ordinary nonroot user"
            )
        if self.process is not None or self.session_id is not None:
            raise RuntimeError("Dispose the previous VPC transport before reconnecting")
        try:
            return self._start()
        except ClientError as error:
            raise classify_aws(error) from None

    def _start(self) -> SocksConnector:  # noqa: C901 - authenticated bootstrap and staged ownership
        pin = self._bootstrap()
        if self.pin is not None and self.pin != pin:
            raise ServerIdentityError("VPC SSH server identity changed; reconnect refused")
        self.pin = pin
        plugin = shutil.which("session-manager-plugin")
        if not plugin:
            raise TransportAuthorizationError(
                "Install session-manager-plugin for VPC dev networking"
            )
        self._temporary = tempfile.TemporaryDirectory(prefix="stelvio-ssh-")
        directory = Path(self._temporary.name)
        key = directory / "client"
        subprocess.run(  # noqa: S603 - fixed key generator and owned temporary path
            ["/usr/bin/ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
        access = self.network.access
        hosts = directory / "known_hosts"
        hosts.write_text(f"{access.instance_id} {pin}\n")
        hosts.chmod(0o600)
        with socket.socket() as reserve:
            reserve.bind(("127.0.0.1", 0))
            port = reserve.getsockname()[1]
        # Token input is transient environment, never state, manifest or command prose.
        self._receipt = uuid4().hex
        self._reason = f"stelvio/{sha256(self.owner_id.encode()).hexdigest()[:16]}/{self._receipt}"
        if self.record:
            self.record(
                f"transport-start-{self._receipt}.json",
                {"instance": access.instance_id, "reason": self._reason},
            )
        result = self.ssm.start_session(
            Target=access.instance_id,
            DocumentName="AWS-StartSSHSession",
            Parameters={"portNumber": ["22"]},
            Reason=self._reason,
        )
        self.session_id = result["SessionId"]
        self._terminated = False
        if self.record:
            self.record(
                f"transport-observed-{self._receipt}.json", {"session_id": self.session_id}
            )
        environment = {
            key: value for key, value in os.environ.items() if not key.startswith("AWS_")
        }
        environment.update(
            STLV_SSM_SESSION=json.dumps(result),
            STLV_SSM_PLUGIN=str(Path(plugin).resolve()),
            STLV_SSM_REGION=self.network.region,
            STLV_SSM_ENDPOINT=self.ssm.meta.endpoint_url,
            STLV_SSM_TARGET=access.instance_id,
        )
        response = self.session.client("ec2-instance-connect", config=AWS_IO).send_ssh_public_key(
            InstanceId=access.instance_id,
            AvailabilityZone=access.availability_zone,
            InstanceOSUser=SSH_USER,
            SSHPublicKey=key.with_suffix(".pub").read_text().strip(),
        )
        if not response.get("Success"):
            raise TransportAuthorizationError("EC2 Instance Connect rejected the client key")
        options = {
            "ProxyCommand": shlex.join([sys.executable, "-I", "-m", "stelvio.tunnel.ssm_proxy"]),
            "HostKeyAlias": access.instance_id,
            "StrictHostKeyChecking": "yes",
            "UserKnownHostsFile": str(hosts),
            "GlobalKnownHostsFile": os.devnull,
            "IdentitiesOnly": "yes",
            "IdentityAgent": "none",
            "ForwardAgent": "no",
            "PermitLocalCommand": "no",
            "RequestTTY": "no",
            "BatchMode": "yes",
            "ExitOnForwardFailure": "yes",
            "ConnectionAttempts": "1",
            "ConnectTimeout": "15",
            "ServerAliveInterval": "5",
            "ServerAliveCountMax": "2",
            "PreferredAuthentications": "publickey",
            "HostKeyAlgorithms": "ssh-ed25519",
        }
        control = directory / "control"
        arguments = [
            "/usr/bin/ssh",
            "-F",
            os.devnull,
            "-N",
            "-M",
            "-S",
            str(control),
            "-D",
            f"127.0.0.1:{port}",
            "-i",
            str(key),
        ]
        for name, value in options.items():
            arguments.extend(["-o", f"{name}={value}"])
        arguments.append(f"{SSH_USER}@{access.instance_id}")
        self.process = subprocess.Popen(  # noqa: S603 - fixed SSH, fixed options, validated owned identity
            arguments,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        deadline = time.monotonic() + 30
        self._process_identity = identity(self.process.pid)
        if (
            self._process_identity is None
            or self._process_identity.group != self.process.pid
            or self._process_identity.uid != os.geteuid()
        ):
            raise TransportAuthorizationError("SSH process ownership could not be established")
        while time.monotonic() < deadline:
            self._pause(0)
            if not self.healthy():
                raise TransportRetryError("Pinned SSH exited before authentication completed")
            if control.exists():
                checked = subprocess.run(  # noqa: S603 - owned SSH master control socket
                    [
                        "/usr/bin/ssh",
                        "-F",
                        os.devnull,
                        "-S",
                        str(control),
                        "-O",
                        "check",
                        access.instance_id,
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=2,
                    check=False,
                )
                if checked.returncode == 0 and self.healthy():
                    return SocksConnector(port, self.network.cidrs, self.stop)
            self._pause(0.1)
        raise TransportRetryError("Pinned SSH authentication exceeded its budget")

    def healthy(self) -> bool:
        if self.process is None or self._process_identity is None:
            return False
        actual = identity(self.process.pid)
        return bool(
            actual and actual.same_process(self._process_identity) and actual.status != ZOMBIE
        )

    def stop_local(self) -> None:
        with self._stopping:
            self._stop_local()

    def _stop_local(self) -> None:
        if self.process is not None:
            # Never poll/wait/reap before the final group signal. An unreaped
            # birth-fenced leader keeps its PGID from being reused by a stranger.
            for kind in (signal.SIGTERM, signal.SIGKILL):
                actual = identity(self.process.pid)
                if not actual or (
                    self._process_identity and not actual.same_process(self._process_identity)
                ):
                    raise RuntimeError("SSH process identity changed; cleanup refused")
                if actual.group != self.process.pid or actual.uid != os.geteuid():
                    raise RuntimeError("SSH process group ownership changed; cleanup refused")
                with suppress(ProcessLookupError):
                    os.killpg(self.process.pid, kind)
                if kind == signal.SIGTERM:
                    deadline = time.monotonic() + 2
                    while time.monotonic() < deadline:
                        actual = identity(self.process.pid)
                        if actual and actual.status == ZOMBIE:
                            break
                        time.sleep(0.05)
            self.process.wait(timeout=3)
            self.process = None
            self._process_identity = None

    def close(self) -> None:
        failure = None
        self.stop_local()
        if self.session_id is None and self._reason is not None:
            pages = self.ssm.get_paginator("describe_sessions").paginate(
                State="Active",
                Filters=[{"key": "Target", "value": self.network.access.instance_id}],
                PaginationConfig={"MaxItems": 1000, "PageSize": 50},
            )
            found = [
                session["SessionId"]
                for page in pages
                for session in page.get("Sessions", [])
                if session.get("Reason") == self._reason
            ]
            for session_id in found:
                self.ssm.terminate_session(SessionId=session_id)
            # One eventually-consistent listing cannot certify an unrecorded
            # StartSession result. Keep intent for explicit ownership recovery.
            raise TransportRetryError(
                "SSM session start outcome uncertain; retain recovery ownership"
            )
        if self.session_id is not None:
            try:
                if not self._terminated:
                    self.ssm.terminate_session(SessionId=self.session_id)
                    self._terminated = True
            except ClientError as error:
                failure = classify_aws(error)
            else:
                if self.record:
                    self.record(f"transport-stop-{self._receipt}.json", {"complete": True})
                self.session_id = None
                self._receipt = self._reason = None
        if self._temporary is not None:
            self._temporary.cleanup()
            self._temporary = None
        if failure is not None:
            raise failure
