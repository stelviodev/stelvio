"""Public launcher runs real isolated child/runtime/forwarder, with local boundary fixtures."""

import os
import subprocess
import sys
import time
from ipaddress import IPv4Network

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

pytestmark = mark.skipif(
    not os.environ.get("STLV_TEST_TRAFORO_IMAGE"),
    reason="Opt-in real Python/Traforo interoperability; pre-download the pinned release",
)

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
        return {"Subnets":[{"SubnetId":"subnet-12345678","VpcId":"vpc-12345678",
            "OwnerId":"123456789012","AvailabilityZone":"us-east-1a",
            "CidrBlock":"10.254.1.0/24","State":"available"}]}
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
    def inspect(self): return SimpleNamespace(owned=False,uncertain=False,units=())
    def acquire(self,session):
        carrier,peer=socket.socketpair(socket.AF_UNIX,socket.SOCK_DGRAM)
        lease=SimpleNamespace(carrier=carrier,connection=peer,closed=False)
        def close(): carrier.close();peer.close()
        lease.close=close;lease.revoke=close
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
from stelvio.tunnel.assets import _ready, _architecture
# Explicit opt-in image: hermetic HOME must not cause downloads from a runtime test.
@contextmanager
def interop_image():
    image=Path(os.environ["STLV_TEST_TRAFORO_IMAGE"])
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
        "vpc-owner",
        "123456789012",
        "us-east-1",
        "vpc-12345678",
        "provider",
        (IPv4Network("10.254.0.0/16"),),
        BastionPolicy.PERSISTENT,
        (),
        (SubnetNetwork("subnet-12345678", "us-east-1a", IPv4Network("10.254.1.0/24")),),
        AccessDescriptor(
            "i-12345678", "sg-12345678", "us-east-1a", "identity-document", "vpc-owner"
        ),
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
                "state": "ready",
                "attempt": 1,
                "cause": None,
                "dns_retained": False,
                "resource_probe": "unverified",
                "phase": "pending",
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
