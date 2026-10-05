"""Unprivileged live OS-socket probe for the two local stand-in VPC paths."""

from __future__ import annotations

import json
import socket
import sys
from concurrent.futures import ThreadPoolExecutor


def probe(target: tuple[str, str]) -> dict[str, str | int]:
    address, marker = target
    payload = bytes(range(256)) * 4096
    with socket.create_connection((address, 8080), timeout=15) as connection:
        connection.settimeout(15)
        # Send and read concurrently: the proof also covers backpressure.
        with ThreadPoolExecutor(max_workers=1) as executor:
            sending = executor.submit(connection.sendall, payload)
            data = bytearray()
            while len(data) < len(payload) + len(marker) + 1:
                received = connection.recv(65536)
                if not received:
                    raise RuntimeError(f"{marker}: early EOF after {len(data)} bytes")
                data.extend(received)
            sending.result()
        connection.shutdown(socket.SHUT_WR)
        if connection.recv(1) != b"":
            raise RuntimeError(f"{marker}: expected EOF after half-close")
    expected = marker.encode() + b"\n" + payload
    if data != expected:
        raise RuntimeError(f"{marker}: destination marker or echoed binary data differs")
    return {"target": address, "marker": marker, "bytes": len(payload), "result": "PASS"}


def main() -> None:
    targets = [("10.254.10.10", "vpc-1"), ("10.253.20.20", "vpc-2")]
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(probe, targets))
    sys.stdout.write(json.dumps({"local_socket_paths": results}, indent=2) + "\n")


if __name__ == "__main__":
    main()
