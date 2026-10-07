"""Independent binary fixtures exercise the native recovery schema boundary."""

import shutil
import struct
import subprocess
import sys
from pathlib import Path
from uuid import UUID

from pytest import fixture, mark, skip

from stelvio.tunnel.helper_protocol import HelperOperation, HelperRequest, ResolverEndpoint

pytestmark = mark.skipif(sys.platform != "darwin", reason="Native macOS audit-token schema")
SESSION = "11111111-1111-4111-8111-111111111111"
SESSION_BYTES = UUID(SESSION).bytes
CAPABILITY = b"opaque-lease-key"


@fixture(scope="module")
def validator(tmp_path_factory):
    compiler = shutil.which("clang")
    if not compiler:
        skip("Native snapshot validation requires clang")
    root = Path(__file__).parents[2] / "stelvio/tunnel/native"
    binary = tmp_path_factory.mktemp("snapshot") / "check-snapshot"
    subprocess.run(  # noqa: S603 - fixed read-only harness
        [
            compiler,
            "-std=c17",
            "-Wall",
            "-Wextra",
            "-Werror",
            str(root / "protocol.c"),
            str(root / "snapshot.c"),
            str(root / "resolver_plan.c"),
            str(root / "check_snapshot.c"),
            "-lbsm",
            "-o",
            str(binary),
        ],
        check=True,
        capture_output=True,
        timeout=20,
    )
    return binary


def configuration(  # noqa: PLR0913 - independent native wire fixture fields
    unit="12345678",
    vpc="vpc-12345678",
    cidr="10.254.0.0/16",
    domain="db.example.internal",
    generation=3,
    resolvers=None,
):
    return HelperRequest(
        HelperOperation.CONFIGURE,
        session=SESSION,
        capability=CAPABILITY,
        generation=generation,
        unit=unit,
        vpc_id=vpc,
        cidrs=(cidr,),
        resolvers=(ResolverEndpoint(domain, 5300),) if resolvers is None else resolvers,
    ).encode()


def unit(  # noqa: PLR0913 - independent native unit schema fields
    packet=None, phase=2, generation=3, receipt=(2, 16777234, 987654), interface=42, keep_dns=None
):
    packet = configuration() if packet is None else packet
    return (
        struct.pack(
            "!BBQII",
            phase,
            int(phase == 4) if keep_dns is None else keep_dns,
            generation,
            interface,
            len(packet),
        )
        + packet
        + struct.pack("!BQQ", *receipt)
    )


def snapshot(  # noqa: PLR0913 - independent binary schema fields
    units=(),
    *,
    uid=502,
    pid=12345,
    birth=123456789,
    micros=123,
    token=None,
    session=SESSION_BYTES,
    capability=CAPABILITY,
    revision=10,
    carrier_version=1,
):
    token = (0, uid, 20, uid, 20, pid, 0, 7) if token is None else token
    return struct.pack(
        "!8sQIIQQ8I16s16sIB",
        b"STLVSNP3",
        revision,
        uid,
        pid,
        birth,
        micros,
        *token,
        session,
        capability,
        carrier_version,
        len(units),
    ) + b"".join(units)


def run(validator, data):
    return subprocess.run(  # noqa: S603 - fixed compiled read-only validator
        [str(validator)],
        input=data,
        capture_output=True,
        timeout=3,
        check=False,
    )


@mark.parametrize(
    "change",
    ["append", "old-domain", "old-port", "range", "generation", "old-receipt", "new-receipt"],
)
def test_append_only_dns_transaction_preserves_existing_grant_and_inode(validator, change):
    first = ResolverEndpoint("db.example.internal", 5300)
    if change == "old-domain":
        first = ResolverEndpoint("replaced.example.internal", 5300)
    if change == "old-port":
        first = ResolverEndpoint("db.example.internal", 5301)
    packet = configuration(
        cidr="10.253.0.0/16" if change == "range" else "10.254.0.0/16",
        generation=4 if change == "generation" else 3,
        resolvers=(first, ResolverEndpoint("new.example.internal", 5300)),
    )
    old_receipt = (2, 16777234, 987655 if change == "old-receipt" else 987654)
    new_receipt = (1, 16777234, 888) if change == "new-receipt" else (0, 0, 0)
    before = snapshot((unit(),))
    after = snapshot(
        (
            unit(
                packet, phase=1, generation=4 if change == "generation" else 3, receipt=old_receipt
            )
            + struct.pack("!BQQ", *new_receipt),
        ),
        revision=11,
    )
    result = subprocess.run(  # noqa: S603 - fixed compiled read-only snapshot validator
        [str(validator), "--successor"],
        input=struct.pack("!II", len(before), len(after)) + before + after,
        capture_output=True,
        timeout=3,
        check=False,
    )
    assert result.returncode == (0 if change == "append" else 1)
    assert result.stdout == result.stderr == b""


@mark.parametrize(
    ("phase", "receipt"),
    [
        (1, (0, 0, 0)),
        (1, (1, 22, 33)),
        (2, (2, 22, 33)),
        (3, (2, 22, 33)),
        (4, (2, 22, 33)),
        (5, (0, 0, 0)),
    ],
)
def test_phase_specific_snapshot_preserves_all_bytes(validator, phase, receipt):
    data = snapshot((unit(phase=phase, receipt=receipt),))
    result = run(validator, data)
    assert result.returncode == 0
    assert result.stdout == data
    assert result.stderr == b""


def test_distinct_vpc_and_domain_ownership_roundtrip(validator):
    data = snapshot(
        (
            unit(),
            unit(
                configuration("87654321", "vpc-87654321", "10.253.0.0/16", "other.internal"),
                interface=43,
            ),
        )
    )
    result = run(validator, data)
    assert result.returncode == 0
    assert result.stdout == data


@mark.parametrize(
    "data",
    [
        snapshot(uid=0),
        snapshot(pid=0),
        snapshot(pid=1 << 31),
        snapshot(birth=0),
        snapshot(micros=1000000),
        snapshot(revision=0),
        snapshot(carrier_version=0),
        snapshot(carrier_version=2),
        snapshot((unit(interface=0),)),
        snapshot((unit(interface=65536),)),
        snapshot().replace(b"STLVSNP3", b"STLVSNP1", 1),
        snapshot(session=bytes(16)),
        snapshot(capability=bytes(16)),
        snapshot((unit(),), session=UUID("22222222-2222-4222-8222-222222222222").bytes),
        snapshot((unit(),), capability=b"different-secret"),
        snapshot(token=(0, 501, 20, 502, 20, 12345, 0, 7)),
        snapshot(token=(0, 502, 20, 501, 20, 12345, 0, 7)),
        snapshot(token=(0, 502, 20, 502, 20, 12346, 0, 7)),
        snapshot((unit(phase=0),)),
        snapshot().replace(b"STLVSNP3", b"STLVSNP2", 1),
        snapshot((unit(keep_dns=1),)),
        snapshot((unit(phase=1, keep_dns=1),)),
        snapshot((unit(phase=5, receipt=(0, 0, 0), keep_dns=1),)),
        snapshot((unit(phase=4, keep_dns=0),)),
        snapshot((unit(phase=3, keep_dns=2),)),
        snapshot((unit(configuration(generation=(1 << 64) - 1), generation=(1 << 64) - 1),)),
        snapshot(
            (
                unit(
                    configuration(generation=(1 << 64) - 1),
                    phase=1,
                    generation=(1 << 64) - 1,
                    receipt=(0, 0, 0),
                ),
            )
        ),
        snapshot((unit(phase=3, keep_dns=1, generation=(1 << 64) - 1),)),
        snapshot((unit(phase=4, generation=(1 << 64) - 1),)),
        snapshot((unit(phase=3, keep_dns=1, receipt=(0, 0, 0)),)),
        snapshot((unit(phase=6),)),
        snapshot((unit(generation=0),)),
        snapshot((unit(generation=2),)),
        snapshot((unit(generation=4),)),
        snapshot((unit(receipt=(3, 22, 33)),)),
        snapshot((unit(receipt=(0, 22, 0)),)),
        snapshot((unit(receipt=(1, 0, 33)),)),
        snapshot((unit(phase=5),)),
        snapshot((unit(receipt=(1, 22, 33)),)),
        snapshot((unit(phase=4, receipt=(0, 0, 0)),)),
        snapshot((unit(packet=HelperRequest(HelperOperation.INSPECT).encode()),)),
        snapshot((unit(), unit())),
        snapshot((unit(),) * 9),
        snapshot(
            (
                unit(),
                unit(configuration("87654321", "vpc-12345678", "10.253.0.0/16", "other.internal")),
            )
        ),
        snapshot(
            (
                unit(),
                unit(configuration("87654321", "vpc-87654321", "10.254.0.0/17", "other.internal")),
            )
        ),
        snapshot(
            (
                unit(),
                unit(
                    configuration("87654321", "vpc-87654321", "10.253.0.0/16", "example.internal")
                ),
            )
        ),
        snapshot(
            (
                unit(),
                unit(
                    configuration(
                        "87654321", "vpc-87654321", "10.253.0.0/16", "child.db.example.internal"
                    )
                ),
            )
        ),
        snapshot() + b"trailing",
        snapshot()[:-1],
        b"",
        b"foreign",
    ],
)
def test_malformed_snapshot_is_rejected_without_partial_adoption(validator, data):
    result = run(validator, data)
    # Harness returns 2 if any destination byte survives a decoding failure.
    assert result.returncode == 1
    assert result.stdout == result.stderr == b""


@mark.parametrize(
    ("before", "after", "accepted"),
    [
        (snapshot(), snapshot((unit(phase=1, receipt=(0, 0, 0)),), revision=11), True),
        (snapshot(), snapshot((unit(),), revision=11), False),
        (snapshot(), snapshot(revision=11), False),
        (
            snapshot((unit(phase=1, receipt=(0, 0, 0)),)),
            snapshot((unit(phase=1, receipt=(1, 22, 33)),), revision=11),
            True,
        ),
        (
            snapshot((unit(phase=1, receipt=(0, 0, 0)),)),
            snapshot((unit(phase=1, receipt=(2, 22, 33)),), revision=11),
            False,
        ),
        (
            snapshot((unit(phase=1, receipt=(1, 22, 33)),)),
            snapshot((unit(phase=1, receipt=(2, 22, 33)),), revision=11),
            True,
        ),
        (
            snapshot((unit(phase=1, receipt=(1, 22, 33)),)),
            snapshot((unit(phase=1, receipt=(2, 22, 34)),), revision=11),
            False,
        ),
        (snapshot((unit(phase=1),)), snapshot((unit(),), revision=11), True),
        (snapshot((unit(),)), snapshot((unit(phase=3, generation=4),), revision=11), True),
        (snapshot((unit(),)), snapshot((unit(phase=3),), revision=11), False),
        (
            snapshot((unit(phase=3, generation=4),)),
            snapshot((unit(phase=3, generation=4, receipt=(0, 0, 0)),), revision=11),
            True,
        ),
        (
            snapshot((unit(phase=3, generation=4, receipt=(0, 0, 0)),)),
            snapshot((unit(phase=5, generation=4, receipt=(0, 0, 0)),), revision=11),
            True,
        ),
        (
            snapshot((unit(phase=3, generation=4, keep_dns=1),)),
            snapshot((unit(phase=4, generation=4),), revision=11),
            True,
        ),
        (
            snapshot((unit(phase=4, generation=4),)),
            snapshot((unit(phase=3, generation=5),), revision=11),
            True,
        ),
        (
            snapshot((unit(phase=5, generation=4, receipt=(0, 0, 0)),)),
            snapshot(
                (
                    unit(
                        configuration(generation=5),
                        phase=1,
                        generation=5,
                        receipt=(0, 0, 0),
                        interface=43,
                    ),
                ),
                revision=11,
            ),
            True,
        ),
        (
            snapshot((unit(phase=5, receipt=(0, 0, 0)),)),
            snapshot((unit(phase=1, receipt=(0, 0, 0)),), revision=11),
            False,
        ),
        (snapshot((unit(),)), snapshot(revision=11), False),
        (snapshot((unit(),)), snapshot((unit(phase=3, generation=4),), revision=10), False),
        (snapshot((unit(),)), snapshot((unit(phase=3, generation=4),), revision=12), False),
        (
            snapshot((unit(),)),
            snapshot((unit(phase=3, generation=4, interface=43),), revision=11),
            False,
        ),
        (
            snapshot((unit(),)),
            snapshot((unit(phase=3, generation=4),), revision=11, birth=123456790),
            False,
        ),
        (
            snapshot((unit(),)),
            snapshot(
                (unit(phase=3, generation=4),),
                revision=11,
                token=(0, 502, 20, 502, 20, 12345, 0, 8),
            ),
            False,
        ),
        (
            snapshot((unit(),), revision=(1 << 64) - 1),
            snapshot((unit(phase=3, generation=4),), revision=1),
            False,
        ),
    ],
)
def test_only_one_fenced_monotonic_transition_can_be_published(validator, before, after, accepted):
    result = subprocess.run(  # noqa: S603 - fixed native read-only transition harness
        [str(validator), "--successor"],
        input=struct.pack("!II", len(before), len(after)) + before + after,
        capture_output=True,
        timeout=3,
        check=False,
    )
    assert result.returncode == (0 if accepted else 1)
    assert result.stdout == result.stderr == b""


@mark.parametrize("change", ["one_unit", "two_units", "append", "one_receipt", "two_receipts"])
def test_independent_changes_cannot_be_combined_in_one_revision(validator, change):
    if change in {"one_receipt", "two_receipts"}:
        packet = HelperRequest(
            HelperOperation.CONFIGURE,
            session=SESSION,
            capability=CAPABILITY,
            generation=3,
            unit="12345678",
            vpc_id="vpc-12345678",
            cidrs=("10.254.0.0/16",),
            resolvers=(
                ResolverEndpoint("db.example.internal", 5300),
                ResolverEndpoint("cache.example.internal", 5300),
            ),
        ).encode()
        first = unit(packet, phase=1, receipt=(0, 0, 0)) + struct.pack("!BQQ", 0, 0, 0)
        second = unit(packet, phase=1, receipt=(1, 22, 33)) + struct.pack(
            "!BQQ",
            *((1, 22, 34) if change == "two_receipts" else (0, 0, 0)),
        )
        before, after = snapshot((first,)), snapshot((second,), revision=11)
    else:
        packet = configuration("87654321", "vpc-87654321", "10.253.0.0/16", "other.internal")
        before = snapshot((unit(), unit(packet)))
        units = [
            unit(phase=3, generation=4),
            unit(packet, phase=3, generation=4) if change == "two_units" else unit(packet),
        ]
        if change == "append":
            units.append(
                unit(
                    configuration("abcdef12", "vpc-abcdef12", "10.252.0.0/16", "third.internal"),
                    phase=1,
                    receipt=(0, 0, 0),
                )
            )
        after = snapshot(tuple(units), revision=11)
    for candidate in (before, after):
        validated = run(validator, candidate)
        assert validated.returncode == 0
        assert validated.stdout == candidate
    result = subprocess.run(  # noqa: S603 - fixed read-only harness
        [str(validator), "--successor"],
        input=struct.pack("!II", len(before), len(after)) + before + after,
        capture_output=True,
        timeout=3,
        check=False,
    )
    assert result.returncode == (0 if change in {"one_unit", "one_receipt"} else 1)
    assert result.stdout == result.stderr == b""


@mark.parametrize("generation", [1, 0x0102030405060708, (1 << 64) - 1])
def test_resolver_names_and_explicit_domain_preserve_full_generation(validator, generation):
    phase = 3 if generation == (1 << 64) - 1 else 2
    data = snapshot(
        (unit(configuration(generation=generation), generation=generation, phase=phase),)
    )
    result = subprocess.run(  # noqa: S603 - read-only formatter harness
        [str(validator), "--resolver", "0", "0"],
        input=data,
        capture_output=True,
        timeout=3,
        check=False,
    )
    assert result.returncode == 0
    assert result.stderr == b""
    assert (
        result.stdout
        == (
            f"stelvio.11111111111141118111111111111111.12345678.{generation:016x}.00\n"
            f"# Stelvio tunnel/1 session=11111111111141118111111111111111 "
            f"unit=12345678 generation={generation}\n"
            "domain db.example.internal\nnameserver 127.0.0.1\nport 5300\nsearch_order 0\n"
        ).encode()
    )
    assert CAPABILITY not in result.stdout
    assert CAPABILITY.hex().encode() not in result.stdout


def test_removing_unit_keeps_original_resolver_filename_generation(validator):
    data = snapshot((unit(phase=3, generation=4),))
    result = subprocess.run(  # noqa: S603 - read-only formatter harness
        [str(validator), "--resolver", "0", "0"],
        input=data,
        capture_output=True,
        timeout=3,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout.splitlines()[0].endswith(b".0000000000000003.00")
    assert b"generation=3\n" in result.stdout


@mark.parametrize(("unit_index", "domain_index"), [(1, 0), (0, 1), (7, 63)])
def test_resolver_formatter_refuses_missing_unit_or_domain(validator, unit_index, domain_index):
    result = subprocess.run(  # noqa: S603 - read-only formatter harness
        [str(validator), "--resolver", str(unit_index), str(domain_index)],
        input=snapshot((unit(),)),
        capture_output=True,
        timeout=3,
        check=False,
    )
    assert result.returncode == 1
    assert result.stdout == result.stderr == b""


def test_resolver_formatter_selects_second_vpc_and_second_domain(validator):
    packet = HelperRequest(
        HelperOperation.CONFIGURE,
        session=SESSION,
        capability=CAPABILITY,
        generation=3,
        unit="87654321",
        vpc_id="vpc-87654321",
        cidrs=("10.253.0.0/16",),
        resolvers=(
            ResolverEndpoint("cache.other.internal", 5400),
            ResolverEndpoint("db.other.internal", 5401),
        ),
    ).encode()
    second = unit(packet, interface=43) + struct.pack("!BQQ", 2, 22, 34)
    result = subprocess.run(  # noqa: S603 - read-only formatter harness
        [str(validator), "--resolver", "1", "1"],
        input=snapshot((unit(), second)),
        capture_output=True,
        timeout=3,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout == (
        b"stelvio.11111111111141118111111111111111.87654321.0000000000000003.01\n"
        b"# Stelvio tunnel/1 session=11111111111141118111111111111111 "
        b"unit=87654321 generation=3\n"
        b"domain db.other.internal\nnameserver 127.0.0.1\nport 5401\nsearch_order 0\n"
    )
    assert result.stderr == b""


@mark.parametrize(
    ("before", "after", "accepted"),
    [
        (unit(), unit(phase=3, generation=4, keep_dns=1), True),
        (unit(phase=3, generation=4, keep_dns=1), unit(phase=4, generation=4), True),
        (unit(phase=3, generation=4, keep_dns=1), unit(phase=3, generation=4, keep_dns=0), False),
        (
            unit(phase=3, generation=4, keep_dns=1),
            unit(phase=5, generation=4, receipt=(0, 0, 0)),
            False,
        ),
        (unit(phase=3, generation=4, keep_dns=0), unit(phase=4, generation=4), False),
        (unit(phase=4, generation=4), unit(phase=3, generation=5, keep_dns=0), True),
    ],
)
def test_removal_retention_intent_is_durable_and_frozen(validator, before, after, accepted):
    first, second = snapshot((before,)), snapshot((after,), revision=11)
    result = subprocess.run(  # noqa: S603 - native read-only successor validator
        [str(validator), "--successor"],
        input=struct.pack("!II", len(first), len(second)) + first + second,
        capture_output=True,
        timeout=3,
        check=False,
    )
    assert result.returncode == (0 if accepted else 1)
    assert result.stdout == result.stderr == b""


def test_maximal_generation_can_finish_nonretaining_cleanup(validator):
    before = snapshot((unit(phase=3, generation=(1 << 64) - 1, receipt=(0, 0, 0)),))
    after = snapshot((unit(phase=5, generation=(1 << 64) - 1, receipt=(0, 0, 0)),), revision=11)
    result = subprocess.run(  # noqa: S603 - native read-only successor validator
        [str(validator), "--successor"],
        input=struct.pack("!II", len(before), len(after)) + before + after,
        capture_output=True,
        timeout=3,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout == result.stderr == b""


@mark.parametrize(
    ("before", "after", "accepted"),
    [
        (unit(phase=3, generation=4, keep_dns=1), unit(phase=3, generation=5, keep_dns=0), True),
        (unit(phase=3, generation=4, keep_dns=1), unit(phase=3, generation=4, keep_dns=0), False),
        (unit(phase=3, generation=4, keep_dns=0), unit(phase=3, generation=5, keep_dns=1), False),
        (unit(phase=3, generation=4, keep_dns=1), unit(phase=3, generation=5, keep_dns=1), False),
    ],
)
def test_new_generation_can_abandon_unfinished_retention_for_final_disposal(
    validator, before, after, accepted
):
    first, second = snapshot((before,)), snapshot((after,), revision=11)
    result = subprocess.run(  # noqa: S603 - native read-only successor validator
        [str(validator), "--successor"],
        input=struct.pack("!II", len(first), len(second)) + first + second,
        capture_output=True,
        timeout=3,
        check=False,
    )
    assert result.returncode == (0 if accepted else 1)
    assert result.stdout == result.stderr == b""
