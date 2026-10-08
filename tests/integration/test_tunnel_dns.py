"""Installed native helper and actual OS DNS, with a public fallback counterexample."""

import json
import platform
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import dns.message
import dns.rcode
import dns.rrset
from pytest import mark

from stelvio.tunnel.dns import DnsListener
from stelvio.tunnel.helper_client import NativeHelper
from stelvio.tunnel.helper_protocol import ResolverEndpoint

pytestmark = mark.integration_tunnel


class _Resolver:
    def __init__(self, private_domain: str) -> None:
        self.available = True
        self.queries: list[str] = []
        self.private_domain = private_domain

    def answer(self, wire: bytes, *, udp: bool) -> bytes:
        query = dns.message.from_wire(wire)
        self.queries.append(query.question[0].name.to_text())
        response = dns.message.make_response(query)
        if self.available:
            if query.question[0].rdtype == 1:
                name = query.question[0].name
                if name.to_text() == "alias." + self.private_domain + ".":
                    target = "member." + self.private_domain + "."
                    response.answer.append(dns.rrset.from_text(name, 1, "IN", "CNAME", target))
                    name = target
                response.answer.append(dns.rrset.from_text(name, 1, "IN", "A", "10.254.9.19"))
        else:
            response.set_rcode(dns.rcode.SERVFAIL)
        return response.to_wire()


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


def test_native_os_dns_outage_rejects_public_fallback_and_restores_baseline():
    assert platform.system() == "Darwin"
    assert platform.mac_ver()[0] == "15.7.5"
    helper = NativeHelper()
    baseline = helper.inspect()
    assert not baseline.owned
    assert not baseline.uncertain
    domain = "1.1.1.1.nip.io"
    assert _os_lookup(uuid4().hex + "." + domain) == {"1.1.1.1"}
    private_domain = f"p6-native-{uuid4().hex}.internal"
    resolver = _Resolver(private_domain)
    listener = DnsListener(resolver)
    listener.start()
    owner = str(uuid4())
    lease = None
    evidence = {"session": owner, "domain": domain}
    try:
        lease = helper.acquire(owner)
        lease.configure(
            unit="00000019",
            generation=1,
            vpc_id="vpc-12345678",
            cidrs=("10.254.0.0/16",),
            resolvers=(
                ResolverEndpoint(domain, listener.port),
                ResolverEndpoint(private_domain, listener.port),
            ),
        )
        assert _os_lookup(uuid4().hex + "." + domain) == {"10.254.9.19"}
        assert _os_lookup("alias." + private_domain) == {"10.254.9.19"}
        evidence["private_cname_resolved"] = True
        resolver.available = False
        failed = uuid4().hex + "." + domain
        assert _os_lookup(failed) == set()
        assert failed + "." in resolver.queries
        evidence["outage_query_received"] = True
        resolver.available = True
        assert _os_lookup(uuid4().hex + "." + domain) == {"10.254.9.19"}
        assert _os_lookup("aws.amazon.com")
    finally:
        try:
            if lease is not None:
                lease.close()
        finally:
            listener.close()
    assert _os_lookup(uuid4().hex + "." + domain) == {"1.1.1.1"}
    final = helper.inspect()
    assert not final.owned
    assert not final.uncertain
    assert not final.units
    evidence["fallback_rejected_and_baseline_restored"] = True
    path = Path(__file__).resolve().parents[2] / "spikes/dev-vpc-v1/build/p6/os-dns.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, indent=2))
