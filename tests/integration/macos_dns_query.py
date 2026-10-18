"""Small fixed-wire DNS interoperability check; DNS algorithms remain Go-owned."""

import socket
import struct
from pathlib import Path


def query_scoped(vpc, manifest):
    resource = next(r for r in manifest.resources if r.vpc == vpc.identity)
    name = resource.hostnames[0]
    candidates = []
    for path in Path("/etc/resolver").glob("stelvio.*"):
        fields = dict(
            line.split(maxsplit=1)
            for line in path.read_text().splitlines()
            if line and not line.startswith("#")
        )
        if fields.get("domain") == name:
            candidates.append(fields)
    assert len(candidates) == 1
    fields = candidates[0]
    # ID=0x1234, recursion desired, one A/IN question. No Python DNS dependency.
    wire = bytes.fromhex("123401000001000000000000")
    wire += b"".join(bytes([len(label)]) + label.encode("ascii") for label in name.split("."))
    wire += bytes.fromhex("0000010001")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:
        connection.settimeout(5)
        connection.connect((fields["nameserver"], int(fields["port"])))
        connection.sendall(wire)
        answer = connection.recv(4096)
    assert len(answer) >= 12
    assert answer[:2] == wire[:2]
    return struct.unpack("!H", answer[2:4])[0] & 15
