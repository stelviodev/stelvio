# ruff: noqa: S603 - fixed system tools and UUID-derived test resource names
"""Explicit privileged Debian acceptance; no AWS operations."""

import json
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
    from .linux_host import route_to

    def selected_routes():
        interfaces = set()
        for octet in (254, 253):
            row = route_to(f"10.{octet}.9.19")
            assert row["destination"] == f"10.{octet}.9.0"
            assert row["mask"] == "255.255.255.0"
            assert row["interface"].startswith("st")
            interfaces.add(row["interface"])
            assert not route_to(f"10.{octet}.0.2")["interface"].startswith("st")
        assert len(interfaces) == 2

    assert_two_vpc_dns_and_packet_forwarding(during_session=selected_routes)


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
    process = subprocess.Popen(
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


def test_linux_foreign_interface_is_never_adopted_or_deleted():
    helper = NativeHelper()
    session = str(uuid4())
    # Match the proposed name, with a distinct foreign ownership alias.
    name = "st" + session.replace("-", "")[:4] + "00000001d"
    subprocess.run(
        ["/usr/bin/sudo", "/usr/sbin/ip", "link", "add", name, "type", "dummy"],
        check=True,
        timeout=10,
    )
    try:
        subprocess.run(
            [
                "/usr/bin/sudo",
                "/usr/sbin/ip",
                "link",
                "set",
                "dev",
                name,
                "alias",
                "foreign-test-owned",
            ],
            check=True,
            timeout=10,
        )
        lease = helper.acquire(session)
        try:
            with pytest.raises(HelperError):
                lease.configure(
                    unit="00000001", generation=1, vpc_id="vpc-12345678", cidrs=("10.254.9.0/24",)
                )
        finally:
            lease.close()
        links = json.loads(
            subprocess.check_output(
                ["/usr/sbin/ip", "-j", "link", "show", name], text=True, timeout=5
            )
        )
        assert links[0]["ifalias"] == "foreign-test-owned"
        assert not helper.status().owned
    finally:
        subprocess.run(
            ["/usr/bin/sudo", "/usr/sbin/ip", "link", "delete", name], check=True, timeout=10
        )


@pytest.mark.parametrize("running", [True, False])
def test_linux_failed_replacement_restores_original_image_and_service(running):
    import os
    from pathlib import Path

    from stelvio.tunnel import installation
    from stelvio.tunnel.assets import select_traforo
    from stelvio.tunnel.linux_installation import authorize, bootstrap

    image = Path(os.environ["STLV_TEST_TRAFORO_FAILED_INSTALL"])
    selection = select_traforo({"STLV_TRAFORO_DEV_BINARY": str(image)})
    before = installation._installed_digest()
    if not running:
        _privileged("/usr/bin/systemctl", "disable", "--now", "stelvio-traforo.service")
    try:
        with pytest.raises(HelperError, match="administration failed"):
            authorize(bootstrap(image, selection.digest))
        assert installation._installed_digest() == before
        if running:
            installation._wait_ready()
            assert not NativeHelper().status().owned
        else:
            for command, expected in (("is-enabled", "disabled"), ("is-active", "inactive")):
                result = subprocess.run(
                    ["/usr/bin/systemctl", command, "stelvio-traforo.service"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                assert result.stdout.strip() == expected
    finally:
        if not running:
            _privileged("/usr/bin/systemctl", "enable", "--now", "stelvio-traforo.service")
            installation._wait_ready()


def _privileged(*args):
    return subprocess.run(["/usr/bin/sudo", *args], check=True, timeout=15)


def _wait_retained(helper):
    deadline = time.monotonic() + 10
    while helper.status().units[0].status.name == "ACTIVE":
        assert time.monotonic() < deadline, "Helper did not withdraw readiness"
        time.sleep(0.1)
    assert helper.status().units[0].status.name == "RETAINED"


def test_linux_dns_repair_refuses_changed_link_ownership():
    from .linux_host import resolver_ports

    helper = NativeHelper()
    session = str(uuid4())
    name = "st" + session.replace("-", "")[:4] + "00000001d"
    domain = uuid4().hex + ".test"
    lease = helper.acquire(session)
    alias = None
    try:
        lease.configure(
            unit="00000001",
            generation=1,
            vpc_id="vpc-12345678",
            cidrs=("10.254.9.0/24",),
            resolvers=(ResolverEndpoint(domain, 65130),),
        )
        link = json.loads(
            subprocess.check_output(
                ["/usr/sbin/ip", "-j", "link", "show", name], text=True, timeout=5
            )
        )[0]
        alias = link["ifalias"]
        _privileged("/usr/sbin/ip", "link", "set", "dev", name, "alias", "foreign-test-owned")
        _privileged("/usr/bin/resolvectl", "revert", name)
        _wait_retained(helper)
        # Exercise several repair polls. Saved ifindex must confer no authority.
        time.sleep(2.2)
        assert resolver_ports((domain,)) == {}
    finally:
        if alias is not None:
            _privileged("/usr/sbin/ip", "link", "set", "dev", name, "alias", alias)
        lease.close()


def test_linux_new_foreign_dns_claim_withdraws_readiness():
    helper = NativeHelper()
    name = "foreign" + uuid4().hex[:7]
    domain = uuid4().hex + ".test"
    lease = helper.acquire(str(uuid4()))
    foreign = False
    try:
        lease.configure(
            unit="00000001",
            generation=1,
            vpc_id="vpc-12345678",
            cidrs=("10.254.9.0/24",),
            resolvers=(ResolverEndpoint(domain, 65130),),
        )
        _privileged("/usr/sbin/ip", "link", "add", name, "type", "dummy")
        foreign = True
        _privileged("/usr/sbin/ip", "address", "add", "192.0.2.240/32", "dev", name)
        _privileged("/usr/sbin/ip", "link", "set", name, "up")
        _privileged("/usr/bin/resolvectl", "dns", name, "127.0.0.1:65131")
        _privileged("/usr/bin/resolvectl", "domain", name, "~child." + domain)
        _wait_retained(helper)
        # resolvectl can scope a loopback server to lo in Manager.DNSEx even
        # though Domains belongs to this dummy link. Inspect the link itself.
        assert "127.0.0.1:65131" in subprocess.check_output(
            ["/usr/bin/resolvectl", "dns", name], text=True, timeout=5
        )
        assert "~child." + domain in subprocess.check_output(
            ["/usr/bin/resolvectl", "domain", name], text=True, timeout=5
        )
        with pytest.raises(OSError, match=r"timed out|unreachable|refused"):
            socket.create_connection(("10.254.9.19", 27017), timeout=0.2)
    finally:
        if foreign:
            _privileged("/usr/sbin/ip", "link", "delete", name)
        lease.close()


def test_linux_failed_first_install_leaves_no_partial_install(tmp_path):
    import os
    from pathlib import Path

    from stelvio.tunnel import installation
    from stelvio.tunnel.assets import select_traforo
    from stelvio.tunnel.linux_installation import authorize, bootstrap

    original = tmp_path / "original-helper"
    original.write_bytes(installation.INSTALLED_HELPER.read_bytes())
    original.chmod(0o700)
    before = installation._installed_digest()
    failed = Path(os.environ["STLV_TEST_TRAFORO_FAILED_INSTALL"])
    candidate = select_traforo({"STLV_TRAFORO_DEV_BINARY": str(failed)})
    authorize(bootstrap(installation.INSTALLED_HELPER, before, remove=True))
    try:
        with pytest.raises(HelperError, match="administration failed"):
            authorize(bootstrap(failed, candidate.digest))
        for path in (
            installation.INSTALLED_HELPER,
            Path("/etc/systemd/system/stelvio-traforo.service"),
            Path("/var/lib/stelvio/tunnel/installation.json"),
        ):
            assert not path.exists(), path
            assert not path.is_symlink(), path
    finally:
        authorize(bootstrap(original, before))
        installation._wait_ready()


@pytest.mark.parametrize("change", ["unit", "drop-in", "owner"])
def test_linux_changed_service_is_refused_before_stop(change):
    import os
    from pathlib import Path

    from stelvio.tunnel import installation
    from stelvio.tunnel.linux_installation import authorize, bootstrap

    service = Path("/etc/systemd/system/stelvio-traforo.service")
    original = service.read_text()
    helper = NativeHelper()
    pid = helper.diagnostics()["pid"]
    override = service.with_name(service.name + ".d")
    try:
        target = service
        content = original + "\n# foreign modification\n"
        if change == "drop-in":
            _privileged("/usr/bin/mkdir", str(override))
            target = override / "foreign.conf"
            content = "[Service]\nEnvironment=FOREIGN_TEST=1\n"
        if change == "owner":
            _privileged("/usr/bin/chown", str(os.getuid()), str(service))
        else:
            subprocess.run(
                ["/usr/bin/sudo", "/usr/bin/tee", str(target)],
                input=content,
                text=True,
                capture_output=True,
                check=True,
                timeout=5,
            )
        with pytest.raises(HelperError, match="administration failed"):
            authorize(bootstrap(installation.INSTALLED_HELPER, installation._installed_digest()))
        assert helper.diagnostics()["pid"] == pid
    finally:
        if change == "drop-in":
            _privileged("/usr/bin/rm", str(override / "foreign.conf"))
            _privileged("/usr/bin/rmdir", str(override))
        elif change == "owner":
            _privileged("/usr/bin/chown", "0:0", str(service))
        else:
            subprocess.run(
                ["/usr/bin/sudo", "/usr/bin/tee", str(service)],
                input=original,
                text=True,
                capture_output=True,
                check=True,
                timeout=5,
            )
