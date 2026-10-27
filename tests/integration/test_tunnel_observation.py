"""Owner-reported snapshots against the installed helper and real host mutations."""

from hashlib import sha256
from uuid import uuid4

from pytest import mark

from stelvio.tunnel.helper_client import NativeHelper
from stelvio.tunnel.helper_protocol import ResolverEndpoint
from stelvio.tunnel.runtime_child import _owner_status
from stelvio.tunnel.supervisor import VpcStatus

from .macos_host import wait_empty

pytestmark = mark.integration_macos_privileged


def test_owner_reports_observed_retirement_and_independent_units():
    session = str(uuid4())
    identities = ("observation-first", "observation-second")
    units = tuple(sha256(identity.encode()).hexdigest()[:8] for identity in identities)
    statuses = [VpcStatus(identity) for identity in identities]
    helper = NativeHelper(timeout=5)
    lease = helper.acquire(session)
    try:
        assert all(value["host_unit"] is None for value in _owner_status(lease, statuses))
        configs = [
            {
                "unit": unit,
                "vpc_id": f"vpc-{index:08x}",
                "cidrs": (f"10.{254 - index}.1.0/24",),
                "resolvers": (
                    ResolverEndpoint(f"{session.replace('-', '')}-{index}.invalid", 65130),
                ),
            }
            for index, unit in enumerate(units)
        ]
        for config in configs:
            lease.configure(**config, generation=1)
        active = _owner_status(lease, statuses)
        assert [value["host_unit"]["status"] for value in active] == ["active", "active"]
        assert [value["host_unit"]["generation"] for value in active] == [1, 1]

        lease.remove(unit=units[0], generation=2, keep_dns=True)
        retained = _owner_status(lease, statuses)
        assert retained[0]["host_unit"] == {
            "unit": units[0],
            "generation": 2,
            "status": "retained",
        }
        assert retained[1]["host_unit"] == active[1]["host_unit"]

        lease.configure(**configs[0], generation=3)
        recovered = _owner_status(lease, statuses)
        assert recovered[0]["host_unit"] == {"unit": units[0], "generation": 3, "status": "active"}
        assert recovered[1]["host_unit"] == active[1]["host_unit"]
    finally:
        lease.close()
        wait_empty()
