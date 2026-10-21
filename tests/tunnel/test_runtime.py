"""Public launcher runs real isolated child/runtime/forwarder, with local boundary fixtures."""

import json
import os
import socket
import struct
import subprocess
import sys
import time
from dataclasses import replace
from ipaddress import IPv4Network
from threading import Lock

from pytest import mark, raises

from stelvio.tunnel.credentials import AwsExecutionContext
from stelvio.tunnel.manifest import (
    AccessDescriptor,
    EndpointNetwork,
    NetworkManifest,
    SessionDescription,
    SubnetNetwork,
    VpcNetwork,
)
from stelvio.tunnel.policy import BastionPolicy
from stelvio.tunnel.runtime import NetworkRuntime
from stelvio.tunnel.runtime_child import (
    _has_unresolved_inventory,
    _owner_status,
    _resolve_independent_inventory,
)

IMAGE_REASON = (
    "Opt-in real Python/Traforo interoperability; pre-download a release or build locally"
)


def _inventory_network(identity, cidr):
    cidr = IPv4Network(cidr)
    return VpcNetwork(
        identity=identity,
        account="123456789012",
        region="us-east-1",
        vpc_id=f"vpc-{identity}",
        provider="provider",
        cidrs=(cidr,),
        policy=BastionPolicy.PERSISTENT,
        dns_domains=(),
        public_subnets=(
            SubnetNetwork(
                f"subnet-{identity}", "us-east-1a", IPv4Network((cidr.network_address, 24))
            ),
        ),
        private_subnets=(
            SubnetNetwork(
                f"subnet-{identity}-private",
                "us-east-1a",
                IPv4Network((cidr.network_address + 256, 24)),
            ),
        ),
        isolated_subnets=(
            SubnetNetwork(
                f"subnet-{identity}-isolated",
                "us-east-1a",
                IPv4Network((cidr.network_address + 512, 24)),
            ),
        ),
    )


def test_inventory_failure_isolated_and_known_overlaps_remain_fenced(monkeypatch):
    from stelvio.tunnel import runtime_child

    failed = _inventory_network("failed", "10.1.0.0/16")
    overlaps = _inventory_network("overlap", "10.2.0.0/16")
    healthy = _inventory_network("healthy", "10.3.0.0/16")
    unrelated = _inventory_network("unrelated", "10.4.0.0/16")
    manifest = NetworkManifest(
        (failed, overlaps, healthy, unrelated),
        (),
        (
            EndpointNetwork("endpoint-failed", "endpoint-failed", ("failed",)),
            EndpointNetwork("endpoint-overlap", "endpoint-overlap", ("overlap",)),
            EndpointNetwork("endpoint-healthy", "endpoint-healthy", ("healthy",)),
            EndpointNetwork("endpoint-unrelated", "endpoint-unrelated", ("unrelated",)),
        ),
    )

    def resolve(network, _client):
        if network.identity == "failed":
            raise RuntimeError("isolated AWS inventory failure")
        if network.identity == "overlap":
            return replace(network, cidrs=(IPv4Network("10.3.1.0/24"),))
        return network

    monkeypatch.setattr(runtime_child, "resolve_vpc_network", resolve)
    resolved, failures = _resolve_independent_inventory(manifest, lambda _: object())
    assert failures == {
        "failed": "network-inventory",
        "overlap": "cidr-overlap",
        "healthy": "cidr-overlap",
    }
    assert "unrelated" not in failures
    assert tuple(vpc.identity for vpc in resolved.enabled_vpcs) == (
        "failed",
        "overlap",
        "healthy",
        "unrelated",
    )


def test_unrelated_vpc_inventory_failure_does_not_block_healthy_vpc(monkeypatch):
    from stelvio.tunnel import runtime_child

    failed = _inventory_network("failed", "10.1.0.0/16")
    healthy = _inventory_network("healthy", "10.2.0.0/16")
    manifest = NetworkManifest(
        (failed, healthy),
        (),
        (
            EndpointNetwork("endpoint-failed", "endpoint-failed", ("failed",)),
            EndpointNetwork("endpoint-healthy", "endpoint-healthy", ("healthy",)),
        ),
    )

    def resolve(network, _client):
        if network.identity == "failed":
            raise RuntimeError("isolated AWS inventory failure")
        return network

    monkeypatch.setattr(runtime_child, "resolve_vpc_network", resolve)
    _, failures = _resolve_independent_inventory(manifest, lambda _: object())
    assert failures == {"failed": "network-inventory"}


def test_healthy_candidate_waits_for_retryable_inventory_or_terminal_release():
    healthy = _inventory_network("healthy", "10.1.0.0/16")
    failed = _inventory_network("failed", "10.2.0.0/16")
    pending = {"failed"}
    candidates = {"healthy": healthy}

    assert _has_unresolved_inventory(pending, candidates)

    candidates["failed"] = failed
    assert not _has_unresolved_inventory(pending, candidates)

    candidates.pop("failed")
    pending.clear()  # Terminal inventory failure releases the shared safety gate.
    assert not _has_unresolved_inventory(pending, candidates)


IPC_CHILD = r"""
import json,os,socket,struct,sys,threading
control=socket.socket(fileno=int(sys.argv[1]))
lease=int(sys.argv[2])
marker=os.environ["STLV_TEST_RUNTIME_MARKER"]
def exact(size):
    data=bytearray()
    while len(data)<size:
        part=control.recv(size-len(data))
        if not part: raise EOFError()
        data.extend(part)
    return bytes(data)
def receive():
    size=struct.unpack("!I",exact(4))[0]
    return json.loads(exact(size))
def send(value):
    wire=json.dumps(value,separators=(",",":")).encode()
    control.sendall(struct.pack("!I",len(wire))+wire)
startup=receive()
assert startup["session"]["app"]=="isolated-fixture"
assert os.environ["AWS_ACCESS_KEY_ID"]=="CAPTURED_STARTUP_KEY"
def owner_watch():
    os.read(lease,1)
    with open(marker,"a") as output: output.write("lease-eof\n")
    try: control.shutdown(socket.SHUT_RDWR)
    except OSError: pass
threading.Thread(target=owner_watch,daemon=True).start()
send({"runtime":"started"})
while True:
    try: command=receive()
    except (EOFError,OSError): break
    if command=={"operation":"status"}:
        send({"vpcs":[]})
    elif command=={"operation":"stop"} and os.environ.get("STLV_TEST_IGNORE_STOP")!="1":
        break
with open(marker,"a") as output: output.write("control-exit\n")
if os.environ.get("STLV_TEST_RUNTIME_FAILURE")=="1": sys.exit(70)
"""

WORKER = r"""
import os,socket,sys
from types import SimpleNamespace
from threading import Event
from botocore.stub import Stubber
from stelvio.tunnel import credentials
from stelvio.tunnel import runtime_child as child
from stelvio.tunnel.supervisor import VpcStatus
real_factory=credentials.boto3.Session
class Ec2:
    meta=SimpleNamespace(region_name="us-east-1")
    def describe_vpcs(self,**kwargs):
        return {"Vpcs":[{"VpcId":"vpc-12345678","OwnerId":"123456789012",
            "State":"available","CidrBlock":"10.254.0.0/16"}]}
    def describe_vpc_attribute(self,**kwargs):
        field=("EnableDnsSupport" if kwargs["Attribute"]=="enableDnsSupport"
               else "EnableDnsHostnames")
        return {"VpcId":"vpc-12345678",field:{"Value":True}}
    def describe_subnets(self,**kwargs):
        expected={"subnet-12345678":"10.254.1.0/24",
                  "subnet-private":"10.254.2.0/24", "subnet-isolated":"10.254.3.0/24"}
        assert set(kwargs["SubnetIds"])==set(expected)
        return {"Subnets":[{"SubnetId":identity,"VpcId":"vpc-12345678",
            "OwnerId":"123456789012","AvailabilityZone":"us-east-1a",
            "CidrBlock":cidr,"State":"available"} for identity,cidr in expected.items()]}
def factory(**kwargs):
    session=real_factory(**kwargs)
    assert session.get_credentials().get_frozen_credentials().access_key=="PUBLIC_STARTUP_KEY"
    sts=session.client("sts",config=credentials.AWS_IO)
    stub=Stubber(sts)
    stub.add_response("get_caller_identity",{
        "Account":"123456789012","UserId":"fixture",
        "Arn":"arn:aws:iam::123456789012:user/fixture"},{})
    stub.activate()
    original=session.client
    def client(service,**config):
        if service=="sts": return sts
        if service=="ec2": return Ec2()
        return original(service,**config)
    session.client=client
    return session
credentials.boto3.Session=factory
class Helper:
    def __init__(self,**kwargs): pass
    def status(self): return SimpleNamespace(owned=False,uncertain=False,units=())
    def acquire(self,session):
        carrier,peer=socket.socketpair(socket.AF_UNIX,socket.SOCK_DGRAM)
        lease=SimpleNamespace(carrier=carrier,connection=peer,closed=False)
        def close(): carrier.close();peer.close()
        lease.close=close;lease.revoke=close
        lease.status=lambda:SimpleNamespace(owned=True,uncertain=False,units=())
        return lease
class Worker:
    def __init__(self,description,network,*args,**kwargs):
        self.network=network;self.stop=Event()
        self.status=VpcStatus(network.identity,"ready",1)
        self.transport=SimpleNamespace(stop_local=lambda:None)
    def start(self): pass
    def close(self):
        marker=os.environ.get("STLV_TEST_WORKER_CLOSE_MARKER")
        if marker:
            with open(marker,"w") as output: output.write("closed")
        if sys.argv[3]=="failure": raise RuntimeError("fixture cleanup incomplete")
from contextlib import contextmanager
from pathlib import Path
from stelvio.tunnel.assets import _ready, _architecture, select_traforo, DEVELOPMENT_BINARY_ENV
# Explicit opt-in image: hermetic HOME must not cause downloads from a runtime test.
@contextmanager
def interop_image():
    image=Path(os.environ["STLV_TEST_TRAFORO_IMAGE"])
    if os.environ.get(DEVELOPMENT_BINARY_ENV):
        assert select_traforo().local_path == image
    else:
        assert _ready(image, _architecture())
    yield image
child.packaged_forwarder=interop_image
child.NativeHelper=Helper
child.VpcWorker=Worker
with socket.socket(fileno=int(sys.argv[1])) as control:
    try: child.run(control,int(sys.argv[2]))
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(70)
"""


@mark.skipif(not os.environ.get("STLV_TEST_TRAFORO_IMAGE"), reason=IMAGE_REASON)
@mark.parametrize(
    ("cleanup_failure", "owner_death"),
    [(False, False), (True, False), (False, True)],
)
def test_actual_isolated_runtime_uses_captured_environment_and_reports_cleanup_failure(  # noqa: PLR0915 - full process lifecycle
    monkeypatch, cleanup_failure, owner_death, tmp_path
):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "PUBLIC_STARTUP_KEY")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "public-fixture-secret")
    close_marker = tmp_path / "worker-closed"
    monkeypatch.setenv("STLV_TEST_WORKER_CLOSE_MARKER", str(close_marker))
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_PROFILE", raising=False)
    context = AwsExecutionContext.capture(
        provider="provider", account="123456789012", region="us-east-1"
    )
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "HANDLER_KEY")
    network = VpcNetwork(
        identity="vpc-owner",
        account="123456789012",
        region="us-east-1",
        vpc_id="vpc-12345678",
        provider="provider",
        cidrs=(IPv4Network("10.254.0.0/16"),),
        policy=BastionPolicy.PERSISTENT,
        dns_domains=(),
        public_subnets=(
            SubnetNetwork("subnet-12345678", "us-east-1a", IPv4Network("10.254.1.0/24")),
        ),
        private_subnets=(
            SubnetNetwork("subnet-private", "us-east-1a", IPv4Network("10.254.2.0/24")),
        ),
        isolated_subnets=(
            SubnetNetwork("subnet-isolated", "us-east-1a", IPv4Network("10.254.3.0/24")),
        ),
        access=AccessDescriptor("i-12345678", "sg-12345678", "us-east-1a", "vpc-owner"),
    )
    description = SessionDescription(
        "11111111-1111-4111-8111-111111111111",
        "app",
        "dev",
        os.geteuid(),
        NetworkManifest((network,), (), (EndpointNetwork("fn", "fn", ("vpc-owner",)),)),
    )
    child_log = tmp_path / "runtime-child.log"
    real_popen = subprocess.Popen

    def fixture_child(arguments, **options):
        assert arguments[:4] == [sys.executable, "-I", "-m", "stelvio.tunnel.runtime_child"]
        with child_log.open("wb") as output:
            return real_popen(
                [
                    sys.executable,
                    "-I",
                    "-c",
                    WORKER,
                    *arguments[4:],
                    "failure" if cleanup_failure else "success",
                ],
                **(options | {"stderr": output}),
            )

    monkeypatch.setattr("stelvio.tunnel.runtime.subprocess.Popen", fixture_child)
    try:
        runtime = NetworkRuntime(description, (context,))
    except RuntimeError as error:
        raise AssertionError(child_log.read_text()) from error
    try:
        deadline = time.monotonic() + 5
        while True:
            status = runtime.status()
            if status[0]["state"] == "ready":
                break
            assert time.monotonic() < deadline, status
        assert status == [
            {
                "identity": "vpc-owner",
                "host_unit": None,
                "state": "ready",
                "attempt": 1,
                "cause": None,
                "dns_retained": False,
                "resource_probe": "unverified",
                "phase": "pending",
                "detail": None,
            }
        ]
        if owner_death:
            # Closing the parent's owner lease simulates abrupt owner loss while
            # leaving the control socket open. The orphan watchdog must wake the
            # runtime finalizer instead of exiting before Worker.close().
            os.close(runtime._lease)
            runtime._lease = -1
            runtime.process.wait(timeout=10)
            assert runtime.process.returncode == 0
            assert close_marker.read_text() == "closed"
            runtime._control.close()
            runtime._closed = True
        elif cleanup_failure:
            with raises(RuntimeError, match="cleanup incomplete; run stlv tunnel cleanup"):
                runtime.close()
            assert runtime.process.returncode == 70
        else:
            runtime.close()
            assert runtime.process.returncode == 0
        runtime.close()
    finally:
        if not runtime._closed:
            if cleanup_failure:
                with raises(RuntimeError, match="cleanup incomplete"):
                    runtime.close()
            else:
                runtime.close()
        assert runtime.process.returncode is not None


@mark.parametrize("cleanup_failure", [False, True])
def test_runtime_is_a_real_subprocess_with_captured_environment_and_bounded_shutdown(
    monkeypatch, cleanup_failure, tmp_path
):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "CAPTURED_STARTUP_KEY")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "captured-fixture-secret")
    monkeypatch.setenv("STLV_TEST_RUNTIME_MARKER", str(tmp_path / "runtime-events"))
    monkeypatch.setenv("STLV_TEST_RUNTIME_FAILURE", "1" if cleanup_failure else "0")
    monkeypatch.delenv("STLV_TEST_IGNORE_STOP", raising=False)
    context = AwsExecutionContext.capture(
        provider="provider", account="123456789012", region="us-east-1"
    )
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "MUTATED_HANDLER_KEY")
    description = SessionDescription(
        "11111111-1111-4111-8111-111111111111",
        "isolated-fixture",
        "dev",
        os.geteuid(),
        NetworkManifest((), (), ()),
    )
    real_popen = subprocess.Popen

    def fixture_child(arguments, **options):
        return real_popen([sys.executable, "-I", "-c", IPC_CHILD, *arguments[4:]], **options)

    monkeypatch.setattr("stelvio.tunnel.runtime.subprocess.Popen", fixture_child)
    runtime = NetworkRuntime(description, (context,))
    assert runtime.status() == []
    if cleanup_failure:
        with raises(RuntimeError, match="cleanup incomplete"):
            runtime.close()
    else:
        runtime.close()
        assert runtime.process.returncode == 0
    runtime.close()
    assert "control-exit" in (tmp_path / "runtime-events").read_text()


def test_runtime_close_releases_lease_after_shutdown_deadline(monkeypatch, tmp_path):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "CAPTURED_STARTUP_KEY")
    monkeypatch.setenv("STLV_TEST_RUNTIME_MARKER", str(tmp_path / "lease-events"))
    monkeypatch.setenv("STLV_TEST_IGNORE_STOP", "1")
    context = AwsExecutionContext.capture(
        provider="provider", account="123456789012", region="us-east-1"
    )
    description = SessionDescription(
        "11111111-1111-4111-8111-111111111111",
        "isolated-fixture",
        "dev",
        os.geteuid(),
        NetworkManifest((), (), ()),
    )
    monkeypatch.setattr("stelvio.tunnel.runtime.STOP_TIMEOUT", 0.01)
    real_popen = subprocess.Popen
    monkeypatch.setattr(
        "stelvio.tunnel.runtime.subprocess.Popen",
        lambda arguments, **options: real_popen(
            [sys.executable, "-I", "-c", IPC_CHILD, *arguments[4:]], **options
        ),
    )
    runtime = NetworkRuntime(description, (context,))
    runtime.close()
    assert runtime.process.returncode == 0
    events = (tmp_path / "lease-events").read_text()
    assert "lease-eof" in events
    assert "control-exit" in events


def test_runtime_status_ipc_is_bounded_and_rejects_malformed_snapshots():
    from stelvio.tunnel import runtime as runtime_module

    control, peer = socket.socketpair()
    value = object.__new__(NetworkRuntime)
    value._control = control
    value._lock = Lock()
    value._closed = False
    started = time.monotonic()
    try:
        with raises(TimeoutError):
            value.status()
        assert time.monotonic() - started < runtime_module.STATUS_TIMEOUT + 1
    finally:
        control.close()
        peer.close()

    control, peer = socket.socketpair()
    value._control = control
    value._closed = False
    wire = json.dumps({"unexpected": []}).encode()
    peer.sendall(struct.pack("!I", len(wire)) + wire)
    try:
        with raises(ValueError, match="status snapshot"):
            value.status()
    finally:
        control.close()
        peer.close()


@mark.parametrize("changed", [False, True])
def test_restarted_process_cannot_replace_a_persisted_host_key(tmp_path, monkeypatch, changed):
    import base64
    from threading import Event

    from stelvio.tunnel import host_trust
    from stelvio.tunnel.host_trust import HOST_KEY_PREFIX, HostIdentity, locked_host_trust

    cache = tmp_path.resolve() / "cache"
    cache.mkdir(mode=0o700)
    monkeypatch.setattr(host_trust, "get_bin_path", lambda: cache)
    if sys.platform != "darwin":
        monkeypatch.setattr("stelvio.tunnel.assets._no_acl", lambda *args: None)
    target = HostIdentity("aws", "123456789012", "us-east-1", "i-12345678")
    key = "ssh-ed25519 " + base64.b64encode(HOST_KEY_PREFIX + bytes(range(32))).decode()
    other = (
        "ssh-ed25519 " + base64.b64encode(HOST_KEY_PREFIX + bytes(reversed(range(32)))).decode()
    )
    with locked_host_trust(target, Event()) as entry:
        entry.accept(key)
    script = """import sys
from pathlib import Path
from threading import Event
from stelvio.tunnel import host_trust, assets
from stelvio.tunnel.host_trust import HostIdentity, HostTrustError, locked_host_trust
host_trust.get_bin_path=lambda:Path(sys.argv[1])
if sys.platform!='darwin': assets._no_acl=lambda *args:None
try:
    target=HostIdentity('aws','123456789012','us-east-1','i-12345678')
    with locked_host_trust(target,Event()) as entry:
        entry.accept(sys.argv[2])
except HostTrustError:
    sys.exit(17)
"""
    result = subprocess.run(  # noqa: S603 - fixed isolated test process and public host key
        [sys.executable, "-I", "-c", script, str(cache), other if changed else key],
        cwd=tmp_path,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == (17 if changed else 0), result.stderr.decode()
    assert result.stdout == b""
    with locked_host_trust(target, Event()) as entry:
        assert entry.key == key


@mark.skipif(not os.environ.get("STLV_TEST_TRAFORO_IMAGE"), reason=IMAGE_REASON)
@mark.parametrize("custom_domains", [(), ("custom.example.test",)])
def test_actual_forwarder_exact_hostnames_and_probe_scope(custom_domains):
    from contextlib import ExitStack
    from pathlib import Path
    from uuid import uuid4

    from stelvio.tunnel.forwarding import Forwarding
    from tests.integration.tunnel_dns_fixture import (
        SocksDnsFixture,
        dns_exchange,
        dns_query_wire,
        dns_response,
        native_network,
    )

    probe = f"probe-{uuid4().hex}-00000019.stelvio.test"
    host = "db.exact.example.test"
    network = native_network(254, *custom_domains)
    with ExitStack() as scope:
        carrier, peer = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
        scope.enter_context(carrier)
        scope.enter_context(peer)
        proxy = SocksDnsFixture()
        scope.callback(proxy.close)
        forwarding = Forwarding(Path(os.environ["STLV_TEST_TRAFORO_IMAGE"]), carrier)
        scope.callback(forwarding.close)
        forwarding.add("00000019", 1, 1, network, proxy.port)
        port = forwarding.configure_dns(
            "00000019", 1, 1, 1, network, (host, probe), probe_domain=probe
        )
        name = f"stlv-{uuid4().hex}.{probe}"
        forwarding.dns("arm", "00000019", 1, 1, 1, name=name)
        wire = dns_query_wire(name)
        assert dns_response(wire, dns_exchange("127.0.0.1", port, wire, "udp"))[0] == 0
        assert forwarding.dns("check", "00000019", 1, 1, 1, name=name)["observed"]
        forwarding.dns("release", "00000019", 1, 1, 1, name=name)
        for name in ("unowned." + host, "other.stelvio.test"):
            wire = dns_query_wire(name)
            assert dns_response(wire, dns_exchange("127.0.0.1", port, wire, "tcp"))[0] == 5


@mark.parametrize(("generation", "state"), [(1, "ACTIVE"), (2, "RETAINED"), (3, "ACTIVE")])
def test_owner_status_exports_observed_generation_once(generation, state):
    from hashlib import sha256
    from types import SimpleNamespace
    from unittest.mock import Mock

    from stelvio.tunnel.helper_client import HelperUnitStatus
    from stelvio.tunnel.supervisor import VpcStatus

    identity = "first-vpc"
    unit = SimpleNamespace(
        unit=sha256(identity.encode()).hexdigest()[:8],
        generation=generation,
        status=HelperUnitStatus[state],
    )
    lease = Mock()
    lease.status.return_value = SimpleNamespace(owned=True, uncertain=False, units=(unit,))
    result = _owner_status(lease, [VpcStatus(identity), VpcStatus("second-vpc")])
    assert result[0]["host_unit"] == {
        "unit": unit.unit,
        "generation": generation,
        "status": state.lower(),
    }
    assert result[1]["host_unit"] is None
    lease.status.assert_called_once_with()
    assert set(result[0]) == {
        "identity",
        "state",
        "attempt",
        "cause",
        "dns_retained",
        "resource_probe",
        "phase",
        "detail",
        "host_unit",
    }


@mark.parametrize(("owned", "uncertain"), [(False, False), (True, True)])
def test_owner_status_does_not_certify_uncertain_ownership(owned, uncertain):
    from types import SimpleNamespace
    from unittest.mock import Mock

    lease = Mock()
    lease.status.return_value = SimpleNamespace(owned=owned, uncertain=uncertain)
    with raises(RuntimeError, match="ownership"):
        _owner_status(lease, [])


def test_owner_status_propagates_closed_lease():
    from unittest.mock import Mock

    lease = Mock()
    lease.status.side_effect = RuntimeError("closed lease")
    with raises(RuntimeError, match="closed lease"):
        _owner_status(lease, [])


def test_owner_snapshot_keeps_other_vpc_independent_during_retirement():
    from hashlib import sha256
    from unittest.mock import Mock

    from stelvio.tunnel.helper_client import (
        HelperOwnershipState,
        HelperUnitState,
        HelperUnitStatus,
    )
    from stelvio.tunnel.supervisor import VpcStatus

    first = sha256(b"first-vpc").hexdigest()[:8]
    second = sha256(b"second-vpc").hexdigest()[:8]
    lease = Mock()
    snapshots = []
    for generation, state in [(1, "ACTIVE"), (2, "RETAINED"), (3, "ACTIVE")]:
        lease.status.return_value = HelperOwnershipState(
            owned=True,
            uncertain=False,
            mutating=False,
            units=(
                HelperUnitState(first, generation, HelperUnitStatus[state]),
                HelperUnitState(second, 7, HelperUnitStatus.ACTIVE),
            ),
        )
        snapshots.append(_owner_status(lease, [VpcStatus("first-vpc"), VpcStatus("second-vpc")]))
    assert [snapshot[0]["host_unit"]["generation"] for snapshot in snapshots] == [1, 2, 3]
    assert [snapshot[0]["host_unit"]["status"] for snapshot in snapshots] == [
        "active",
        "retained",
        "active",
    ]
    assert all(
        snapshot[1]["host_unit"] == {"unit": second, "generation": 7, "status": "active"}
        for snapshot in snapshots
    )
    assert lease.status.call_count == 3
