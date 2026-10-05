"""No-AWS P0 fixture for OS DNS outage/recovery and native runtime independence.

Run after building the artifacts. The separate installed native broker owns host
routes/resolver files; this process remains nonroot and shuts down its children.
Results identify local fixture evidence, never real VPC/DocumentDB acceptance.
"""

from __future__ import annotations

import argparse
import json
import os
import selectors
import signal
import socket
import socketserver
import struct
import subprocess
import sys
import threading
import time
from contextlib import ExitStack
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

import dns.message
import dns.rcode
import dns.rdatatype
import dns.rrset

if TYPE_CHECKING:
    from collections.abc import Callable

relay = import_module("spikes.dev-vpc-v1.dns_relay")
STOP = threading.Event()
BOUND = 180
WORKER = """import json,socket,sys
from importlib import import_module
probe = import_module('spikes.dev-vpc-v1.probe').probe
for line in sys.stdin:
    request = json.loads(line)
    try:
        if request['probe']:
            value = probe((request['name'], request['marker']))
        else:
            addresses = socket.getaddrinfo(request['name'],8080,0,socket.SOCK_STREAM)
            value = sorted({r[4][0] for r in addresses})
        result = {'ok': True, 'value': value}
    except Exception as error:
        result = {'ok': False, 'type': type(error).__name__, 'error': str(error)}
    sys.stdout.write(json.dumps(result) + '\\n')
    sys.stdout.flush()
"""


def stop_child(process: subprocess.Popen, sig: signal.Signals = signal.SIGTERM) -> None:
    process.send_signal(sig)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


class Worker:
    def __init__(self) -> None:
        # Keep one resolver client alive across outage; the parent can cancel it.
        self.process = subprocess.Popen(  # noqa: S603  # Fixed local diagnostic code.
            [sys.executable, "-u", "-c", WORKER],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )

    def call(self, name: str, marker: str | None = None, timeout: float = 20) -> object:
        self.process.stdin.write(
            json.dumps({"name": name, "probe": bool(marker), "marker": marker}) + "\n"
        )
        self.process.stdin.flush()
        deadline = time.monotonic() + timeout
        with selectors.DefaultSelector() as ready:
            ready.register(self.process.stdout, selectors.EVENT_READ)
            while time.monotonic() < deadline and not STOP.is_set():
                if ready.select(min(0.5, max(0, deadline - time.monotonic()))):
                    # One bounded JSON response is written atomically by our child.
                    result = json.loads(self.process.stdout.readline())
                    if result["ok"]:
                        return result["value"]
                    if result["type"] == "gaierror":
                        raise socket.gaierror(result["error"])
                    raise RuntimeError(result["error"])
        raise RuntimeError("Cancellable OS lookup/probe exceeded its deadline or was interrupted")

    def close(self) -> None:
        stop_child(self.process)
        self.process.stdin.close()
        self.process.stdout.close()


class Fixture(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        connection = self.request
        connection.settimeout(5)
        if relay.receive(connection, 3) != b"\x05\x01\x00":
            return
        connection.sendall(b"\x05\x00")
        request = relay.receive(connection, 10)
        dns_target = b"\x05\x01\x00\x01\x0a" + bytes((self.server.octet, 0, 2)) + b"\x00\x35"
        echo_target = b"\x05\x01\x00\x01\x0a" + bytes((self.server.octet, 0, 10)) + b"\x1f\x90"
        if self.server.unavailable.is_set() or request not in (dns_target, echo_target):
            connection.sendall(b"\x05\x05\x00\x01\x00\x00\x00\x00\x00\x00")
            return
        connection.sendall(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00")
        if request == echo_target:
            connection.sendall(f"vpc-{self.server.index + 1}\n".encode())
            while data := connection.recv(65536):
                connection.sendall(data)
            return
        length = struct.unpack("!H", relay.receive(connection, 2))[0]
        query = dns.message.from_wire(relay.receive(connection, length))
        response = dns.message.make_response(query)
        question = query.question[0]
        allowed = (
            self.server.hostname + ".",
            self.server.hostname.replace("service.", "fresh-service.", 1) + ".",
        )
        if question.name.to_text() in allowed:
            if question.rdtype == dns.rdatatype.A:
                response.answer.append(
                    dns.rrset.from_text(
                        question.name, 1, "IN", "A", f"10.{self.server.octet}.0.10"
                    )
                )
        else:
            response.set_rcode(dns.rcode.NXDOMAIN)
        wire = response.to_wire()
        connection.sendall(struct.pack("!H", len(wire)) + wire)


class Server(relay.BoundedServer, socketserver.TCPServer):
    request_queue_size = 16


def exercise(
    servers: list[Server],
    forwarder: subprocess.Popen,
    dns_process: subprocess.Popen,
    record: Callable[..., None],
    worker: Worker,
) -> None:
    hostname = servers[0].hostname
    record("healthy", addresses=worker.call(hostname), tcp=worker.call(hostname, "vpc-1"))
    # Cached valid answers are allowed during an outage; query a fresh record.
    hostname = hostname.replace("service.", "fresh-service.", 1)
    servers[0].unavailable.set()
    try:
        worker.call(hostname)
    except socket.gaierror:
        record("outage", result="OS lookup rejected")
    else:
        raise RuntimeError("OS accepted unavailable private path")
    record("other_vpc_healthy", tcp=worker.call(servers[1].hostname, "vpc-2"))
    servers[0].unavailable.clear()
    record("transport_restored")
    deadline = time.monotonic() + BOUND
    while time.monotonic() < deadline and not STOP.is_set():
        if forwarder.poll() is not None or dns_process.poll() is not None:
            raise RuntimeError("Fixture process exited during recovery")
        try:
            addresses = worker.call(hostname, timeout=min(20, max(0, deadline - time.monotonic())))
        except socket.gaierror:
            record("OS_recovery_pending")
            STOP.wait(3)
            continue
        record("OS_recovered", addresses=addresses, tcp=worker.call(hostname, "vpc-1"))
        break
    else:
        raise RuntimeError("OS lookup recovery deadline exceeded")


def wait_for_control(
    out: Path, forwarder: subprocess.Popen, dns_process: subprocess.Popen
) -> None:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline and not STOP.wait(0.5):
        if forwarder.poll() is not None or dns_process.poll() is not None:
            raise RuntimeError("Fixture process exited before capture control")
        if (out / "start").exists():
            return
    raise RuntimeError("Capture control deadline exceeded")


def run(args: argparse.Namespace) -> None:
    owner = args.owner
    out = args.out
    out.mkdir(parents=True, exist_ok=False)
    root = Path(__file__).resolve().parents[2]
    manifest = out / "manifest.json"
    manifest.write_text(json.dumps({"owner": owner}))
    config = out / "forwarder.json"
    config.write_text(
        json.dumps(
            [
                {"cidr": f"10.{octet}.0.0/16", "socks": f"127.0.0.1:{10880 + i}"}
                for i, octet in enumerate((254, 253))
            ]
        )
    )
    checks = []
    began = time.monotonic()

    def record(stage: str, **values: object) -> None:
        entry = {"stage": stage, "elapsed": round(time.monotonic() - began, 3), **values}
        checks.append(entry)
        (out / "results.json").write_text(json.dumps(checks, indent=2))
        sys.stdout.write(json.dumps(entry) + "\n")
        sys.stdout.flush()

    with ExitStack() as scope:
        servers = []
        for index, octet in enumerate((254, 253)):
            server = scope.enter_context(Server(("127.0.0.1", 10880 + index), Fixture))
            server.index = index
            server.octet = octet
            server.hostname = f"service.vpc{index}.{owner}.stelvio-proof.test"
            server.unavailable = threading.Event()
            server.request_slots = threading.BoundedSemaphore(32)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            scope.callback(thread.join, 5)
            scope.callback(server.shutdown)
            servers.append(server)
        log = scope.enter_context((out / "dns.log").open("w"))
        dns_process = subprocess.Popen(  # noqa: S603  # Fixed proof entry point.
            [
                sys.executable,
                str(root / "spikes/dev-vpc-v1/dns_relay.py"),
                "--manifest",
                str(manifest),
            ],
            stdout=log,
            stderr=log,
        )
        scope.callback(stop_child, dns_process)
        forwarder_log = scope.enter_context((out / "forwarder.log").open("w"))
        # A stdlib-only second venv invokes the native binary, with no installer venv.
        forwarder = subprocess.Popen(  # noqa: S603  # Fixed binary/config and second venv.
            [
                str(args.venv / "bin/python"),
                "-c",
                "import os,sys; os.execv(sys.argv[1], sys.argv[1:])",
                str(root / "spikes/dev-vpc-v1/build/forwarder"),
                "--config",
                str(config),
            ],
            stdout=forwarder_log,
            stderr=forwarder_log,
        )
        scope.callback(stop_child, forwarder, signal.SIGINT)
        worker = Worker()
        scope.callback(worker.close)
        record("waiting_for_native_broker", owner=owner, pid=forwarder.pid)
        deadline = time.monotonic() + 100
        while time.monotonic() < deadline and not STOP.wait(0.5):
            if forwarder.poll() is not None or dns_process.poll() is not None:
                raise RuntimeError("Local fixture child exited before broker readiness")
            if "READY uid=" in (out / "forwarder.log").read_text():
                break
        else:
            raise RuntimeError("Native broker readiness deadline exceeded")
        if args.wait_start:
            record("waiting_for_capture_control")
            wait_for_control(out, forwarder, dns_process)
        exercise(servers, forwarder, dns_process, record, worker)
    record("local_children_stopped")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--venv", type=Path, required=True)
    parser.add_argument("--wait-start", action="store_true")
    args = parser.parse_args()
    if str(UUID(args.owner)) != args.owner:
        parser.error("Require canonical proof UUID")
    if os.geteuid() == 0:
        parser.error("Local fixture must never run as root")
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: STOP.set())
    run(args)


if __name__ == "__main__":
    main()
