"""P0 loopback UDP/TCP DNS clients, forwarded over authenticated SOCKS TCP.

Each listener serves only its fixture suffix and its own VPC resolver. A missing
SSH transport returns SERVFAIL; there is no upstream public resolver selection.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import socketserver
import struct
import sys
import threading
from contextlib import ExitStack
from pathlib import Path
from uuid import UUID

import dns.exception
import dns.message
import dns.name
import dns.rcode

TIMEOUT = 3
MAX_WIRE = 65535
MAX_UDP = 4096
TCP_QUESTIONS = 32
SOCKS_IPV4 = 1
SOCKS_IPV6 = 4
SOCKS_DOMAIN = 3
STOP = threading.Event()


def receive(connection: socket.socket, length: int) -> bytes:
    data = bytearray()
    while len(data) < length:
        part = connection.recv(length - len(data))
        if not part:
            raise ConnectionError("DNS/SOCKS stream ended early")
        data.extend(part)
    return bytes(data)


class Relay:
    def __init__(self, suffix: str, octet: int, port: int) -> None:
        self.suffix = dns.name.from_text(suffix)
        self.octet = octet
        self.port = port
        self.slots = threading.BoundedSemaphore(32)

    def answer(self, wire: bytes, *, udp: bool) -> bytes:
        query = dns.message.from_wire(wire)
        response = dns.message.make_response(query)
        response.set_rcode(dns.rcode.SERVFAIL)
        limit = min(MAX_UDP, max(512, query.payload)) if udp else MAX_WIRE
        if len(query.question) != 1 or not query.question[0].name.is_subdomain(self.suffix):
            response.set_rcode(dns.rcode.REFUSED)
        elif self.slots.acquire(blocking=False):
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=TIMEOUT) as conn:
                    conn.sendall(b"\x05\x01\x00")
                    if receive(conn, 2) != b"\x05\x00":
                        raise ConnectionError("SOCKS authentication rejected")
                    conn.sendall(
                        b"\x05\x01\x00\x01"
                        + socket.inet_aton(f"10.{self.octet}.0.2")
                        + struct.pack("!H", 53)
                    )
                    reply = receive(conn, 4)
                    if reply[:3] != b"\x05\x00\x00":
                        raise ConnectionError("SOCKS DNS connection rejected")
                    if reply[3] == SOCKS_IPV4:
                        receive(conn, 6)
                    elif reply[3] == SOCKS_IPV6:
                        receive(conn, 18)
                    elif reply[3] == SOCKS_DOMAIN:
                        receive(conn, receive(conn, 1)[0] + 2)
                    else:
                        raise ConnectionError("SOCKS response has invalid address type")
                    conn.sendall(struct.pack("!H", len(wire)) + wire)
                    length = struct.unpack("!H", receive(conn, 2))[0]
                    upstream = dns.message.from_wire(receive(conn, length))
                    if not query.is_response(upstream):
                        raise ConnectionError("VPC DNS response does not match query")
                    response = upstream
            except (OSError, dns.exception.DNSException):
                pass
            finally:
                self.slots.release()
        return response.to_wire(max_size=limit, prefer_truncation=True)


class UDPHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        wire, connection = self.request
        try:
            answer = self.server.relay.answer(wire, udp=True)
            connection.sendto(answer, self.client_address)
        except (OSError, dns.exception.DNSException):
            return


class TCPHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        self.request.settimeout(TIMEOUT)
        try:
            for _ in range(TCP_QUESTIONS):
                length = struct.unpack("!H", receive(self.request, 2))[0]
                answer = self.server.relay.answer(receive(self.request, length), udp=False)
                self.request.sendall(struct.pack("!H", len(answer)) + answer)
        except (OSError, dns.exception.DNSException):
            return


class BoundedServer(socketserver.ThreadingMixIn):
    daemon_threads = True

    def process_request(self, request: object, client_address: tuple) -> None:
        if not self.request_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.request_slots.release()
            raise

    def process_request_thread(self, request: object, client_address: tuple) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.request_slots.release()


class UDPServer(BoundedServer, socketserver.UDPServer):
    daemon_threads = True
    max_packet_size = MAX_UDP


class TCPServer(BoundedServer, socketserver.TCPServer):
    daemon_threads = True
    request_queue_size = 16


def run(manifest: dict) -> None:
    owner = str(UUID(manifest["owner"]))
    if owner != manifest["owner"]:
        raise ValueError("Require canonical proof UUID")
    with ExitStack() as scope:
        for index, octet in enumerate((254, 253)):
            relay = Relay(f"vpc{index}.{owner}.stelvio-proof.test", octet, 10880 + index)
            for server_type, handler in ((UDPServer, UDPHandler), (TCPServer, TCPHandler)):
                server = scope.enter_context(server_type(("127.0.0.1", 10890 + index), handler))
                server.relay = relay
                server.request_slots = threading.BoundedSemaphore(32)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                scope.callback(thread.join, 5)
                scope.callback(server.shutdown)
        sys.stdout.write("READY: two private suffix DNS relays (loopback UDP/TCP)\n")
        sys.stdout.flush()
        STOP.wait()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    if os.geteuid() == 0:
        parser.error("DNS relay must never run as root")
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: STOP.set())
    run(json.loads(args.manifest.read_text()))


if __name__ == "__main__":
    main()
