"""Input bounds and ownership checks for Python helper requests."""

from uuid import uuid4

import pytest

from stelvio.tunnel.helper_protocol import HEADER, HelperOperation, HelperRequest, ResolverEndpoint

SESSION = "abcdefab-cdef-4abc-8def-abcdefabcdef"
CAPABILITY = b"opaque-lease-key"


def configure(**changes):
    values = {
        "operation": HelperOperation.CONFIGURE,
        "session": SESSION,
        "capability": CAPABILITY,
        "generation": 1,
        "unit": "00000001",
        "vpc_id": "vpc-12345678",
        "cidrs": ("10.254.0.0/16",),
        "resolvers": (ResolverEndpoint("db.internal", 53053),),
    }
    values.update(changes)
    return HelperRequest(**values)


@pytest.mark.parametrize(
    "session", [None, "", "00000000-0000-0000-0000-000000000000", SESSION.upper()]
)
def test_authenticated_requests_require_canonical_nonzero_session(session):
    with pytest.raises(ValueError, match="canonical nonzero UUID"):
        configure(session=session).encode()


@pytest.mark.parametrize("capability", [None, b"", bytes(16), b"too-short"])
def test_mutations_require_nonzero_fixed_size_capability(capability):
    with pytest.raises(ValueError, match="acquired session capability"):
        configure(capability=capability).encode()


@pytest.mark.parametrize("generation", [True, 0, -1, 1 << 64])
def test_scoped_operations_require_integral_nonzero_bounded_generation(generation):
    with pytest.raises(ValueError, match="generation"):
        configure(generation=generation).encode()


@pytest.mark.parametrize("unit", [None, "00000000", "0000000A", "1234567", "00000001x"])
def test_scoped_operations_require_nonzero_canonical_unit(unit):
    with pytest.raises(ValueError, match="VPC unit"):
        configure(unit=unit).encode()


@pytest.mark.parametrize(
    ("cidrs", "message"),
    [
        ((), "1 through 32 IPv4 destination ranges"),
        (("10.254.0.0/16", "10.254.0.0/16"), "overlapping ranges"),
        (("8.8.8.0/24",), "RFC1918"),
        (("10.254.1.0/16",), "host bits set"),
        (("10.254.0.0/15",), "RFC1918"),
    ],
)
def test_configuration_rejects_empty_overlapping_nonprivate_or_noncanonical_ranges(cidrs, message):
    with pytest.raises(ValueError, match=message):
        configure(cidrs=cidrs).encode()


@pytest.mark.parametrize("port", [True, 0, 53, 65536])
def test_resolver_requires_unprivileged_integral_port(port):
    with pytest.raises(ValueError, match="unprivileged loopback port"):
        configure(resolvers=(ResolverEndpoint("db.internal", port),)).encode()


def test_status_requests_are_anonymous_and_configuration_is_bounded():
    request = HelperRequest(HelperOperation.STATUS)
    assert HEADER.size == 56
    assert len(request.encode()) == HEADER.size
    assert request.encode()[8] == 1
    with pytest.raises(ValueError, match="accepts no session capability"):
        HelperRequest(HelperOperation.STATUS, session=str(uuid4())).encode()
    endpoints = tuple(ResolverEndpoint(f"{index}.db.internal", 53053) for index in range(65))
    oversized = configure(resolvers=endpoints)
    with pytest.raises(ValueError, match="domain limit"):
        oversized.encode()


@pytest.mark.parametrize("count", [1, 8, 20, 32])
def test_configure_preserves_selected_subnet_wire_order(count):
    cidrs = tuple(f"10.254.{index + 1}.0/24" for index in range(count))
    packet = configure(cidrs=cidrs, resolvers=()).encode()
    # Independently construct the body: unit, VPC text, ranges, empty DNS list.
    expected = b"\x00\x00\x00\x01\x0cvpc-12345678" + bytes([count])
    expected += b"".join(bytes([len(value)]) + value.encode("ascii") for value in cidrs)
    expected += b"\x00"
    assert packet[56:] == expected
    assert int.from_bytes(packet[12:16], "big") == len(expected)
    assert packet[:12] == b"STLVTUN1\x03\x00\x00\x00"
    assert b"10.254.0.0/16" not in packet


@pytest.mark.parametrize("count", [0, 33, 255])
def test_configure_rejects_range_count_outside_protocol_bound(count):
    cidrs = tuple(f"10.254.{index}.0/24" for index in range(count))
    with pytest.raises(ValueError, match="1 through 32 IPv4 destination ranges"):
        configure(cidrs=cidrs).encode()


def test_maximum_range_and_dns_request_stays_within_wire_body_limit():
    from stelvio.tunnel.helper_protocol import MAX_BODY, MAX_RANGES

    assert MAX_RANGES == 32
    cidrs = tuple(f"192.168.{index}.240/28" for index in range(128, 160))
    endpoints = tuple(
        ResolverEndpoint(f"{index:03d}{'a' * 60}.{'b' * 63}.{'c' * 63}.{'d' * 61}", 65535)
        for index in range(64)
    )
    assert all(len(endpoint.domain) == 253 for endpoint in endpoints)
    packet = configure(vpc_id="vpc-0123456789abcdef0", cidrs=cidrs, resolvers=endpoints).encode()
    assert len(packet) - HEADER.size == 17020
    assert int.from_bytes(packet[12:16], "big") == 17020 < MAX_BODY
