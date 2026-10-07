"""One VPC's scoped loopback DNS view, forwarded exclusively over private TCP."""

from __future__ import annotations

import os
import socketserver
import struct
import time
from dataclasses import dataclass
from ipaddress import IPv4Address
from threading import BoundedSemaphore, Event, Lock, Thread
from typing import TYPE_CHECKING

import dns.exception
import dns.flags
import dns.message
import dns.opcode
import dns.rcode

from stelvio.tunnel.policy import domain_contains, normalize_dns_name

if TYPE_CHECKING:
    from stelvio.tunnel.socks import SocksConnector

MAX_WIRE = 65535
MAX_UDP = 4096
MIN_UDP = 512
IO_TIMEOUT = 3
MAX_REQUESTS = 32
MAX_TCP_QUERIES = 8


@dataclass(frozen=True)
class DnsView:
    generation: int
    domains: tuple[str, ...]
    hostnames: tuple[str, ...]
    resolver: IPv4Address
    connector: SocksConnector

    def __post_init__(self) -> None:
        if type(self.generation) is not int or self.generation <= 0:
            raise ValueError("DNS view requires a positive generation")
        if not isinstance(self.resolver, IPv4Address) or not any(
            self.resolver in cidr for cidr in self.connector.cidrs
        ):
            raise ValueError("DNS resolver belongs to another VPC")
        if any(normalize_dns_name(name) != name for name in (*self.domains, *self.hostnames)):
            raise ValueError("DNS view requires canonical domain and resource names")

    def owns(self, name: str) -> bool:
        return name in self.hostnames or any(
            domain_contains(domain, name) for domain in self.domains
        )


class DnsRelay:
    """Generation-fenced answers; outage retains private rejection ownership."""

    def __init__(self, view: DnsView) -> None:
        self._view = view
        self._available = True
        self._lock = Lock()
        self._slots = BoundedSemaphore(MAX_REQUESTS)
        self._observations: dict[str, tuple[int, Event]] = {}

    def expect_query(self, name: str, generation: int) -> Event:
        event = Event()
        with self._lock:
            if len(self._observations) >= MAX_REQUESTS or name in self._observations:
                raise ValueError("DNS observation exceeds its bound")
            self._observations[name] = (generation, event)
        return event

    def forget_query(self, name: str) -> None:
        with self._lock:
            self._observations.pop(name, None)

    def activate(self, view: DnsView) -> None:
        with self._lock:
            if view.generation <= self._view.generation:
                raise ValueError("DNS view generation is stale")
            self._view = view
            self._available = True

    def reject(self, generation: int) -> bool:
        with self._lock:
            if generation != self._view.generation:
                return False
            self._available = False
            return True

    def answer(self, wire: bytes, *, udp: bool) -> bytes:
        query = dns.message.from_wire(wire)
        response = dns.message.make_response(query)
        response.set_rcode(dns.rcode.SERVFAIL)
        limit = min(MAX_UDP, max(MIN_UDP, query.payload)) if udp else MAX_WIRE
        if query.opcode() != dns.opcode.QUERY or len(query.question) != 1:
            response.set_rcode(dns.rcode.REFUSED)
            return response.to_wire(max_size=limit)
        try:
            name = normalize_dns_name(query.question[0].name.to_text())
        except ValueError:
            response.set_rcode(dns.rcode.REFUSED)
            return response.to_wire(max_size=limit)
        with self._lock:
            view, available = self._view, self._available
            observation = self._observations.get(name)
            if observation and observation[0] == view.generation:
                observation[1].set()
        if not view.owns(name):
            response.set_rcode(dns.rcode.REFUSED)
        elif available and self._slots.acquire(blocking=False):
            try:
                upstream = _exchange(view, query, wire)
                with self._lock:
                    # A completed old query never resurrects a failed/new view.
                    if self._view is view and self._available:
                        response = upstream
            except (OSError, dns.exception.DNSException):
                pass
            finally:
                self._slots.release()
        return response.to_wire(max_size=limit, prefer_truncation=True)


def _exchange(view: DnsView, query: dns.message.Message, wire: bytes) -> dns.message.Message:
    deadline = time.monotonic() + IO_TIMEOUT
    with view.connector.connect(view.resolver, 53, timeout=IO_TIMEOUT) as stream:
        stream.send(struct.pack("!H", len(wire)) + wire, deadline=deadline)
        length = struct.unpack("!H", stream.receive(2, deadline=deadline))[0]
        response = dns.message.from_wire(stream.receive(length, deadline=deadline))
    if not query.is_response(response) or response.flags & dns.flags.TC:
        raise ConnectionError("Private TCP DNS returned a mismatched or incomplete answer")
    return response


class _BoundedServer(socketserver.ThreadingMixIn):
    daemon_threads = False
    block_on_close = True

    def process_request(self, request: object, client_address: tuple) -> None:
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request: object, client_address: tuple) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request: object, client_address: tuple) -> None:
        # Never print raw DNS packets or transport/SDK exceptions to the console.
        pass


class _UdpServer(_BoundedServer, socketserver.UDPServer):
    max_packet_size = MAX_UDP


class _TcpServer(_BoundedServer, socketserver.TCPServer):
    request_queue_size = MAX_REQUESTS


class _UdpHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        wire, connection = self.request
        try:
            answer = self.server.relay.answer(wire, udp=True)
            connection.sendto(answer, self.client_address)
        except (OSError, ValueError, dns.exception.DNSException):
            return


def _receive(connection: object, length: int, deadline: float) -> bytes:
    data = bytearray()
    while len(data) < length:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Local DNS client exceeded its request budget")
        connection.settimeout(remaining)
        part = connection.recv(length - len(data))
        if not part:
            raise ConnectionError("Local DNS client disconnected")
        data.extend(part)
    return bytes(data)


class _TcpHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        # A single connection gets a finite lifetime, including slow partial IO.
        deadline = time.monotonic() + IO_TIMEOUT
        try:
            for _ in range(MAX_TCP_QUERIES):
                length = struct.unpack("!H", _receive(self.request, 2, deadline))[0]
                answer = self.server.relay.answer(
                    _receive(self.request, length, deadline), udp=False
                )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                self.request.settimeout(remaining)
                self.request.sendall(struct.pack("!H", len(answer)) + answer)
        except (OSError, ValueError, dns.exception.DNSException):
            return


class DnsListener:
    """Exclusive UDP/TCP loopback sockets at one ephemeral port, outside handlers."""

    def __init__(self, relay: DnsRelay) -> None:
        if not os.geteuid() or os.getuid() != os.geteuid():
            raise RuntimeError("Private DNS listener must run as the ordinary nonroot user")
        udp = _UdpServer(("127.0.0.1", 0), _UdpHandler)
        try:
            tcp = _TcpServer(udp.server_address, _TcpHandler)
        except BaseException:
            udp.server_close()
            raise
        self.port = udp.server_address[1]
        self._servers = (udp, tcp)
        self._threads: list[Thread] = []
        for server in self._servers:
            server.relay = relay
            server.slots = BoundedSemaphore(MAX_REQUESTS)

    def start(self) -> None:
        if self._threads:
            raise RuntimeError("Private DNS listener is already started")
        for server in self._servers:
            thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1})
            thread.start()
            self._threads.append(thread)

    def close(self) -> None:
        for server in self._servers[: len(self._threads)]:
            server.shutdown()
        for server in self._servers:
            server.server_close()
        for thread in self._threads:
            thread.join()
