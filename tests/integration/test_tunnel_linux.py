"""Explicit privileged Debian acceptance; no AWS operations."""

import socket
import subprocess
import sys
import time
from contextlib import ExitStack
from uuid import uuid4

import pytest

from stelvio.tunnel.forwarding import Forwarding, packaged_forwarder
from stelvio.tunnel.helper_client import HelperBusyError, HelperError, NativeHelper
from stelvio.tunnel.helper_protocol import ResolverEndpoint

from .linux_host import wait_empty
from .test_tunnel_dns import (
    _os_lookup,
    assert_two_vpc_dns_and_packet_forwarding,
)
from .test_tunnel_dns import (
    test_supervisor_exact_hostname_readiness_and_independent_reconnect as exact_readiness,
)
from .test_tunnel_observation import (
    test_owner_reports_observed_retirement_and_independent_units as owner_observation,
)
from .tunnel_dns_fixture import SocksDnsFixture, native_network, packet_network

pytestmark = pytest.mark.integration_linux_privileged


def test_linux_two_vpc_dns_packet_outage_and_reconnect():
    assert_two_vpc_dns_and_packet_forwarding()


def test_linux_exact_hostname_readiness():
    exact_readiness()


def test_linux_owner_observation():
    owner_observation()


def test_linux_administration_refuses_active_owner():
    from stelvio.tunnel import installation
    from stelvio.tunnel.linux_installation import authorize, bootstrap

    helper = NativeHelper()
    lease = helper.acquire(str(uuid4()))
    try:
        with pytest.raises(HelperBusyError):
            helper.acquire(str(uuid4()))
        with pytest.raises(HelperError):
            authorize(bootstrap(installation.INSTALLED_HELPER, installation._installed_digest()))
        assert helper.status().owned
    finally:
        lease.close()


def test_linux_sigkill_owner_cleans_kernel_state(tmp_path):
    script = """from uuid import uuid4
from stelvio.tunnel.helper_client import NativeHelper
from stelvio.tunnel.helper_protocol import ResolverEndpoint
import sys,time
lease=NativeHelper().acquire(str(uuid4()))
lease.configure(unit='00000001', generation=1, vpc_id='vpc-12345678',
 cidrs=('10.254.9.0/24',),resolvers=(ResolverEndpoint('owner.traforo.test',65130),))
print('READY',flush=True)
sys.stdin.read()
"""
    process = subprocess.Popen(  # noqa: S603 - owned Python fixture process
        [sys.executable, "-c", script], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
    )
    try:
        assert process.stdout.readline().strip() == "READY"
        process.kill()
        process.wait(timeout=5)
        wait_empty()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        process.stdin.close()
        process.stdout.close()


def test_linux_resolver_restart_withdraws_readiness():
    helper = NativeHelper()
    with ExitStack() as scope:
        lease = helper.acquire(str(uuid4()))
        scope.callback(lease.close)
        forwarding = Forwarding(scope.enter_context(packaged_forwarder()), lease.carrier)
        scope.callback(forwarding.close)
        proxy = SocksDnsFixture()
        scope.callback(proxy.close)
        network = native_network(254, uuid4().hex + ".test")
        forwarding.add("00000001", 1, 1, packet_network(network), proxy.port)
        port = forwarding.configure_dns("00000001", 1, 1, 1, network, ())
        lease.configure(
            unit="00000001",
            generation=1,
            vpc_id="vpc-12345678",
            cidrs=("10.254.9.0/24",),
            resolvers=(ResolverEndpoint(network.dns_domains[0], port),),
        )
        assert _os_lookup("before." + network.dns_domains[0]) == {"10.254.9.19"}
        subprocess.run(
            ["/usr/bin/sudo", "/usr/bin/systemctl", "restart", "systemd-resolved"],
            check=True,
            timeout=15,
        )
        deadline = time.monotonic() + 10
        while helper.status().units[0].status.name == "ACTIVE" and time.monotonic() < deadline:
            time.sleep(0.1)
        assert helper.status().units[0].status.name == "RETAINED"
        # The retained DNS view still directs queries to its relay.
        assert _os_lookup("after." + network.dns_domains[0]) == {"10.254.9.19"}
        with pytest.raises(OSError, match=r"timed out|unreachable|refused"):
            socket.create_connection(("10.254.9.19", 27017), timeout=0.2)


def test_linux_helper_crash_reconciles_owned_state():
    helper = NativeHelper(timeout=1)
    lease = helper.acquire(str(uuid4()))
    try:
        lease.configure(
            unit="00000001",
            generation=1,
            vpc_id="vpc-12345678",
            cidrs=("10.254.9.0/24",),
            resolvers=(ResolverEndpoint(uuid4().hex + ".test", 65130),),
        )
        old_pid = helper.diagnostics()["pid"]
        subprocess.run(
            [
                "/usr/bin/sudo",
                "/usr/bin/systemctl",
                "kill",
                "--signal=SIGKILL",
                "stelvio-traforo.service",
            ],
            check=True,
            timeout=10,
        )
        deadline = time.monotonic() + 15
        while True:
            try:
                state = helper.status()
                if (
                    not state.owned
                    and not state.uncertain
                    and helper.diagnostics()["pid"] != old_pid
                ):
                    break
            except (HelperError, OSError):
                pass
            assert time.monotonic() < deadline, "Helper crash recovery timed out"
            time.sleep(0.1)
    finally:
        lease.revoke()


def test_linux_monitor_and_diagnostics_use_real_sources(macos_host, tmp_path):
    from .test_tunnel_diagnostics import (
        test_two_unit_diagnostics_are_read_only_and_survive_failed_readers,
    )

    test_two_unit_diagnostics_are_read_only_and_survive_failed_readers(macos_host, tmp_path)
