"""Real macOS uncached private DNS outage/rejection proof; no AWS effects."""

import json
from pathlib import Path
from uuid import uuid4

import dns.message
import dns.rcode
import dns.rrset

from p4_proof import Proof
from stelvio.tunnel.dns import DnsListener
from stelvio.tunnel.helper_client import NativeHelper
from stelvio.tunnel.helper_protocol import ResolverEndpoint


class Resolver:
    available = True
    queries = []

    def answer(self, wire: bytes, *, udp: bool) -> bytes:
        query = dns.message.from_wire(wire)
        self.queries.append(query.question[0].name.to_text())
        response = dns.message.make_response(query)
        if self.available:
            if query.question[0].rdtype == 1:
                response.answer.append(
                    dns.rrset.from_text(query.question[0].name, 1, "IN", "A", "10.254.9.19")
                )
        else:
            response.set_rcode(dns.rcode.SERVFAIL)
        return response.to_wire()


def main() -> None:
    domain = "1.1.1.1.nip.io"
    proof_id = str(uuid4())
    resolver = Resolver()
    listener = DnsListener(resolver)
    listener.start()
    lease = NativeHelper().acquire(proof_id)
    result = {"session": proof_id, "domain": domain}
    try:
        lease.configure(
            unit="00000019",
            generation=1,
            vpc_id="vpc-12345678",
            cidrs=("10.254.0.0/16",),
            resolvers=(ResolverEndpoint(domain, listener.port),),
        )
        name = uuid4().hex + "." + domain
        result["private_addresses"] = sorted(Proof.os_lookup(name))
        assert result["private_addresses"] == ["10.254.9.19"]
        resolver.available = False
        failed_name = uuid4().hex + "." + domain
        result["outage_addresses"] = sorted(Proof.os_lookup(failed_name))
        result["outage_query_received"] = failed_name + "." in resolver.queries
        assert result["outage_query_received"]
        assert not result["outage_addresses"], result
        resolver.available = True
        result["recovery_addresses"] = sorted(Proof.os_lookup(uuid4().hex + "." + domain))
        assert result["recovery_addresses"] == ["10.254.9.19"]
    finally:
        lease.close()
        listener.close()
        result["final_public_addresses"] = sorted(Proof.os_lookup(uuid4().hex + "." + domain))
        path = Path(__file__).parent / "build/p4-dns-fallback.json"
        path.write_text(json.dumps(result, indent=2))
        print(json.dumps(result), flush=True)
    assert result["final_public_addresses"] == ["1.1.1.1"]
    print(
        "PASS: fresh private DNS outage rejects; recovery and public baseline preserved",
        flush=True,
    )


if __name__ == "__main__":
    main()
