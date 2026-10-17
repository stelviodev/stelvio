"""Installed native helper and actual OS DNS, with a public fallback counterexample."""

import json
import platform
import subprocess
import sys
from contextlib import ExitStack
from ipaddress import IPv4Network
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from pytest import mark

from stelvio.tunnel.forwarding import Forwarding, packaged_forwarder
from stelvio.tunnel.helper_client import NativeHelper
from stelvio.tunnel.helper_protocol import ResolverEndpoint

from .tunnel_dns_fixture import SocksDnsFixture

pytestmark = mark.integration_tunnel


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


def test_native_os_dns_outage_rejects_public_fallback_and_restores_baseline():  # noqa: PLR0915 - complete host DNS acceptance
    assert platform.system() == "Darwin"
    assert platform.mac_ver()[0] == "15.7.5"
    helper = NativeHelper()
    baseline = helper.inspect()
    assert not baseline.owned
    assert not baseline.uncertain
    domain = "1.1.1.1.nip.io"
    assert _os_lookup(uuid4().hex + "." + domain) == {"1.1.1.1"}
    private_domain = f"p6-native-{uuid4().hex}.internal"
    scope = ExitStack()
    proxy = SocksDnsFixture()
    scope.callback(proxy.close)
    owner = str(uuid4())
    lease = None
    evidence = {"session": owner, "domain": domain}
    try:
        lease = helper.acquire(owner)
        binary = scope.enter_context(packaged_forwarder())
        forwarding = Forwarding(binary, lease.carrier)
        scope.callback(forwarding.close)
        network = SimpleNamespace(
            cidrs=(IPv4Network("10.254.0.0/16"),), dns_domains=(domain, private_domain)
        )
        forwarding.add("00000019", 1, 1, network, proxy.port)
        port = forwarding.configure_dns("00000019", 1, 1, network, ())
        lease.configure(
            unit="00000019",
            generation=1,
            vpc_id="vpc-12345678",
            cidrs=("10.254.0.0/16",),
            resolvers=(
                ResolverEndpoint(domain, port),
                ResolverEndpoint(private_domain, port),
            ),
        )
        assert _os_lookup(uuid4().hex + "." + domain) == {"10.254.9.19"}
        assert _os_lookup("alias." + private_domain) == {"10.254.9.19"}
        evidence["private_cname_resolved"] = True
        forwarding.dns("reject", "00000019", 1, 1)
        failed = uuid4().hex + "." + domain
        forwarding.dns("arm", "00000019", 1, 1, name=failed)
        try:
            assert _os_lookup(failed) == set()
            assert forwarding.dns("check", "00000019", 1, 1, name=failed)["observed"]
        finally:
            forwarding.dns("release", "00000019", 1, 1, name=failed)
        assert failed not in proxy.server.queries
        evidence["outage_query_received"] = True
        assert forwarding.configure_dns("00000019", 1, 2, network, ()) == port
        assert _os_lookup(uuid4().hex + "." + domain) == {"10.254.9.19"}
        assert _os_lookup("aws.amazon.com")
    finally:
        try:
            if lease is not None:
                lease.close()
        finally:
            scope.close()
    assert _os_lookup(uuid4().hex + "." + domain) == {"1.1.1.1"}
    final = helper.inspect()
    assert not final.owned
    assert not final.uncertain
    assert not final.units
    evidence["fallback_rejected_and_baseline_restored"] = True
    path = Path(__file__).resolve().parents[2] / "spikes/dev-vpc-v1/build/p6/os-dns.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, indent=2))


def test_two_vpc_go_dns_and_packet_forwarding_reconnect():
    import socket

    assert platform.system() == "Darwin"
    helper = NativeHelper()
    initial = helper.inspect()
    assert not initial.owned
    assert not initial.uncertain
    assert not initial.units
    owner = str(uuid4())
    with ExitStack() as scope:
        lease = helper.acquire(owner)
        scope.callback(lease.close)
        binary = scope.enter_context(packaged_forwarder())
        forwarding = Forwarding(binary, lease.carrier)
        scope.callback(forwarding.close)
        networks = []
        ports = []
        for index, octet in enumerate((254, 253), start=1):
            address = f"10.{octet}.9.19"
            proxy = SocksDnsFixture(address)
            scope.callback(proxy.close)
            domain = f"dns-{owner.replace('-', '')}-{index}.internal"
            network = SimpleNamespace(
                cidrs=(IPv4Network(f"10.{octet}.0.0/16"),), dns_domains=(domain,)
            )
            unit = f"{index:08x}"
            forwarding.add(unit, 1, 1, network, proxy.port)
            port = forwarding.configure_dns(unit, 1, 1, network, ())
            lease.configure(
                unit=unit,
                generation=1,
                vpc_id=f"vpc-1234567{index}",
                cidrs=(str(network.cidrs[0]),),
                resolvers=(ResolverEndpoint(domain, port),),
            )
            for direct in (False, True):
                forwarding.dns("probe", unit, 1, 1, direct=direct)
                forwarding.dns("probe", unit, 1, 1, name="db." + domain, port=27017, direct=direct)
            assert _os_lookup("alias." + domain) == {address}
            with socket.create_connection((address, 27017), timeout=3) as connection:
                connection.sendall(b"actual host carrier payload")
                assert connection.recv(256) == b"actual host carrier payload"
            networks.append((unit, network, proxy, address))
            ports.append(port)
        unit, network, proxy, address = networks[0]
        forwarding.dns("reject", unit, 1, 1)
        forwarding.remove(unit, 1, 1)
        assert _os_lookup(uuid4().hex + "." + network.dns_domains[0]) == set()
        assert _os_lookup(uuid4().hex + "." + networks[1][1].dns_domains[0]) == {networks[1][3]}
        forwarding.add(unit, 1, 2, network, proxy.port)
        assert forwarding.configure_dns(unit, 2, 2, network, ()) == ports[0]
        assert _os_lookup(uuid4().hex + "." + network.dns_domains[0]) == {address}
    final = helper.inspect()
    assert not final.owned
    assert not final.uncertain
    assert not final.units
