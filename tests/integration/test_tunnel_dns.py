"""Installed native helper and actual OS DNS, with a public fallback counterexample."""

import json
import platform
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path
from uuid import uuid4

from pytest import mark, raises

from stelvio.tunnel.forwarding import Forwarding, packaged_forwarder
from stelvio.tunnel.helper_client import HelperError, NativeHelper
from stelvio.tunnel.helper_protocol import ResolverEndpoint

from .tunnel_dns_fixture import (
    SocksDnsFixture,
    dns_exchange,
    dns_query_wire,
    dns_response,
    native_network,
    packet_network,
)

pytestmark = mark.integration_macos_privileged


def _os_lookup(name: str) -> set[str]:
    script = (
        "import json,socket,sys\n"
        "try:\n"
        "    result=sorted({r[4][0] for r in "
        "socket.getaddrinfo(sys.argv[1],None,socket.AF_INET)})\n"
        "except socket.gaierror:\n"
        "    result=[]\n"
        "print(json.dumps(result))\n"
    )
    result = subprocess.run(  # noqa: S603 - fresh ordinary OS resolver process
        [sys.executable, "-I", "-S", "-c", script, name],
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    return set(json.loads(result.stdout))


def _scoped_resolver(domain: str, port: int) -> tuple[Path, int]:
    expected = {f"domain {domain}", "nameserver 127.0.0.1", f"port {port}"}
    matches = []
    resolver_directory = Path("/etc/resolver")
    for path in resolver_directory.glob("stelvio.*"):
        lines = set(path.read_text().splitlines())
        if expected <= lines:
            matches.append((path, path.lstat().st_ino))
    assert len(matches) == 1, f"Expected one scoped resolver for {domain} at 127.0.0.1:{port}"
    return matches[0]


def test_native_os_dns_outage_rejects_public_fallback_and_restores_baseline(tmp_path):  # noqa: PLR0915 - complete host DNS acceptance
    assert platform.system() == "Darwin"
    assert int(platform.mac_ver()[0].split(".")[0]) >= 15
    helper = NativeHelper()
    baseline = helper.status()
    assert not baseline.owned
    assert not baseline.uncertain
    assert not baseline.units
    domain = "1.1.1.1.nip.io"
    assert _os_lookup(uuid4().hex + "." + domain) == {"1.1.1.1"}
    owner = str(uuid4())
    private_domain = f"p6-native-{owner.replace('-', '')}.internal"
    scope = ExitStack()
    proxy = SocksDnsFixture()
    scope.callback(proxy.close)
    lease = None
    evidence = {"session": owner, "domains": [domain, private_domain]}
    try:
        lease = helper.acquire(owner)
        binary = scope.enter_context(packaged_forwarder())
        forwarding = Forwarding(binary, lease.carrier)
        scope.callback(forwarding.close)
        network = native_network(254, domain, private_domain)
        forwarding.add("00000019", 1, 1, packet_network(network), proxy.port)
        port = forwarding.configure_dns("00000019", 1, 1, 1, network, ())
        lease.configure(
            unit="00000019",
            generation=1,
            vpc_id="vpc-12345678",
            cidrs=tuple(str(cidr) for cidr in network.route_cidrs),
            resolvers=(
                ResolverEndpoint(domain, port),
                ResolverEndpoint(private_domain, port),
            ),
        )
        assert _os_lookup(uuid4().hex + "." + domain) == {"10.254.9.19"}
        assert _os_lookup("alias." + private_domain) == {"10.254.9.19"}
        evidence["private_cname_resolved"] = True

        observed = f"observed-{uuid4().hex}.{private_domain}"
        forwarding.dns("arm", "00000019", 1, 1, 1, name=observed)
        assert _os_lookup(observed) == {"10.254.9.19"}
        assert forwarding.dns("check", "00000019", 1, 1, 1, name=observed)["observed"]
        assert forwarding.dns("release", "00000019", 1, 1, 1, name=observed)["observed"]

        resolver_identity = _scoped_resolver(private_domain, port)
        upstream_before_outage = tuple(proxy.server.queries)
        forwarding.dns("reject", "00000019", 1, 1, 1)

        outage_name = f"outage-{uuid4().hex}.{private_domain}"
        for index, transport in enumerate(("udp", "tcp"), start=1):
            wire = dns_query_wire(outage_name, 0x4B30 + index)
            response = dns_exchange("127.0.0.1", port, wire, transport)
            assert dns_response(wire, response) == (2, 0)
        assert _os_lookup(f"os-outage-{uuid4().hex}.{private_domain}") == set()
        assert _os_lookup(f"outage-{uuid4().hex}.{domain}") == set()
        assert _scoped_resolver(private_domain, port) == resolver_identity
        assert tuple(proxy.server.queries) == upstream_before_outage
        evidence["outage"] = {
            "udp_servfail": True,
            "tcp_servfail": True,
            "scoped_resolver_retained": True,
            "macos_lookup_failed": True,
            "upstream_queries_unchanged": True,
        }

        assert forwarding.configure_dns("00000019", 1, 1, 2, network, ()) == port
        assert _scoped_resolver(private_domain, port) == resolver_identity
        recovered = f"recovered-{uuid4().hex}.{private_domain}"
        forwarding.dns("arm", "00000019", 1, 1, 2, name=recovered)
        assert _os_lookup(recovered) == {"10.254.9.19"}
        assert forwarding.dns("check", "00000019", 1, 1, 2, name=recovered)["observed"]
        assert forwarding.dns("release", "00000019", 1, 1, 2, name=recovered)["observed"]
        assert _os_lookup("aws.amazon.com")
    finally:
        try:
            if lease is not None:
                lease.close()
        finally:
            scope.close()
    assert _os_lookup(uuid4().hex + "." + domain) == {"1.1.1.1"}
    final = helper.status()
    assert not final.owned
    assert not final.uncertain
    assert not final.units
    evidence["fallback_rejected_and_baseline_restored"] = True
    (tmp_path / "os-dns.json").write_text(json.dumps(evidence, indent=2))


def test_two_vpc_go_dns_and_packet_forwarding_reconnect():
    assert_two_vpc_dns_and_packet_forwarding()


def assert_two_vpc_dns_and_packet_forwarding(during_session=None, release_digest=None):  # noqa: PLR0915 - complete host/carrier acceptance
    """Shared actual-host acceptance for the selected installed image."""
    import socket

    assert platform.system() == "Darwin"
    helper = NativeHelper()
    initial = helper.status()
    assert not initial.owned
    assert not initial.uncertain
    assert not initial.units
    owner = str(uuid4())
    with ExitStack() as scope:
        lease = helper.acquire(owner)
        scope.callback(lease.close)
        if release_digest is None:
            binary = scope.enter_context(packaged_forwarder())
        else:
            # Upgrade acceptance uses the independently verified pinned release,
            # never a relaxed production activation gate or a user-owned image.
            from stelvio.tunnel.installation import INSTALLED_HELPER, _installed_digest

            assert _installed_digest() == release_digest
            binary = INSTALLED_HELPER
        forwarding = Forwarding(binary, lease.carrier)
        scope.callback(forwarding.close)
        networks = []
        ports = []
        for index, octet in enumerate((254, 253), start=1):
            address = f"10.{octet}.9.19"
            proxy = SocksDnsFixture(address)
            scope.callback(proxy.close)
            domain = f"dns-{owner.replace('-', '')}-{index}.internal"
            network = native_network(octet, domain)
            unit = f"{index:08x}"
            forwarding.add(unit, 1, 1, packet_network(network), proxy.port)
            port = forwarding.configure_dns(unit, 1, 1, 1, network, ())
            lease.configure(
                unit=unit,
                generation=1,
                vpc_id=f"vpc-1234567{index}",
                cidrs=tuple(str(cidr) for cidr in network.route_cidrs),
                resolvers=(ResolverEndpoint(domain, port),),
            )
            forwarding.dns("probe", unit, 1, 1, 1, direct=False)
            for direct in (False, True):
                forwarding.dns(
                    "probe", unit, 1, 1, 1, name="db." + domain, port=27017, direct=direct
                )
            assert _os_lookup("alias." + domain) == {address}
            with socket.create_connection((address, 27017), timeout=3) as connection:
                connection.sendall(b"actual host carrier payload")
                assert connection.recv(256) == b"actual host carrier payload"
            networks.append((unit, network, proxy, address))
            ports.append(port)
        if during_session is not None:
            during_session()
            for _, network, _, address in networks:
                assert _os_lookup(uuid4().hex + "." + network.dns_domains[0]) == {address}
                with socket.create_connection((address, 27017), timeout=3) as connection:
                    connection.sendall(b"healthy after refused upgrade")
                    assert connection.recv(256) == b"healthy after refused upgrade"
        unit, network, proxy, address = networks[0]
        forwarding.dns("reject", unit, 1, 1, 1)
        forwarding.remove(unit, 1, 1)
        # Retain scoped DNS during the outage, then retire that generation before
        # configuring the helper and carrier with a fresh shared generation.
        lease.remove(unit=unit, generation=2, keep_dns=True)
        assert _os_lookup(uuid4().hex + "." + network.dns_domains[0]) == set()
        assert _os_lookup(uuid4().hex + "." + networks[1][1].dns_domains[0]) == {networks[1][3]}
        with raises(ConnectionError, match="Forwarder rejected operation"):
            forwarding.add(unit, 1, 2, packet_network(network), proxy.port)
        lease.remove(unit=unit, generation=3)
        with raises(HelperError):
            lease.configure(
                unit=unit,
                generation=1,
                vpc_id="vpc-12345671",
                cidrs=tuple(str(cidr) for cidr in network.route_cidrs),
            )
        forwarding.add(unit, 4, 2, packet_network(network), proxy.port)
        assert forwarding.configure_dns(unit, 4, 2, 2, network, ()) == ports[0]
        lease.configure(
            unit=unit,
            generation=4,
            vpc_id="vpc-12345671",
            cidrs=tuple(str(cidr) for cidr in network.route_cidrs),
            resolvers=(ResolverEndpoint(network.dns_domains[0], ports[0]),),
        )
        with raises(ConnectionError, match="Forwarder rejected operation"):
            forwarding.dns("reject", unit, 1, 1, 1)
        assert _os_lookup(uuid4().hex + "." + network.dns_domains[0]) == {address}
    final = helper.status()
    assert not final.owned
    assert not final.uncertain
    assert not final.units


def test_supervisor_exact_hostname_readiness_and_independent_reconnect():  # noqa: PLR0915 - full scoped-DNS lifecycle
    """Production readiness with exact resources, real scoped DNS and carrier IO."""
    from types import SimpleNamespace

    from stelvio.tunnel.supervisor import VpcWorker

    helper = NativeHelper()
    owner = str(uuid4())
    with ExitStack() as scope:
        lease = helper.acquire(owner)
        scope.callback(lease.close)
        binary = scope.enter_context(packaged_forwarder())
        forwarding = Forwarding(binary, lease.carrier)
        scope.callback(forwarding.close)
        workers = []
        for octet in (254, 253):
            proxy = SocksDnsFixture(f"10.{octet}.9.19")
            scope.callback(proxy.close)
            network = native_network(octet)
            network.identity = f"exact-{octet}"
            network.vpc_id = f"vpc-{octet:08x}"
            hostname = f"database-{owner.replace('-', '')}-{octet}.example.test"
            resource = SimpleNamespace(vpc=network.identity, hostnames=(hostname,), ports=(27017,))
            discovery = SimpleNamespace(manifest=SimpleNamespace(resources=(resource,)))
            worker = VpcWorker(
                SimpleNamespace(app="proof", environment="test", session_id=owner),
                network,
                SimpleNamespace(client=lambda *args, **kwargs: None),
                lease,
                forwarding,
                discovery,
            )
            worker.transport = SimpleNamespace(healthy=lambda: True, close=lambda: None)
            worker._host_generation = worker._attempt = 1
            forwarding.add(worker.unit, 1, 1, network, proxy.port)
            worker._stack_attempt = 1
            worker._configure(worker._view())
            worker._probe(worker._dns_revision_current)
            assert _os_lookup(hostname) == {proxy.server.address}
            wire = dns_query_wire("unowned." + hostname)
            response = dns_exchange("127.0.0.1", worker._dns_port, wire, "tcp")
            assert dns_response(wire, response)[0] == 5
            workers.append((worker, proxy))
        first, proxy = workers[0]
        second, _ = workers[1]
        assert first._probe_domain != second._probe_domain
        port, domain = first._dns_port, first._probe_domain
        first._reject()
        second._probe(second._dns_revision_current)
        first._attempt = 2
        forwarding.add(first.unit, first._host_generation, 2, first.network, proxy.port)
        first._stack_attempt = 2
        first._configure(first._view())
        first._probe(first._dns_revision_current)
        assert first._dns_port == port
        assert first._probe_domain == domain
        second.discovery.manifest.resources[0].hostnames += (
            f"member-{owner.replace('-', '')}.example.test",
        )
        second._configure(second._view())
        second._probe(second._dns_revision_current)
    final = helper.status()
    assert not final.owned
    assert not final.uncertain
    assert not final.units
