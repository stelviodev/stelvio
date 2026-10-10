"""The privileged packet boundary accepts only IPv4 traffic in its unit grant."""

import struct
import subprocess

from pytest import fixture, mark

from stelvio.tunnel.helper_protocol import HelperOperation, HelperRequest


@fixture(scope="module")
def validator(traforo_checker):
    return traforo_checker("packet")


def packet(  # noqa: PLR0913 - independent IP fields
    source=0xC0000201, destination=0x0AFE0101, version=0x45, total=24, family=2, protocol=6
):
    return (
        struct.pack(
            "!IBBHHHBBHII", family, version, 0, total, 1, 0, 64, protocol, 0, source, destination
        )
        + b"data"
    )


@mark.parametrize(
    ("data", "from_host", "accepted"),
    [
        (packet(), True, True),
        (packet(source=0x0AFE0101, destination=0xC0000201), False, True),
        (packet(destination=0x0AFC0101), True, True),
        (packet(source=0x0AFC0101, destination=0xC0000201), False, True),
        (packet(destination=0x0AFD0101), True, False),
        (packet(destination=0x08080808), True, False),
        (packet(source=0xC0000203), True, False),
        (packet(source=0x0AFD0101, destination=0xC0000201), False, False),
        (packet(source=0x0AFE0101, destination=0xC0000203), False, False),
        (packet(), False, False),
        (packet(version=0x65), True, False),
        (packet(version=0x44), True, False),
        (packet(version=0x4F), True, False),
        (packet(version=0x46), True, False),
        (packet(protocol=17), True, False),
        (packet(protocol=1), True, False),
        (packet(protocol=0), True, False),
        (packet(total=20), True, False),
        (packet(total=25), True, False),
        (packet(family=30), True, False),
        (packet()[:-1], True, False),
        (packet() + b"extra", True, False),
        (b"", True, False),
        (packet()[:23], True, False),
    ],
)
def test_packet_direction_length_and_vpc_identity_are_enforced(
    validator, data, from_host, accepted
):
    grant = HelperRequest(
        HelperOperation.CONFIGURE,
        session="11111111-1111-4111-8111-111111111111",
        capability=b"opaque-lease-key",
        generation=1,
        unit="12345678",
        vpc_id="vpc-12345678",
        cidrs=("10.254.0.0/16", "10.252.0.0/16"),
    ).encode()
    result = subprocess.run(  # noqa: S603 - fixed read-only compiled harness
        [str(validator)],
        input=struct.pack("!IIB", len(grant), 0xC0000201, from_host) + grant + data,
        check=False,
        capture_output=True,
        timeout=3,
    )
    assert result.returncode == (0 if accepted else 1)
    assert result.stdout == result.stderr == b""
