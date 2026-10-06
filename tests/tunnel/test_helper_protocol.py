"""Exercise the production request encoder against independent native validation."""

import json
import shutil
import struct
import subprocess
from pathlib import Path

from pytest import fixture, mark, skip

from stelvio.tunnel.helper_protocol import HelperOperation, HelperRequest, ResolverEndpoint


@fixture(scope="module")
def native_validator(tmp_path_factory):
    compiler = shutil.which("clang")
    if not compiler:
        skip("Native helper parser requires a C compiler")
    root = Path(__file__).parents[2] / "stelvio/tunnel/native"
    binary = tmp_path_factory.mktemp("helper-native") / "check-request"
    subprocess.run(  # noqa: S603 - fixed source files, resolved compiler
        [
            compiler,
            "-std=c17",
            "-Wall",
            "-Wextra",
            "-Werror",
            str(root / "protocol.c"),
            str(root / "check_request.c"),
            "-o",
            str(binary),
        ],
        check=True,
        capture_output=True,
        timeout=20,
    )
    return binary


@mark.parametrize("operation", list(HelperOperation))
def test_native_helper_accepts_only_operation_scoped_configuration(native_validator, operation):
    authenticated = operation in {
        HelperOperation.CONFIGURE,
        HelperOperation.REMOVE,
        HelperOperation.RELEASE,
    }
    scoped = operation in {HelperOperation.CONFIGURE, HelperOperation.REMOVE}
    anonymous = operation in {HelperOperation.INSPECT, HelperOperation.RECONCILE}
    configuring = operation == HelperOperation.CONFIGURE
    request = HelperRequest(
        operation,
        session=None if anonymous else "38321ac9-f086-41ec-91ce-770956914537",
        capability=b"opaque-lease-key" if authenticated else None,
        generation=0x0102030405060708 if scoped else 0,
        unit="00000001" if scoped else None,
        vpc_id="vpc-0123456789abcdef0" if configuring else None,
        cidrs=("10.254.0.0/16", "10.253.0.0/16") if configuring else (),
        resolvers=(ResolverEndpoint("db.private.test", 10890),) if configuring else (),
        keep_dns=operation == HelperOperation.REMOVE,
    )
    result = subprocess.run(  # noqa: S603 - compiled read-only native parser
        [str(native_validator)],
        input=request.encode(),
        check=False,
        capture_output=True,
        timeout=5,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout) == {
        "operation": operation.value,
        "unit": 1 if scoped else 0,
        "generation": 0x0102030405060708 if scoped else 0,
        "keep_dns": operation == HelperOperation.REMOVE,
        "vpc": "vpc-0123456789abcdef0" if configuring else "",
        "session": "00" * 16 if anonymous else "38321ac9f08641ec91ce770956914537",
        "capability_matches_fixture": True,
        "ranges": [
            {"cidr": "10.254.0.0/16", "network": 184418304, "mask": 4294901760},
            {"cidr": "10.253.0.0/16", "network": 184352768, "mask": 4294901760},
        ]
        if configuring
        else [],
        "domains": [{"domain": "db.private.test", "port": 10890}] if configuring else [],
    }
    assert b"opaque-lease-key" not in result.stdout + result.stderr


def raw_configuration(
    *,
    vpc=b"vpc-0123456789abcdef0",
    ranges=(b"10.254.0.0/16",),
    domains=((b"db.private.test", 10890),),
):
    def text(value):
        return bytes([len(value)]) + value

    body = b"\x00\x00\x00\x01" + text(vpc) + bytes([len(ranges)])
    body += b"".join(text(value) for value in ranges) + bytes([len(domains)])
    body += b"".join(text(name) + struct.pack("!H", port) for name, port in domains)
    return (
        struct.pack("!8sBBHI16s16sQ", b"STLVTUN1", 3, 0, 0, len(body), b"s" * 16, b"c" * 16, 1)
        + body
    )


@mark.parametrize(
    "packet",
    [
        raw_configuration(vpc=b"../../resolver"),
        raw_configuration(vpc=b"vpc-0123456789"),
        raw_configuration(ranges=()),
        raw_configuration(ranges=(b"0.0.0.0/0",)),
        raw_configuration(ranges=(b"8.8.0.0/16",)),
        raw_configuration(ranges=(b"10.254.0.1/16",)),
        raw_configuration(ranges=(b"10.254.0.0/16", b"10.254.1.0/24")),
        raw_configuration(ranges=(b"10.254.0.0/29",)),
        raw_configuration(domains=((b"../../hosts", 10890),)),
        raw_configuration(domains=((b"*.private.test", 10890),)),
        raw_configuration(domains=((b"DB.private.test", 10890),)),
        raw_configuration(domains=((b"db\x00.private.test", 10890),)),
        raw_configuration(domains=((b"\xff.private.test", 10890),)),
        raw_configuration(domains=((b"x" * 64 + b".test", 10890),)),
        raw_configuration(domains=((b"10.254.0.2", 10890),)),
        raw_configuration(domains=((b"db.private.test", 53),)),
        raw_configuration(domains=((b"db.private.test", 10890), (b"db.private.test", 10891))),
        raw_configuration(domains=tuple((f"db{i}.test".encode(), 10890) for i in range(65))),
    ],
)
def test_native_helper_rejects_untrusted_configuration_packets(native_validator, packet):
    result = subprocess.run(  # noqa: S603 - compiled read-only native validator
        [str(native_validator)],
        input=packet,
        check=False,
        capture_output=True,
        timeout=5,
    )
    assert result.returncode == 1
    assert result.stdout == b""
    assert result.stderr == b"invalid native helper request\n"


@mark.parametrize(
    "alteration",
    [
        "magic",
        "operation",
        "flags",
        "reserved",
        "length",
        "session",
        "capability",
        "generation",
        "unit",
        "truncated",
        "trailing",
        "oversized",
    ],
)
def test_native_helper_rejects_invalid_authorization_and_framing(native_validator, alteration):
    packet = bytearray(raw_configuration())
    mutations = {
        "magic": (0, 8),
        "operation": (8, 1),
        "session": (16, 16),
        "capability": (32, 16),
        "generation": (48, 8),
        "unit": (56, 4),
    }
    if alteration in mutations:
        offset, size = mutations[alteration]
        packet[offset : offset + size] = bytes(size)
    elif alteration == "flags":
        packet[9] = 1  # DNS retention is meaningful only for remove, never configure.
    elif alteration == "reserved":
        packet[10] = 1
    elif alteration == "length":
        packet[15] += 1
    elif alteration == "truncated":
        packet = packet[:-1]
    elif alteration == "trailing":
        packet += b"\x00"
    elif alteration == "oversized":
        packet += bytes(32769)
    result = subprocess.run(  # noqa: S603 - fixed native parser; no root/host changes
        [str(native_validator)],
        input=packet,
        check=False,
        capture_output=True,
        timeout=5,
    )
    assert result.returncode == 1
    assert result.stdout == b""
    assert result.stderr == b"invalid native helper request\n"


@mark.parametrize("operation", [1, 2, 4, 5, 6])
@mark.parametrize("violation", ["body", "session", "capability", "generation"])
def test_native_helper_rejects_other_operation_scope_violations(
    native_validator, operation, violation
):
    anonymous = operation in {1, 6}
    authenticated = operation in {4, 5}
    session = bytes(16) if anonymous else b"s" * 16
    capability = b"c" * 16 if authenticated else bytes(16)
    generation = 1 if operation == 4 else 0
    body = b"\x00\x00\x00\x01" if operation == 4 else b""
    if violation == "body":
        body += raw_configuration()[56:]
    elif violation == "session":
        session = b"s" * 16 if anonymous else bytes(16)
    elif violation == "capability":
        capability = bytes(16) if authenticated else b"c" * 16
    elif violation == "generation":
        generation = 0 if operation == 4 else 1
    packet = (
        struct.pack(
            "!8sBBHI16s16sQ",
            b"STLVTUN1",
            operation,
            0,
            0,
            len(body),
            session,
            capability,
            generation,
        )
        + body
    )
    result = subprocess.run(  # noqa: S603 - read-only native decoder, independent raw input
        [str(native_validator)],
        input=packet,
        check=False,
        capture_output=True,
        timeout=5,
    )
    assert result.returncode == 1
    assert result.stdout == b""
    assert result.stderr == b"invalid native helper request\n"
