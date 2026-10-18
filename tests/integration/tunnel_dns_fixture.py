"""A bounded loopback SOCKS upstream for actual Traforo/host interoperability."""

import socket
import socketserver
import struct
from threading import Thread


def _receive(connection, length):
    result = bytearray()
    while len(result) < length:
        part = connection.recv(length - len(result))
        if not part:
            raise ConnectionError("Fixture stream closed")
        result.extend(part)
    return bytes(result)


def _name(value):
    return b"".join(bytes([len(label)]) + label.encode() for label in value.split(".")) + b"\0"


class _Handler(socketserver.BaseRequestHandler):
    def handle(self):
        connection = self.request
        connection.settimeout(3)
        try:
            if _receive(connection, 3) != b"\x05\x01\x00":
                return
            connection.sendall(b"\x05\x00")
            target = _receive(connection, 10)
            if target[:4] != b"\x05\x01\x00\x01":
                return
            connection.sendall(bytes.fromhex("050000017f0000010000"))
            port = struct.unpack("!H", target[-2:])[0]
            if port != 53:
                while data := connection.recv(4096):
                    connection.sendall(data)
                return
            length = struct.unpack("!H", _receive(connection, 2))[0]
            wire = _receive(connection, length)
            # Traforo's upstream query has one uncompressed question. This fixture
            # returns simple deterministic records, not a general DNS implementation.
            end = 12
            labels = []
            while wire[end]:
                count = wire[end]
                labels.append(wire[end + 1 : end + 1 + count].decode())
                end += count + 1
            name = ".".join(labels)
            kind = struct.unpack("!H", wire[end + 1 : end + 3])[0]
            question = wire[12 : end + 5]
            self.server.queries.append(name)
            records = b""
            count = 0
            if self.server.available and kind == 1:
                record_name = b"\xc0\x0c"
                if name.startswith("alias."):
                    member = _name("member." + name.removeprefix("alias."))
                    records += record_name + struct.pack("!HHIH", 5, 1, 0, len(member)) + member
                    record_name = member
                    count += 1
                records += record_name + struct.pack("!HHIH", 1, 1, 0, 4)
                records += socket.inet_aton(self.server.address)
                count += 1
            flags = 0x8180 if self.server.available else 0x8182
            answer = wire[:2] + struct.pack("!HHHHH", flags, 1, count, 0, 0) + question + records
            connection.sendall(struct.pack("!H", len(answer)) + answer)
        except (ConnectionError, OSError):
            return


class SocksDnsFixture:
    def __init__(self, address="10.254.9.19"):
        class Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
            pass

        self.server = Server(("127.0.0.1", 0), _Handler)
        self.server.address = address
        self.server.available = True
        self.server.queries = []
        self.port = self.server.server_address[1]
        self.thread = Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.1})
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        assert not self.thread.is_alive()
