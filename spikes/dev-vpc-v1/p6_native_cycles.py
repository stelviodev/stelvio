# Proof assertions and JSON stdout are the executable acceptance interface.
# ruff: noqa: S101, T201, S603, ARG002, PLR2004, C901, PLR0912, PLR0915
"""100 real two-VPC native route/DNS cycles; no AWS or privileged Python."""

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import dns.message
import dns.rrset

from stelvio.tunnel.dns import DnsListener
from stelvio.tunnel.helper_client import HelperError, HelperUnitStatus, NativeHelper
from stelvio.tunnel.helper_protocol import ResolverEndpoint


class Resolver:
    def __init__(self, domain: str, address: str) -> None:
        self.domain, self.address = domain, address

    def answer(self, wire: bytes, *, udp: bool) -> bytes:
        query = dns.message.from_wire(wire)
        response = dns.message.make_response(query)
        if query.question[0].rdtype == 1:
            target = "member." + self.domain + "."
            response.answer.extend(
                [
                    dns.rrset.from_text(query.question[0].name, 0, "IN", "CNAME", target),
                    dns.rrset.from_text(target, 0, "IN", "A", self.address),
                ]
            )
        return response.to_wire()


def lookup(domain: str) -> list[str]:
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-c",
            "import json,socket,sys; print(json.dumps(sorted({r[4][0] for r in "
            "socket.getaddrinfo(sys.argv[1],None,socket.AF_INET)})))",
            "alias." + domain,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return json.loads(result.stdout)


def cycle(count: int) -> dict:
    helper = NativeHelper(timeout=10)
    owner = str(uuid.uuid4())
    lease = None
    listeners = []
    errors = []
    result = {"cycle": count, "session": owner, "uid": os.geteuid()}
    try:
        initial = helper.inspect()
        assert not initial.owned
        assert not initial.uncertain
        assert not initial.units
        lease = helper.acquire(owner)
        for number, (cidr, answer) in enumerate(
            [
                ("10.254.0.0/16", "10.254.9.19"),
                ("10.253.0.0/16", "10.253.9.19"),
            ]
        ):
            domain = f"p6-{uuid.uuid4().hex}.internal"
            listener = DnsListener(Resolver(domain, answer))
            listeners.append((listener, domain, answer))
            listener.start()
            lease.configure(
                unit=f"{number + 1:08x}",
                generation=1,
                vpc_id=f"vpc-0123456789abcde{number:02x}",
                cidrs=(cidr,),
                resolvers=(ResolverEndpoint(domain, listener.port),),
            )
            assert lookup(domain) == [answer]
        assert {(u.unit, u.generation, u.status) for u in helper.inspect().units} == {
            ("00000001", 1, HelperUnitStatus.ACTIVE),
            ("00000002", 1, HelperUnitStatus.ACTIVE),
        }
        lease.remove(unit="00000001", generation=2, keep_dns=False)
        assert {(u.unit, u.generation, u.status) for u in helper.inspect().units} == {
            ("00000001", 2, HelperUnitStatus.INACTIVE),
            ("00000002", 1, HelperUnitStatus.ACTIVE),
        }
        _, domain, answer = listeners[1]
        assert lookup(domain) == [answer]
        result["configured"] = True
    except Exception as error:
        errors.append(error)
        result.update(configured=False, error=repr(error))
        if isinstance(error, HelperError):
            result["stage"] = error.configure_stage
    finally:
        try:
            if lease is not None:
                try:
                    lease.revoke()
                except Exception as error:
                    errors.append(error)
            for listener, _, _ in listeners:
                try:
                    listener.close()
                except Exception as error:
                    errors.append(error)
            if lease is not None:
                try:
                    deadline = time.monotonic() + 30
                    while True:
                        inspected = helper.inspect()
                        if not inspected.owned and not inspected.uncertain and not inspected.units:
                            result["cleanup_certain_empty"] = True
                            break
                        assert time.monotonic() < deadline, "Owned host cleanup not certified"
                        time.sleep(0.1)
                except Exception as error:
                    errors.append(error)
        finally:
            if errors:
                result["errors"] = [repr(error) for error in errors]
            results.append(result)
            path.write_text(json.dumps(results, indent=2))
            print(json.dumps(result), flush=True)
    if errors:
        raise ExceptionGroup("Native cycle failed; recorded primary and cleanup errors", errors)
    assert result.get("configured") is True, result
    assert result.get("cleanup_certain_empty") is True, result
    return result


assert os.geteuid() != 0
results = []
path = Path(__file__).parent / "build/p6/native-cycles.json"
path.parent.mkdir(parents=True, exist_ok=True)
for count in range(100):
    cycle(count)
assert len(results) == 100
