"""The Python control client rejects uncertain framing without reusing the channel."""

import socket
import subprocess
import sys
from contextlib import closing
from pathlib import Path

from pytest import fixture, mark, raises

from stelvio.tunnel.forwarding import Forwarding


@fixture
def control_peer(monkeypatch):
    processes = []
    popen = subprocess.Popen

    def make(fault):
        script = """import json,sys,time
fault=sys.argv[1]
hello={"version":2,"status":"ready"}
if fault=="hello": hello["version"]=1
print(json.dumps(hello),flush=True)
for line in sys.stdin:
    request=json.loads(line)
    reply={"version":2,"id":request["id"],"status":"ok","port":32124}
    if fault=="id": reply["id"]+=1
    if fault=="boolean-id": reply["id"]=True
    if fault=="port": reply["port"]=53
    if fault=="status": reply["status"]="unknown"
    if fault=="json": print("not-json",flush=True)
    elif fault=="oversize": print("x"*32769,flush=True)
    elif fault=="eof": sys.stdout.write("{");sys.stdout.flush();sys.exit(0)
    else: print(json.dumps(reply),flush=True)
"""

        def launch(arguments, **options):
            process = popen([sys.executable, "-I", "-u", "-c", script, fault], **options)
            processes.append(process)
            return process

        monkeypatch.setattr("stelvio.tunnel.forwarding.subprocess.Popen", launch)
        carrier, peer = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
        with carrier, peer:
            return Forwarding(Path("unused-fixture-image"), carrier)

    yield make
    for process in processes:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


@mark.parametrize("fault", ["hello", "id", "boolean-id", "status", "json", "oversize", "eof"])
def test_invalid_control_protocol_fails_and_reaps_or_poisons_channel(control_peer, fault):
    if fault == "hello":
        with raises(RuntimeError, match="failed to start"):
            control_peer(fault)
        return
    with closing(control_peer(fault)) as forwarding:
        with raises(ConnectionError):
            forwarding.remove("1", 1, 1)
        forwarding.process.wait(timeout=5)
        assert not forwarding.healthy()


def test_control_timeout_poisons_channel(control_peer, monkeypatch):
    with closing(control_peer("ok")) as forwarding:
        monkeypatch.setattr(
            "stelvio.tunnel.forwarding.select.select",
            lambda read, write, errors, timeout: ([], write, []),
        )
        with raises(TimeoutError, match="acknowledgement timed out"):
            forwarding.remove("1", 1, 1)
        forwarding.process.wait(timeout=5)
        assert not forwarding.healthy()


def test_invalid_dns_listener_port_is_not_published(control_peer):
    from ipaddress import IPv4Network
    from types import SimpleNamespace

    with closing(control_peer("port")) as forwarding:
        network = SimpleNamespace(cidrs=(IPv4Network("10.254.0.0/16"),), dns_domains=())
        with raises(ConnectionError, match="listener port"):
            forwarding.configure_dns("1", 1, 1, network, ())
