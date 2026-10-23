"""Local two-unit diagnostics acceptance with exact helper restoration; no AWS."""

import json
import os
import socket
import subprocess
import sys
import time
from contextlib import ExitStack
from uuid import uuid4

from pytest import MonkeyPatch, fixture, mark

from stelvio.tunnel.diagnostics import report
from stelvio.tunnel.diagnostics.runtime import Events, RuntimeDiagnostics, snapshot
from stelvio.tunnel.forwarding import Forwarding, packaged_forwarder
from stelvio.tunnel.helper_client import NativeHelper
from stelvio.tunnel.helper_protocol import ResolverEndpoint

from .tunnel_dns_fixture import SocksDnsFixture, native_network, packet_network

pytestmark = mark.integration_macos_privileged


@fixture(scope="session", autouse=True)
def _private_plugin_cache(tmp_path_factory):
    # Native fixtures need no AWS but install_helper validates the pinned plugin.
    # Copy only digest-verified bytes to a private cache; retain all production gates.
    from stelvio import pulumi
    from stelvio.tunnel.assets import _architecture
    from stelvio.tunnel.plugin import _valid, plugin_path

    original = plugin_path()
    descriptor = os.open(original, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        data = stream.read(32 * 1024 * 1024 + 1)
    _valid(data, _architecture())
    cache = tmp_path_factory.mktemp("diagnostics-cache")
    patch = MonkeyPatch()
    patch.setattr(pulumi, "get_stelvio_config_dir", lambda: cache)
    target = plugin_path()
    target.write_bytes(data)
    target.chmod(0o755)
    try:
        yield
    finally:
        patch.undo()


def test_two_unit_diagnostics_are_read_only_and_survive_failed_readers(macos_host, tmp_path):  # noqa: PLR0915
    helper = NativeHelper(timeout=0.5)
    owner = str(uuid4())
    with ExitStack() as scope:
        lease = helper.acquire(owner)
        scope.callback(lease.close)
        binary = scope.enter_context(packaged_forwarder())
        forwarding = Forwarding(binary, lease.carrier)
        scope.callback(forwarding.close)
        vpcs = []
        for index, octet in enumerate((254, 253), start=1):
            address = f"10.{octet}.9.19"
            proxy = SocksDnsFixture(address)
            scope.callback(proxy.close)
            domain = f"diagnostics-{uuid4().hex}.internal"
            network = native_network(octet, domain)
            unit = f"{index:08x}"
            forwarding.add(unit, 1, 1, packet_network(network), proxy.port)
            port = forwarding.configure_dns(unit, 1, 1, 1, network, ())
            lease.configure(
                unit=unit,
                generation=1,
                vpc_id=f"vpc-{index:08x}",
                cidrs=tuple(str(cidr) for cidr in network.route_cidrs),
                resolvers=(ResolverEndpoint(domain, port),),
            )
            forwarding.dns("probe", unit, 1, 1, 1, direct=False)
            with socket.create_connection((address, 27017), timeout=3) as connection:
                connection.sendall(b"known diagnostics transfer")
                assert connection.recv(256) == b"known diagnostics transfer"
            vpcs.append(
                {
                    "identity": unit,
                    "unit": unit,
                    "ranges": [str(cidr) for cidr in network.route_cidrs],
                    "state": "ready",
                    "generation": 1,
                    "dns_revision": 1,
                }
            )
        events = Events()
        events.append("session-ready")
        service = RuntimeDiagnostics(
            owner,
            lambda: {
                "session_id": owner,
                "app": "diagnostics",
                "environment": "local",
                "owner_uid": os.geteuid(),
                "runtime_pid": os.getpid(),
                "forwarder_pid": forwarding.process.pid,
                "vpcs": vpcs,
                "records": [],
            },
            {"helper": helper.diagnostics, "forwarder": forwarding.diagnostics},
            events,
        )
        service.start()
        scope.callback(service.close)
        before = helper.status()
        first = snapshot(service.path)
        second = snapshot(service.path)
        assert first["sources"] == second["sources"] or (
            first["sources"]["helper"]["observed_at"] == second["sources"]["helper"]["observed_at"]
        )
        host_units = first["sources"]["helper"]["data"]["units"]
        assert len(host_units) == 2
        assert all(unit["counters"]["host_delivered_bytes"] > 0 for unit in host_units)
        forward_units = first["sources"]["forwarder"]["data"]["units"]
        assert all(unit["tcp"]["opened"] > 0 for unit in forward_units)
        value = report.collect()
        assert helper.status() == before
        assert value["sections"]["interfaces"]["status"] == "collected"
        assert value["sections"]["resolvers"]["status"] == "collected"
        assert len(value["sections"]["resolver_files"]["data"]["files"]) == 2
        assert value["sections"]["sessions"]["data"]["sessions"]
        wire = json.dumps(value)
        assert "capability" not in wire
        assert "SessionToken" not in wire
        (tmp_path / "diagnostics-report.json").write_text(wire)
        # A separate command can read the live owner without acquiring its lease.
        result = subprocess.run(  # noqa: S603 - fixed installed CLI entry point
            [
                sys.executable,
                "-c",
                "from stelvio.cli import main; main()",
                "tunnel",
                "diagnose",
                "--json",
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=True,
        )
        assert json.loads(result.stdout)["schema_version"] == 1
        assert helper.status() == before
        # Lose the statistics connection. The mutation channel and data plane continue.
        forwarding._diagnostics.close()
        time.sleep(1.05)
        failed = snapshot(service.path)
        assert failed["sources"]["forwarder"]["status"] == "unavailable"
        assert failed["sources"]["helper"]["status"] == "collected"
        forwarding.dns("probe", "00000001", 1, 1, 1, direct=False)
        with socket.create_connection(("10.254.9.19", 27017), timeout=3) as connection:
            connection.sendall(b"healthy after diagnostics failure")
            assert connection.recv(256) == b"healthy after diagnostics failure"
    macos_host.assert_restored("after-diagnostics")


def test_native_helper_snapshot_allows_separate_process_and_broken_forwarder_reader(macos_host):
    helper = NativeHelper(timeout=0.5)
    with ExitStack() as scope:
        owner = str(uuid4())
        lease = helper.acquire(owner)
        scope.callback(lease.close)
        binary = scope.enter_context(packaged_forwarder())
        forwarding = Forwarding(binary, lease.carrier)
        scope.callback(forwarding.close)
        value = helper.diagnostics()
        assert value["session_id"] == owner
        assert value["epoch"]
        assert value["units"] == []
        result = subprocess.run(  # noqa: S603 - fixed snapshot-only entry point
            [
                sys.executable,
                "-c",
                "import json; from stelvio.tunnel.helper_client import NativeHelper; "
                "print(json.dumps(NativeHelper(timeout=.5).diagnostics()))",
            ],
            capture_output=True,
            text=True,
            timeout=3,
            check=True,
        )
        assert json.loads(result.stdout)["session_id"] == owner
        assert forwarding.diagnostics()["units"] == []
        forwarding._diagnostics.close()
        # Even with no configured host routes, the live process still answers
        # mutation requests with a normal operation error instead of exiting.
        from pytest import raises

        with raises(ConnectionError, match="rejected operation"):
            forwarding.remove("00000001", 1, 1)
        assert forwarding.healthy()
        assert helper.status().owned
    macos_host.assert_restored("after-helper-snapshot")
