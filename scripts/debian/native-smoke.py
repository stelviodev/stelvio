# ruff: noqa: INP001, S101, T201 - standalone opt-in VM acceptance checks
"""Exercise the installed Linux helper without AWS or changes outside the VM."""

from uuid import uuid4

from stelvio.tunnel.helper_client import NativeHelper
from stelvio.tunnel.helper_protocol import ResolverEndpoint

helper = NativeHelper(timeout=10)
lease = helper.acquire(str(uuid4()))
try:
    lease.configure(
        unit="00000001",
        generation=1,
        vpc_id="vpc-12345678",
        cidrs=("10.254.9.0/24",),
        resolvers=(ResolverEndpoint("test.traforo.invalid", 65130),),
    )
    assert helper.status().units[0].status.name == "ACTIVE"
    print(helper.diagnostics())
finally:
    lease.close()
assert not helper.status().owned
