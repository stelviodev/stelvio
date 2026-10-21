"""Nonroot SSH over SSM/EIC with persistent first-connect host-key trust."""

from __future__ import annotations

import json
import os
import re
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import suppress
from hashlib import sha256
from pathlib import Path
from threading import RLock, Thread
from typing import TYPE_CHECKING
from uuid import uuid4

from botocore.exceptions import ClientError

from stelvio.tunnel.bastion import SSH_USER
from stelvio.tunnel.credentials import AWS_IO
from stelvio.tunnel.host_trust import (
    HostIdentity,
    HostTrustError,
    fingerprint,
    locked_host_trust,
    parse_host_key,
)
from stelvio.tunnel.plugin import PluginAssetError, installed_plugin
from stelvio.tunnel.processes import identity, process_group_empty, unreaped_child
from stelvio.tunnel.socks import SocksConnector, TransportInterruptedError

if TYPE_CHECKING:
    from collections.abc import Callable
    from threading import Event
    from typing import BinaryIO

    import boto3
    from botocore.client import BaseClient

    from stelvio.tunnel.host_trust import LockedHostTrust
    from stelvio.tunnel.manifest import VpcNetwork

SSH_PORT = 22
ONLINE_SECONDS = 300
MAX_SSH_ERROR_BYTES = 65536
ZOMBIE = 5


class TransportRetryError(ConnectionError):
    """Transient AWS/SSM/SSH connectivity; retry with revalidation."""


class TransportAuthorizationError(RuntimeError):
    """Authorization or deployed ownership was revoked; do not reconnect."""


class TransportArtifactError(TransportAuthorizationError):
    """A required local transport artifact is missing, corrupt, or unsafe."""


class TransportCleanupError(RuntimeError):
    """Owned local or SSM transport cleanup could not be certified."""


class ServerIdentityError(TransportAuthorizationError):
    """The SSM target presented a different SSH key than its persistent pin."""


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
    if code in {
        "InvalidInstanceID.NotFound",
        "InvalidGroup.NotFound",
        "DBClusterNotFoundFault",
        "DBInstanceNotFoundFault",
        "DBSubnetGroupNotFoundFault",
    }:
        return TransportAuthorizationError("VPC owned resource is missing; redeploy before dev")
    return TransportRetryError("VPC transport AWS operation is temporarily unavailable")


def terminate_owned_session(
    ssm: BaseClient, session_id: str, instance_id: str, reason: str
) -> None:
    """Certify known-session termination, including an external interruption."""
    try:
        ssm.terminate_session(SessionId=session_id)
    except ClientError as error:
        if error.response["Error"]["Code"] not in {"ValidationException", "InvalidSessionId"}:
            raise
        pages = ssm.get_paginator("describe_sessions").paginate(
            State="History",
            Filters=[{"key": "SessionId", "value": session_id}],
            PaginationConfig={"MaxItems": 1000, "PageSize": 50},
        )
        matches = [
            item
            for page in pages
            for item in page.get("Sessions", [])
            if item.get("SessionId") == session_id
        ]
        if (
            len(matches) != 1
            or matches[0].get("Target") != instance_id
            or (matches[0].get("Reason") != reason or matches[0].get("Status") != "Terminated")
        ):
            raise TransportRetryError("SSM session termination is not certified") from None


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
        self._stderr_thread: Thread | None = None
        self._stderr = bytearray()
        self._stopping = RLock()
        self.session_id: str | None = None
        self._terminated = False
        self._session_start_attempted = False
        self._temporary: tempfile.TemporaryDirectory | None = None
        self.ssm = session.client("ssm", config=AWS_IO)
        self.ec2 = session.client("ec2", config=AWS_IO)

    def _pause(self, seconds: float) -> None:
        if self.stop.wait(seconds):
            raise TransportInterruptedError("VPC transport attempt cancelled")

    def _owned(self, tags: dict[str, str]) -> bool:
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

    def _bootstrap(self) -> None:
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

    def start(self) -> SocksConnector:
        if not os.geteuid() or os.getuid() != os.geteuid():
            raise TransportAuthorizationError(
                "VPC transport must run as the ordinary nonroot user"
            )
        self._pause(0)
        if (
            self.process is not None
            or self.session_id is not None
            or self._reason is not None
            or self._temporary is not None
            or self._stderr_thread is not None
        ):
            raise RuntimeError("Dispose the previous VPC transport before reconnecting")
        try:
            return self._start()
        except BaseException as error:
            try:
                self.close()
            except Exception as cleanup_error:
                raise TransportCleanupError(
                    "VPC transport startup failed and owned cleanup could not be certified"
                ) from cleanup_error
            if isinstance(error, HostTrustError):
                raise TransportAuthorizationError(str(error)) from error
            if isinstance(error, ClientError):
                raise classify_aws(error) from None
            raise

    def _start(self) -> SocksConnector:
        self._bootstrap()
        try:
            plugin = installed_plugin()
        except PluginAssetError as error:
            raise TransportArtifactError(str(error)) from None
        access = self.network.access
        target = HostIdentity(
            self.session.get_partition_for_region(self.network.region),
            self.network.account,
            self.network.region,
            access.instance_id,
        )
        with locked_host_trust(target, self.stop) as trust:
            if self.pin is not None and trust.key != self.pin:
                raise ServerIdentityError(
                    f"SSH host trust changed for {target.label}; "
                    "restart after explicit trust reset"
                )
            return self._connect(plugin, trust)

    def _connect(self, plugin: Path, trust: LockedHostTrust) -> SocksConnector:  # noqa: C901, PLR0915 - staged SSM and owned SSH startup
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
        hosts.write_text(f"{trust.identity.alias} {trust.key}\n" if trust.key else "")
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
        self._session_start_attempted = True
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
        environment["LC_ALL"] = "C"
        environment.update(
            STLV_SSM_SESSION=json.dumps(result),
            STLV_SSM_PLUGIN=str(plugin),
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
            "HostKeyAlias": trust.identity.alias,
            "StrictHostKeyChecking": "yes" if trust.key else "accept-new",
            "CheckHostIP": "no",
            "UpdateHostKeys": "no",
            "VerifyHostKeyDNS": "no",
            "KnownHostsCommand": "none",
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
        with self._stopping:
            # The watchdog sets stop before taking this lock. Either spawn and
            # birth registration finish before its group fence, or admission
            # refuses without creating a child after that fence has returned.
            self._pause(0)
            self.process = subprocess.Popen(  # noqa: S603 - fixed SSH and owned identity
                arguments,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            self._stderr.clear()
            self._stderr_thread = Thread(
                target=self._read_ssh_errors, args=(self.process.stderr,), daemon=True
            )
            self._stderr_thread.start()
            self._process_identity = identity(self.process.pid)
            if (
                self._process_identity is None
                or self._process_identity.group != self.process.pid
                or self._process_identity.uid != os.geteuid()
            ):
                raise TransportAuthorizationError("SSH process ownership could not be established")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            self._pause(0)
            if not self.healthy():
                self._raise_ssh_failure(trust)
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
                    entries = hosts.read_text().splitlines()
                    if len(entries) != 1 or not entries[0].startswith(trust.identity.alias + " "):
                        raise HostTrustError("SSH did not return one verified target host key")
                    key = parse_host_key(entries[0][len(trust.identity.alias) + 1 :])
                    self._pause(0)
                    trust.accept(key)
                    self.pin = key
                    return SocksConnector(port, self.network.cidrs, self.stop)
            self._pause(0.1)
        raise TransportRetryError("Pinned SSH authentication exceeded its budget")

    def _read_ssh_errors(self, stream: BinaryIO) -> None:
        with stream:
            while chunk := stream.read(4096):
                remaining = MAX_SSH_ERROR_BYTES - len(self._stderr)
                self._stderr.extend(chunk[: max(0, remaining)])

    def _raise_ssh_failure(self, trust: LockedHostTrust) -> None:
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=1)
        output = bytes(self._stderr)
        if b"REMOTE HOST IDENTIFICATION HAS CHANGED" in output or b"REVOKED HOST KEY" in output:
            observed = re.search(rb"SHA256:[A-Za-z0-9+/]{43}", output)
            expected = fingerprint(trust.key) if trust.key else "none"
            presented = observed.group().decode() if observed else "unavailable"
            raise ServerIdentityError(
                f"SSH host key changed for {trust.identity.label}: expected {expected}, "
                f"presented {presented}. Connection refused. Restore the original key or stop "
                "all tunnels and use stlv tunnel trust reset for this instance."
            )
        raise TransportRetryError(
            "SSH exited before authentication completed; check bastion startup and access"
        )

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
                exited = actual is None and unreaped_child(self.process.pid)
                if not exited and (
                    not actual
                    or (self._process_identity and not actual.same_process(self._process_identity))
                ):
                    raise RuntimeError("SSH process identity changed; cleanup refused")
                if actual and (actual.group != self.process.pid or actual.uid != os.geteuid()):
                    raise RuntimeError("SSH process group ownership changed; cleanup refused")
                with suppress(ProcessLookupError):
                    try:
                        os.killpg(self.process.pid, kind)
                    except PermissionError:
                        # Darwin returns EPERM for a group with only its zombie
                        # leader. Never swallow it while a live member remains.
                        if not unreaped_child(self.process.pid) or not process_group_empty(
                            self.process.pid
                        ):
                            raise
                if kind == signal.SIGTERM:
                    deadline = time.monotonic() + 2
                    while time.monotonic() < deadline:
                        actual = identity(self.process.pid)
                        if (actual and actual.status == ZOMBIE) or unreaped_child(
                            self.process.pid
                        ):
                            break
                        time.sleep(0.05)
            self._wait_for_group_shutdown()
            self.process.wait(timeout=3)
            self.process = None
            self._process_identity = None
        self._join_ssh_errors()

    def _wait_for_group_shutdown(self) -> None:
        # Keep the leader unreaped until every live member is gone. Reaping it
        # earlier releases the PGID fence while SIGKILL is still taking effect.
        deadline = time.monotonic() + 3
        try:
            while (remaining := deadline - time.monotonic()) > 0:
                if process_group_empty(self.process.pid, timeout=remaining):
                    return
                time.sleep(min(0.01, max(0, deadline - time.monotonic())))
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            raise TransportCleanupError(
                "SSH process group shutdown could not be verified; retain cleanup ownership"
            ) from error
        raise TransportCleanupError(
            "SSH process group shutdown exceeded its budget; retain cleanup ownership"
        )

    def _join_ssh_errors(self) -> None:
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=2)
            if self._stderr_thread.is_alive():
                raise TransportCleanupError("SSH error stream did not close after process cleanup")
            self._stderr_thread = None

    def _recover_session_id(self) -> str:
        pages = self.ssm.get_paginator("describe_sessions").paginate(
            State="Active",
            Filters=[{"key": "Target", "value": self.network.access.instance_id}],
            PaginationConfig={"MaxItems": 1000, "PageSize": 50},
        )
        found = [
            session
            for page in pages
            for session in page.get("Sessions", [])
            if session.get("Reason") == self._reason
        ]
        if (
            len(found) != 1
            or found[0].get("Target") != self.network.access.instance_id
            or not found[0].get("SessionId")
        ):
            raise TransportCleanupError(
                "SSM session start outcome is uncertain; retain recovery ownership"
            )
        return found[0]["SessionId"]

    def _terminate_session(self) -> None:
        if self.session_id is None:
            return
        if not self._terminated:
            terminate_owned_session(
                self.ssm, self.session_id, self.network.access.instance_id, self._reason
            )
            self._terminated = True
        if self.record:
            self.record(f"transport-stop-{self._receipt}.json", {"complete": True})
        self.session_id = None
        self._receipt = self._reason = None
        self._session_start_attempted = False

    def close(self) -> None:
        failures = []
        try:
            self.stop_local()
        except Exception as error:
            failures.append(error)
        if self.session_id is None and self._reason is not None:
            if not self._session_start_attempted:
                self._receipt = self._reason = None
            else:
                try:
                    self.session_id = self._recover_session_id()
                except Exception as error:
                    failures.append(error)
        try:
            self._terminate_session()
        except ClientError as error:
            failures.append(classify_aws(error))
        except Exception as error:
            failures.append(error)
        if self._temporary is not None and self.process is None:
            self._temporary.cleanup()
            self._temporary = None
        if failures:
            if len(failures) == 1 and isinstance(failures[0], TransportCleanupError):
                raise failures[0]
            raise TransportCleanupError(
                "VPC transport cleanup could not be certified"
            ) from failures[0]
