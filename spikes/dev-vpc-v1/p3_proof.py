# ruff: noqa: S101, S603, T201, PLR0913, C901, PLR0915, PT017, PLR2004
"""Nonroot proof of the installed P3 helper; no AWS or privileged Python.

Run from an installed wheel venv. The only elevated action is the production
native cleanup command, which must refuse while this process holds its lease.
"""

import errno
import json
import os
import select
import socket
import struct
import subprocess
import sys
import threading
import time
from contextlib import ExitStack
from pathlib import Path
from uuid import uuid4

from stelvio.tunnel.helper_client import HelperError, NativeHelper
from stelvio.tunnel.helper_protocol import HelperOperation, HelperRequest, ResolverEndpoint
from stelvio.tunnel.installation import cleanup_helper


def checksum(data: bytes) -> int:
    data += b"\0" * (len(data) % 2)
    total = sum(struct.unpack(f"!{len(data) // 2}H", data))
    while total >> 16:
        total = (total & 65535) + (total >> 16)
    return (~total) & 65535


def response(
    source: str,
    target: str,
    sport: int,
    dport: int,
    sequence: int,
    acknowledge: int,
    flags: int,
    data: bytes = b"",
) -> bytes:
    src, dst = socket.inet_aton(source), socket.inet_aton(target)
    tcp = (
        struct.pack("!HHIIBBHHH", sport, dport, sequence, acknowledge, 80, flags, 65535, 0, 0)
        + data
    )
    pseudo = src + dst + struct.pack("!BBH", 0, 6, len(tcp))
    tcp = tcp[:16] + struct.pack("!H", checksum(pseudo + tcp)) + tcp[18:]
    ip = struct.pack("!BBHHHBBH4s4s", 69, 0, 20 + len(tcp), 1, 0, 64, 6, 0, src, dst)
    return ip[:10] + struct.pack("!H", checksum(ip)) + ip[12:] + tcp


def tcp_probe(
    carrier: socket.socket, unit: str, generation: int, remote: str, document_source: str
) -> None:
    marker = b"native carrier proof"
    client = socket.socket()
    client.setblocking(False)
    try:
        assert client.connect_ex((remote, 27017)) in (errno.EINPROGRESS, errno.EWOULDBLOCK)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if not select.select([carrier], [], [], 0.1)[0]:
                continue
            frame = carrier.recv(65547)
            identity, version = struct.unpack("!IQ", frame[:12])
            if identity != int(unit, 16) or version != generation:
                continue
            assert struct.unpack("!I", frame[12:16])[0] == socket.AF_INET
            ip = frame[16:]
            if socket.inet_ntoa(ip[16:20]) != remote or ip[33] & 2 == 0:
                continue
            assert socket.inet_ntoa(ip[12:16]) == document_source
            sport, dport, seq = struct.unpack("!HHI", ip[20:28])
            carrier.send(
                struct.pack("!IQI", identity, generation, socket.AF_INET)
                + response(remote, document_source, dport, sport, 1000, seq + 1, 18)
            )
            assert select.select([], [client], [], 3)[1]
            assert client.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR) == 0
            carrier.send(
                struct.pack("!IQI", identity, generation, socket.AF_INET)
                + response(remote, document_source, dport, sport, 1001, seq + 1, 24, marker)
            )
            assert select.select([client], [], [], 3)[0]
            assert client.recv(1024) == marker
            return
        raise AssertionError("No owned TCP SYN reached the revocable carrier")
    finally:
        client.close()


class DNS:
    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.bind(("127.0.0.1", 0))
        self.socket.settimeout(0.1)
        self.port = self.socket.getsockname()[1]
        self.stop = threading.Event()
        self.queries = 0
        self.thread = threading.Thread(target=self.run)
        self.thread.start()

    def run(self) -> None:
        while not self.stop.is_set():
            try:
                data, peer = self.socket.recvfrom(8192)
            except TimeoutError:
                continue
            except OSError:
                break
            end = 12
            while data[end]:
                end += 1 + data[end]
            end += 5
            kind = struct.unpack("!H", data[end - 4 : end - 2])[0]
            self.queries += 1
            answer = (
                b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, 0, 4) + socket.inet_aton(self.answer)
                if kind == 1
                else b""
            )
            reply = (
                data[:2]
                + struct.pack("!HHHHH", 0x8180, 1, int(bool(answer)), 0, 0)
                + data[12:end]
                + answer
            )
            self.socket.sendto(reply, peer)

    def close(self) -> None:
        self.stop.set()
        self.thread.join(2)
        self.socket.close()
        assert not self.thread.is_alive()


def main() -> None:
    assert os.geteuid() != 0
    helper = NativeHelper(timeout=15)
    assert not helper.inspect().owned
    session = str(uuid4())
    token = session.replace("-", "")
    domains = (f"p3a-{token}.invalid", f"p3b-{token}.invalid")
    passed = []
    with ExitStack() as stack:
        dns = []
        for address in ("10.254.0.7", "10.253.0.7"):
            fixture = DNS(address)
            stack.callback(fixture.close)
            dns.append(fixture)
        lease = helper.acquire(session)
        stack.callback(lease.close)
        other = subprocess.run(
            [
                sys.executable,
                "-c",
                """from stelvio.tunnel.helper_client import NativeHelper,HelperError
from uuid import uuid4
try:
    lease=NativeHelper(timeout=3).acquire(str(uuid4()))
except HelperError as e:
    assert 'busy' in str(e).lower(); print('second process refused')
else:
    lease.close(); raise AssertionError('Second process acquired')
""",
            ],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        assert other.returncode == 0, other.stderr
        passed.append("kernel peer: second process refused")
        try:
            cleanup_helper()
        except HelperError as error:
            assert "active" in str(error).lower(), str(error)
        else:
            raise AssertionError("Active uninstall succeeded")
        assert helper.inspect().owned
        passed.append("native active uninstall refusal without disruption")
        for index, unit in enumerate(("a0000001", "b0000002")):
            lease.configure(
                unit=unit,
                generation=7,
                vpc_id=f"vpc-{index + 1:08x}",
                cidrs=(("10.254.0.0/16", "10.253.0.0/16")[index],),
                resolvers=(ResolverEndpoint(domains[index], dns[index].port),),
            )
        assert len(helper.inspect().units) == 2
        for index, domain in enumerate(domains):
            deadline = time.monotonic() + 15
            while True:
                try:
                    addresses = socket.getaddrinfo("node." + domain, 27017, socket.AF_INET)
                except socket.gaierror:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.1)
                else:
                    assert addresses[0][4][0] == ("10.254.0.7", "10.253.0.7")[index]
                    break
            assert dns[index].queries > 0
        passed.append("actual OS resolver: two dedicated private domains")
        for expected, request in zip(
            ("invalid", "unauthorized"),
            (
                HelperRequest(
                    HelperOperation.REMOVE,
                    session=session,
                    capability=lease.capability,
                    unit="a0000001",
                    generation=6,
                ),
                HelperRequest(
                    HelperOperation.REMOVE,
                    session=session,
                    capability=b"x" * 16,
                    unit="a0000001",
                    generation=8,
                ),
            ),
            strict=True,
        ):
            try:
                helper.request(request)
            except HelperError as error:
                assert str(error) == f"Native helper request failed: {expected}"
            else:
                raise AssertionError("Invalid control accepted")
        assert [
            (unit.unit, unit.generation, unit.status.name) for unit in helper.inspect().units
        ] == [("a0000001", 7, "ACTIVE"), ("b0000002", 7, "ACTIVE")]
        tcp_probe(lease.carrier, "a0000001", 7, "10.254.0.7", "192.0.2.1")
        passed.append("real host TCP through root-owned utun and nonroot carrier")
        lease.remove(unit="a0000001", generation=8)
        tcp_probe(lease.carrier, "b0000002", 7, "10.253.0.7", "192.0.2.3")
        passed.append("per-unit detach preserves other unit TCP")
    assert not helper.inspect().owned
    assert not helper.inspect().units
    assert not list(Path("/private/etc/resolver").glob(f"stelvio.{token}.*"))
    passed.append("lease release removes all owned host state")
    print(json.dumps({"session": session, "uid": os.geteuid(), "passed": passed}, indent=2))


if __name__ == "__main__":
    main()
