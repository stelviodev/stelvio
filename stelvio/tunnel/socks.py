"""Bounded IPv4 TCP connections through one VPC's authenticated SSH proxy.

No destination hostname reaches the host resolver: private DNS uses this same
connector to the owning VPC's resolver, then forwarding uses the returned IP.
Authentication of the local SSH proxy belongs to the transport supervisor.
"""

from __future__ import annotations

import errno
import os
import select
import socket
import struct
import time
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network
from typing import TYPE_CHECKING, Self

if TYPE_CHECKING:
    from threading import Event

POLL_INTERVAL = 0.1
MAX_TIMEOUT = 60
MAX_READ = 65535
MAX_PORT = 65535
SOCKS_VERSION = 5
SOCKS_IPV4 = 1
SOCKS_IPV6 = 4
SOCKS_DOMAIN = 3


class TransportInterruptedError(ConnectionError):
    """The owning VPC attempt was cancelled."""


class SocksRejectedError(ConnectionError):
    """The proxy refused a new connection; the caller decides retry policy."""


def _wait(connection: socket.socket, *, write: bool, deadline: float, stop: Event) -> None:
    while True:
        if stop.is_set():
            raise TransportInterruptedError("VPC connection attempt cancelled")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("VPC TCP operation exceeded its budget")
        readable, writable, exceptional = select.select(
            [] if write else [connection],
            [connection] if write else [],
            [connection],
            min(POLL_INTERVAL, remaining),
        )
        if exceptional:
            raise ConnectionError("VPC TCP connection failed")
        if readable or writable:
            return


class TcpStream:
    """One owned nonblocking stream with bounded, interruptible exact IO."""

    def __init__(self, connection: socket.socket, stop: Event) -> None:
        self._connection = connection
        self._stop = stop

    def send(self, data: bytes, *, deadline: float) -> None:
        remaining = memoryview(data)
        while remaining:
            _wait(self._connection, write=True, deadline=deadline, stop=self._stop)
            try:
                count = self._connection.send(remaining)
            except BlockingIOError:
                continue
            if not count:
                raise ConnectionError("VPC TCP stream closed while sending")
            remaining = remaining[count:]

    def receive(self, length: int, *, deadline: float) -> bytes:
        if not 0 <= length <= MAX_READ:
            raise ValueError("VPC TCP read exceeds its bound")
        result = bytearray()
        while len(result) < length:
            _wait(self._connection, write=False, deadline=deadline, stop=self._stop)
            try:
                part = self._connection.recv(length - len(result))
            except BlockingIOError:
                continue
            if not part:
                raise ConnectionError("VPC TCP stream ended early")
            result.extend(part)
        return bytes(result)

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


@dataclass(frozen=True)
class SocksConnector:
    proxy_port: int
    cidrs: tuple[IPv4Network, ...]
    stop: Event

    def __post_init__(self) -> None:
        if type(self.proxy_port) is not int or not 1 <= self.proxy_port <= MAX_PORT:
            raise ValueError("SOCKS proxy requires a loopback TCP port")
        if not self.cidrs or any(not isinstance(cidr, IPv4Network) for cidr in self.cidrs):
            raise ValueError("SOCKS connector requires its owning VPC's IPv4 ranges")

    def connect(self, address: IPv4Address, port: int, *, timeout: float = 5) -> TcpStream:
        if not os.geteuid() or os.getuid() != os.geteuid():
            raise RuntimeError("VPC TCP transport must run as the ordinary nonroot user")
        if not isinstance(address, IPv4Address) or not any(address in cidr for cidr in self.cidrs):
            raise ValueError("TCP destination belongs to another VPC")
        if type(port) is not int or not 1 <= port <= MAX_PORT:
            raise ValueError("TCP destination requires a valid port")
        if not 0 < timeout <= MAX_TIMEOUT:
            raise ValueError("TCP connection requires a finite bounded timeout")
        deadline = time.monotonic() + timeout
        connection = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        stream = TcpStream(connection, self.stop)
        try:
            connection.setblocking(False)
            _connect_proxy(connection, self.proxy_port, deadline, self.stop)
            _handshake(stream, address, port, deadline)
        except BaseException:
            stream.close()
            raise
        return stream


def _connect_proxy(connection: socket.socket, port: int, deadline: float, stop: Event) -> None:
    error = connection.connect_ex(("127.0.0.1", port))
    if error not in (0, errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY):
        raise OSError(error, "Local VPC proxy connection failed")
    _wait(connection, write=True, deadline=deadline, stop=stop)
    error = connection.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
    if error:
        raise OSError(error, "Local VPC proxy connection failed")


def _handshake(stream: TcpStream, address: IPv4Address, port: int, deadline: float) -> None:
    stream.send(b"\x05\x01\x00", deadline=deadline)
    if stream.receive(2, deadline=deadline) != b"\x05\x00":
        raise SocksRejectedError("Local VPC proxy rejected its authentication method")
    stream.send(b"\x05\x01\x00\x01" + address.packed + struct.pack("!H", port), deadline=deadline)
    version, status, reserved, kind = stream.receive(4, deadline=deadline)
    if version != SOCKS_VERSION or reserved != 0 or status != 0:
        raise SocksRejectedError("Local VPC proxy rejected the TCP destination")
    if kind == SOCKS_IPV4:
        length = 4
    elif kind == SOCKS_IPV6:
        length = 16
    elif kind == SOCKS_DOMAIN:
        length = stream.receive(1, deadline=deadline)[0]
        if not length:
            raise SocksRejectedError("Local VPC proxy returned an empty bound address")
    else:
        raise SocksRejectedError("Local VPC proxy returned an invalid address type")
    stream.receive(length + 2, deadline=deadline)
