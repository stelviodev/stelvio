"""Exercise the production request encoder against independent native validation."""

import array
import errno
import json
import os
import select
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from pytest import fixture, mark, raises, skip

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


@mark.skipif(sys.platform != "darwin", reason="macOS kernel audit-token authentication")
def test_native_helper_authenticates_actual_socket_peer_and_rejects_stale_generation(tmp_path):
    compiler = shutil.which("clang")
    if not compiler:
        skip("Kernel peer validation requires a native compiler")
    root = Path(__file__).parents[2] / "stelvio/tunnel/native"
    binary = tmp_path / "check-peer"
    subprocess.run(  # noqa: S603 - known source files and resolved compiler
        [
            compiler,
            "-std=c17",
            "-Wall",
            "-Wextra",
            "-Werror",
            str(root / "ownership.c"),
            str(root / "check_peer.c"),
            "-lbsm",
            "-o",
            str(binary),
        ],
        check=True,
        capture_output=True,
        timeout=20,
    )
    # Darwin Unix socket paths are short; the regular pytest temp path is too long.
    with tempfile.TemporaryDirectory(prefix="stlv-peer-", dir="/private/tmp") as scratch:
        path = str(Path(scratch) / "peer.sock")
        child = subprocess.Popen(  # noqa: S603 - nonroot read-only native diagnostic
            [str(binary), path],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert select.select([child.stdout], [], [], 5)[0]
            assert child.stdout.readline() == "READY\n"
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(5)
                client.connect(path)
                stdout, stderr = child.communicate(timeout=5)
            assert child.returncode == 0
            assert stderr == ""
            actual = json.loads(stdout)
            assert actual.pop("birth_seconds") > 0
            assert actual == {
                "uid": os.geteuid(),
                "pid": os.getpid(),
                "alive": True,
                "stale_rejected": True,
                "audit_rejected": True,
                "uninstalled_rejected": True,
            }
            assert not Path(path).exists()
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
            child.stdout.close()
            child.stderr.close()


@fixture(scope="module")
def native_io(tmp_path_factory):
    compiler = shutil.which("clang")
    if not compiler:
        skip("Native frame validation requires a compiler")
    root = Path(__file__).parents[2] / "stelvio/tunnel/native"
    binary = tmp_path_factory.mktemp("helper-io") / "check-io"
    subprocess.run(  # noqa: S603 - fixed native read-only harness sources
        [
            compiler,
            "-std=c17",
            "-Wall",
            "-Wextra",
            "-Werror",
            str(root / "protocol.c"),
            str(root / "io.c"),
            str(root / "packet_io.c"),
            str(root / "check_io.c"),
            "-o",
            str(binary),
        ],
        check=True,
        capture_output=True,
        timeout=20,
    )
    return binary


def _consume_fragment(client, server, fragment):
    import fcntl

    # Observe the receive queue without reading from the parent's descriptor.
    # The child must consume an incomplete fragment before the rest is sent.
    client.sendall(fragment)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        queued = array.array("i", [0])
        fcntl.ioctl(server, 0x4004667F, queued)  # Darwin FIONREAD
        if queued[0] == 0:
            return
        time.sleep(0.005)
    raise AssertionError("Native receiver did not consume initial fragment")


@mark.skipif(sys.platform != "darwin", reason="Selected Darwin ancillary-descriptor bound")
@mark.parametrize("descriptors", [0, 1, 33, 128, 254, 255, -1, "body"])
def test_native_helper_frames_cannot_import_or_close_service_descriptors(native_io, descriptors):
    frame = HelperRequest(HelperOperation.INSPECT).encode()
    if descriptors == "body":
        frame = HelperRequest(
            HelperOperation.CONFIGURE,
            session="11111111-1111-4111-8111-111111111111",
            capability=b"opaque-lease-key",
            generation=1,
            unit="12345678",
            vpc_id="vpc-12345678",
            cidrs=("10.254.0.0/16",),
        ).encode()
    client, server = socket.socketpair()
    with client, server, Path("/dev/null").open("rb") as source:
        client.settimeout(5)
        child = subprocess.Popen(  # noqa: S603 - nonroot controlled socket/harness
            [str(native_io), str(server.fileno())],
            pass_fds=(server.fileno(),),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert select.select([child.stdout], [], [], 5)[0]
            assert child.stdout.readline() == "READY\n"

            if descriptors == 0:
                _consume_fragment(client, server, frame[:7])
                client.sendall(frame[7:])
            elif descriptors == "body":
                _consume_fragment(client, server, frame[:56])
                rights = array.array("i", [source.fileno()])
                assert (
                    client.sendmsg([frame[56:]], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, rights)])
                    == len(frame) - 56
                )
            elif descriptors == -1:
                client.sendall(frame[:-1])
                client.shutdown(socket.SHUT_WR)
            else:
                rights = array.array("i", [source.fileno()] * descriptors)
                if descriptors == 255:
                    # Selected arm64 kernel rejects controls whose expanded
                    # mbuf exceeds 2048 bytes, before installing received FDs.
                    with raises(OSError, match=r"[Ii]nvalid argument") as failure:
                        client.sendmsg([frame], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, rights)])
                    assert failure.value.errno == errno.EINVAL
                    client.sendall(frame)
                else:
                    assert client.sendmsg(
                        [frame], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, rights)]
                    ) == len(frame)
            server.close()
            stdout, stderr = child.communicate(timeout=5)
            assert child.returncode == 0
            assert stderr == ""
            result = json.loads(stdout)
            assert result["before"] == result["after"]
            assert result["sentinels_intact"] is True
            assert result["valid"] is (descriptors in {0, 255})
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
            child.stdout.close()
            child.stderr.close()


@mark.skipif(sys.platform != "darwin", reason="Selected Darwin packet carrier profile")
@mark.parametrize(
    ("descriptors", "kind"),
    [(count, kind) for count in (0, 1, 33, 254) for kind in ("valid", "empty")]
    + [(0, "zero_unit"), (0, "zero_generation"), (0, "short")],
)
def test_packet_datagrams_reject_rights_even_without_payload(native_io, descriptors, kind):
    generation = 0x0102030405060708
    packet = bytes(range(28))
    frame = (
        struct.pack(
            "!IQ",
            0 if kind == "zero_unit" else 0x12345678,
            0 if kind == "zero_generation" else generation,
        )
        + packet
    )
    if kind == "empty":
        frame = b""
    elif kind == "short":
        frame = frame[:35]
    client, server = socket.socketpair(type=socket.SOCK_DGRAM)
    with client, server, Path("/dev/null").open("rb") as source:
        client.settimeout(5)
        child = subprocess.Popen(  # noqa: S603 - fixed nonroot datagram harness
            [str(native_io), str(server.fileno()), "--packet"],
            pass_fds=(server.fileno(),),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            server.close()
            assert select.select([child.stdout], [], [], 5)[0]
            assert child.stdout.readline() == "READY\n"
            controls = (
                [
                    (
                        socket.SOL_SOCKET,
                        socket.SCM_RIGHTS,
                        array.array("i", [source.fileno()] * descriptors),
                    )
                ]
                if descriptors
                else []
            )
            assert client.sendmsg([frame], controls) == len(frame)
            stdout, stderr = child.communicate(timeout=5)
            assert child.returncode == 0
            assert stderr == ""
            result = json.loads(stdout)
            assert result["before"] == result["after"]
            assert result["sentinels_intact"] is True
            assert result["valid"] is (kind == "valid" and descriptors == 0)
            if result["valid"]:
                assert result["unit"] == 0x12345678
                assert result["generation"] == generation
                assert result["packet"] == packet.hex()
            else:
                assert (result["unit"], result["generation"], result["packet"]) == (0, 0, "")
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
            child.stdout.close()
            child.stderr.close()
