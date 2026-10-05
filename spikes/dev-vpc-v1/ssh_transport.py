"""Non-root P0 SSH transport; authenticated AWS bootstrap, pinned OpenSSH identity.

Owns ephemeral developer keys and SSH process groups until SIGINT/SIGTERM. Host
pins are public and retained to compare against AWS bootstrap on later starts.
No project handler is loaded, and no root operation or host networking occurs.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shlex
import shutil
import signal
import socket
import struct
import subprocess
import tempfile
import threading
import time
from contextlib import ExitStack, suppress
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

import boto3
from botocore.config import Config

if TYPE_CHECKING:
    from io import TextIOWrapper

fixture = import_module("spikes.dev-vpc-v1.aws_transport")
console = fixture.ownership.console
STOP = threading.Event()
AWS_CONFIG = Config(connect_timeout=5, read_timeout=20, retries={"max_attempts": 2})
SSH_PORT = 22
SSH_FAILURE = 255
ED25519_WIRE_LENGTH = 51
ED25519_WIRE_PREFIX = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20"
OCTETS = (254, 253)
SOCKS_REPLY_SIZE = 10


def validate_instance(session: boto3.Session, manifest: dict, index: int) -> None:
    node = manifest["bastions"][index]
    ec2 = session.client("ec2", config=AWS_CONFIG)
    instance = ec2.describe_instances(InstanceIds=[node["instance"]])["Reservations"][0][
        "Instances"
    ][0]
    tags = {tag["Key"]: tag["Value"] for tag in instance.get("Tags", [])}
    if (
        tags.get("stlv:proof-owner") != manifest["owner"]
        or instance["PrivateIpAddress"] != node["ip"]
    ):
        raise RuntimeError("Instance ownership/address mismatch; no transport started")
    groups = ec2.describe_security_groups(
        GroupIds=[g["GroupId"] for g in instance["SecurityGroups"]]
    )
    for group in groups["SecurityGroups"]:
        for rule in group.get("IpPermissions", []):
            if rule["IpProtocol"] == "-1" or (
                rule["IpProtocol"] in ("tcp", "6")
                and rule["FromPort"] <= SSH_PORT <= rule["ToPort"]
            ):
                raise RuntimeError("Bastion admits inbound SSH; refusing proof")


def parse_identity(output: str, index: int) -> str:
    try:
        key, marker = output.strip().splitlines()
        key_type, encoded, *_ = key.split()
    except ValueError as error:
        raise RuntimeError("Host identity bootstrap/ready marker malformed") from error
    if marker != f"proof-ready vpc-{index + 1}" or key_type != "ssh-ed25519":
        raise RuntimeError("Expected ready bastion with Ed25519 host key")
    decoded = base64.b64decode(encoded, validate=True)
    if len(decoded) != ED25519_WIRE_LENGTH or not decoded.startswith(ED25519_WIRE_PREFIX):
        raise RuntimeError("Malformed Ed25519 server key")
    return f"{key_type} {encoded}"


def bootstrap(session: boto3.Session, manifest: dict, index: int) -> str:
    validate_instance(session, manifest, index)
    node = manifest["bastions"][index]
    ssm = session.client("ssm", config=AWS_CONFIG)
    document = ssm.get_document(Name=manifest["identity_document"], DocumentVersion="$DEFAULT")
    doc_tags = ssm.list_tags_for_resource(
        ResourceType="Document", ResourceId=manifest["identity_document"]
    )["TagList"]
    if (
        json.loads(document["Content"]) != fixture.identity_document()
        or {tag["Key"]: tag["Value"] for tag in doc_tags}.get("stlv:proof-owner")
        != manifest["owner"]
    ):
        raise RuntimeError("Identity document ownership/content mismatch; bootstrap refused")
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline and not STOP.is_set():
        info = ssm.describe_instance_information(
            Filters=[{"Key": "InstanceIds", "Values": [node["instance"]]}]
        )["InstanceInformationList"]
        if info and info[0]["PingStatus"] == "Online":
            break
        STOP.wait(3)
    else:
        raise RuntimeError("SSM agent did not become online within 300s")
    command = ssm.send_command(
        InstanceIds=[node["instance"]],
        DocumentName=manifest["identity_document"],
        TimeoutSeconds=60,
    )["Command"]["CommandId"]
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline and not STOP.is_set():
        try:
            result = ssm.get_command_invocation(CommandId=command, InstanceId=node["instance"])
        except ssm.exceptions.InvocationDoesNotExist:
            STOP.wait(2)
            continue
        if result["Status"] == "Success":
            return parse_identity(result["StandardOutputContent"], index)
        if result["Status"] not in ("Pending", "InProgress", "Delayed"):
            raise RuntimeError(f"Host identity bootstrap failed: {result['Status']}")
        STOP.wait(2)
    raise RuntimeError("Host identity bootstrap exceeded 120s")


def ssh_command(manifest: dict, node: dict, key: Path, hosts: Path) -> list[str]:
    aws_cli = shutil.which("aws")
    if not aws_cli or not shutil.which("session-manager-plugin"):
        raise RuntimeError("Install AWS CLI and session-manager-plugin before this proof")
    proxy = [aws_cli, "--region", manifest["region"]]
    if manifest["profile"]:
        proxy += ["--profile", manifest["profile"]]
    proxy += [
        "ssm",
        "start-session",
        "--target",
        node["instance"],
        "--document-name",
        "AWS-StartSSHSession",
        "--parameters",
        "portNumber=22",
    ]
    options = {
        "ProxyCommand": shlex.join(proxy),
        "HostKeyAlias": node["instance"],
        "StrictHostKeyChecking": "yes",
        "UserKnownHostsFile": str(hosts),
        "GlobalKnownHostsFile": "/dev/null",
        "IdentitiesOnly": "yes",
        "IdentityAgent": "none",
        "ForwardAgent": "no",
        "PermitLocalCommand": "no",
        "RequestTTY": "no",
        "BatchMode": "yes",
        "ExitOnForwardFailure": "yes",
        "ConnectionAttempts": "1",
        "ConnectTimeout": "15",
        "ServerAliveInterval": "10",
        "ServerAliveCountMax": "2",
        "PreferredAuthentications": "publickey",
        "HostKeyAlgorithms": "ssh-ed25519",
    }
    command = ["/usr/bin/ssh", "-F", "/dev/null", "-N", "-D", node["socks"], "-i", str(key)]
    for name, value in options.items():
        command += ["-o", f"{name}={value}"]
    return [*command, f"{fixture.USER}@{node['instance']}"]


def group_is_running(group: int) -> bool:
    # Omit argv: the SSM plugin receives session tokens on its command line.
    listing = subprocess.check_output(
        ["/bin/ps", "-axo", "pgid=,state="], text=True, timeout=5
    )
    for line in listing.splitlines():
        pgid, state = line.split()
        if int(pgid) == group and not state.startswith("Z"):
            return True
    return False


def signal_group(group: int, sig: signal.Signals) -> None:
    try:
        os.killpg(group, sig)
    except ProcessLookupError:
        pass
    except PermissionError:
        # Darwin can return EPERM for a group containing only orphaned zombies.
        # A live member still makes this a cleanup failure, never a silent leak.
        if group_is_running(group):
            raise


def terminate(process: subprocess.Popen) -> None:
    # The proxy AWS CLI/plugin live in this new group; stop them even if ssh exited.
    signal_group(process.pid, signal.SIGTERM)
    with suppress(subprocess.TimeoutExpired):
        process.wait(timeout=5)
    # Killing ssh can leave its proxy alive. Fence the whole original group.
    signal_group(process.pid, signal.SIGKILL)
    process.wait(timeout=5)
    deadline = time.monotonic() + 5
    while group_is_running(process.pid):
        if time.monotonic() >= deadline:
            raise RuntimeError("SSH proxy process group remains after cleanup")
        time.sleep(0.05)


def send_key(session: boto3.Session, node: dict, public: Path) -> None:
    response = session.client("ec2-instance-connect", config=AWS_CONFIG).send_ssh_public_key(
        InstanceId=node["instance"],
        AvailabilityZone=node["az"],
        InstanceOSUser=fixture.USER,
        SSHPublicKey=public.read_text().strip(),
    )
    if not response["Success"]:
        raise RuntimeError("EC2 Instance Connect rejected ephemeral client key")


def reject_bad_pin(
    command: list[str], session: boto3.Session, node: dict, public: Path, log: TextIOWrapper
) -> None:
    send_key(session, node, public)
    # Built from the fixed OpenSSH executable and quoted, AWS-validated identity.
    with subprocess.Popen(  # noqa: S603
        command, stdout=log, stderr=log, start_new_session=True
    ) as process:
        try:
            result = process.wait(timeout=30)
        except subprocess.TimeoutExpired as error:
            raise RuntimeError(
                "SSH failed to reject incorrect host identity within 30s"
            ) from error
        finally:
            terminate(process)
    log.flush()
    if result != SSH_FAILURE or "Host key verification failed." not in Path(log.name).read_text():
        raise RuntimeError("Incorrect SSH host identity was not rejected as expected")


def wait_socks(process: subprocess.Popen, node: dict, index: int) -> None:
    host, port = node["socks"].split(":")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not STOP.is_set():
        if process.poll() is not None:
            raise RuntimeError("SSH exited before its authenticated SOCKS listener was ready")
        try:
            with socket.create_connection((host, int(port)), timeout=1) as connection:
                connection.sendall(b"\x05\x01\x00")
                with connection.makefile("rb") as stream:
                    if stream.read(2) != b"\x05\x00":
                        raise RuntimeError("SOCKS authentication negotiation rejected")
                    connection.sendall(
                        b"\x05\x01\x00\x01"
                        + socket.inet_aton(node["ip"])
                        + struct.pack("!H", 8080)
                    )
                    reply = stream.read(SOCKS_REPLY_SIZE)
                    if reply[:4] != b"\x05\x00\x00\x01":
                        raise RuntimeError("SOCKS path could not connect to private echo target")
                    marker = f"vpc-{index + 1}\n".encode()
                    if stream.read(len(marker)) != marker or process.poll() is not None:
                        raise RuntimeError(
                            "Expected private echo marker/new SSH process not ready"
                        )
                    return
        except OSError:
            pass
        STOP.wait(0.25)
    raise RuntimeError("Authenticated SOCKS listener did not become ready within 30s")


def run(manifest: dict, out: Path) -> None:
    if str(UUID(manifest["owner"])) != manifest["owner"] or len(manifest["bastions"]) != len(
        OCTETS
    ):
        raise RuntimeError("Require one canonical proof owner and exactly two bastions")
    for index, (node, octet) in enumerate(zip(manifest["bastions"], OCTETS, strict=True)):
        if (
            node["socks"] != f"127.0.0.1:{10880 + index}"
            or node["cidr"] != f"10.{octet}.0.0/16"
            or node["ip"] != f"10.{octet}.0.10"
        ):
            raise RuntimeError("Manifest networking is outside the fixed P0 profile")
        # Refuse an earlier listener rather than mistaking it for this SSH child.
        with socket.socket() as check:
            check.bind(("127.0.0.1", 10880 + index))
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    if out.is_symlink() or out.stat().st_uid != os.getuid() or out.stat().st_mode & 0o077:
        raise RuntimeError("Transport output directory must be caller-owned and mode 0700")
    session = boto3.Session(profile_name=manifest["profile"], region_name=manifest["region"])
    pins = {
        node["instance"]: bootstrap(session, manifest, i)
        for i, node in enumerate(manifest["bastions"])
    }
    pin_file = out / "pins.json"
    if pin_file.exists() and json.loads(pin_file.read_text()) != pins:
        raise RuntimeError("AWS bootstrap server identity changed; reconnect refused")
    pin_file.write_text(json.dumps(pins, indent=2))
    with ExitStack() as scope:
        temporary = Path(scope.enter_context(tempfile.TemporaryDirectory(prefix="keys-", dir=out)))
        key = temporary / "client"
        wrong = temporary / "wrong"
        for path in (key, wrong):
            # Fixed executable; generated paths are inside our mode-0700 temp dir.
            subprocess.run(  # noqa: S603
                ["/usr/bin/ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(path)],
                check=True,
                capture_output=True,
                timeout=15,
            )
        public = key.with_suffix(".pub")
        processes = []
        for index, node in enumerate(manifest["bastions"]):
            bad_hosts = temporary / f"bad-hosts-{index}"
            bad_hosts.write_text(f"{node['instance']} {wrong.with_suffix('.pub').read_text()}")
            bad_log = scope.enter_context((out / f"bad-pin-{index}.log").open("w"))
            reject_bad_pin(
                ssh_command(manifest, node, key, bad_hosts), session, node, public, bad_log
            )
            hosts = temporary / f"hosts-{index}"
            hosts.write_text(f"{node['instance']} {pins[node['instance']]}\n")
            send_key(session, node, public)
            log = scope.enter_context((out / f"ssh-{index}.log").open("w"))
            # Fixed OpenSSH executable; no shell, user SSH config or agent.
            process = subprocess.Popen(  # noqa: S603
                ssh_command(manifest, node, key, hosts),
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
            scope.callback(terminate, process)
            wait_socks(process, node, index)
            processes.append(process)
        (out / "forwarder.json").write_text(
            json.dumps(
                [{"cidr": node["cidr"], "socks": node["socks"]} for node in manifest["bastions"]]
            )
        )
        console.print("READY: two authenticated SSH-over-SSM SOCKS paths; incorrect pins rejected")
        while not STOP.wait(1):
            if any(process.poll() is not None for process in processes):
                raise RuntimeError("SSH path exited; proof transport stopping")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if os.geteuid() == 0:
        parser.error("SSH/AWS transport must never run as root")
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: STOP.set())
    run(json.loads(args.manifest.read_text()), args.out)


if __name__ == "__main__":
    main()
