"""Bounded native helper requests; no AWS data or executable paths cross this boundary."""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field
from enum import IntEnum
from ipaddress import IPv4Network
from uuid import UUID

from stelvio.tunnel.policy import normalize_dns_name

MAGIC = b"STLVTUN1"
HEADER = struct.Struct("!8sBBHI16s16sQ")
MAX_BODY = 32768
MAX_RANGES = 8
MAX_DOMAINS = 64
IDENTITY_BYTES = 16
MIN_PREFIX = 16
MAX_PREFIX = 28
_MAX_GENERATION = (1 << 64) - 1
_MIN_PORT = 1024
_MAX_PORT = 65535
_UNIT = re.compile(r"[0-9a-f]{8}\Z")
_VPC = re.compile(r"vpc-(?:[0-9a-f]{8}|[0-9a-f]{17})\Z")
_PRIVATE = tuple(IPv4Network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


class HelperOperation(IntEnum):
    INSPECT = 1
    ACQUIRE = 2
    CONFIGURE = 3
    REMOVE = 4
    RELEASE = 5
    RECONCILE = 6


@dataclass(frozen=True)
class ResolverEndpoint:
    domain: str
    port: int


@dataclass(frozen=True)
class HelperRequest:
    operation: HelperOperation
    session: str | None = None
    capability: bytes | None = field(default=None, repr=False)
    generation: int = 0
    unit: str | None = None
    vpc_id: str | None = None
    cidrs: tuple[str, ...] = ()
    resolvers: tuple[ResolverEndpoint, ...] = ()
    keep_dns: bool = False

    def encode(self) -> bytes:
        if not isinstance(self.operation, HelperOperation):
            raise TypeError("Unknown native helper operation")
        session, capability = self._identity()
        configuring = self.operation == HelperOperation.CONFIGURE
        removing = self.operation == HelperOperation.REMOVE
        if (
            type(self.generation) is not int
            or not 0 <= self.generation <= _MAX_GENERATION
            or bool(self.generation) != (configuring or removing)
            or type(self.keep_dns) is not bool
            or (self.keep_dns and not removing)
        ):
            raise ValueError("Invalid helper generation or removal flags")
        body = bytearray()
        if configuring or removing:
            if (
                not isinstance(self.unit, str)
                or not _UNIT.fullmatch(self.unit)
                or int(self.unit, 16) == 0
            ):
                raise ValueError("Helper VPC unit must be eight lowercase hex digits, nonzero")
            body.extend(bytes.fromhex(self.unit))
        elif self.unit is not None:
            raise ValueError("This helper operation accepts no VPC unit")
        if configuring:
            self._configuration(body)
        elif self.vpc_id is not None or self.cidrs or self.resolvers:
            raise ValueError("This helper operation accepts no configuration")
        if len(body) > MAX_BODY:
            raise ValueError("Native helper request exceeds its size limit")
        return (
            HEADER.pack(
                MAGIC,
                self.operation,
                int(self.keep_dns),
                0,
                len(body),
                session,
                capability,
                self.generation,
            )
            + body
        )

    def _identity(self) -> tuple[bytes, bytes]:
        anonymous = self.operation in {HelperOperation.INSPECT, HelperOperation.RECONCILE}
        acquiring = self.operation == HelperOperation.ACQUIRE
        if anonymous:
            if self.session is not None or self.capability is not None:
                raise ValueError("Inspection/reconciliation accepts no session capability")
            return bytes(16), bytes(16)
        try:
            session = UUID(self.session)
        except (ValueError, TypeError, AttributeError) as error:
            raise ValueError("Helper session must be a canonical nonzero UUID") from error
        if str(session) != self.session or session.int == 0:
            raise ValueError("Helper session must be a canonical nonzero UUID")
        if acquiring:
            if self.capability is not None:
                raise ValueError("Acquisition accepts no prior capability")
            return session.bytes, bytes(16)
        if (
            not isinstance(self.capability, bytes)
            or len(self.capability) != IDENTITY_BYTES
            or not any(self.capability)
        ):
            raise ValueError("Helper operation requires the acquired session capability")
        return session.bytes, self.capability

    def _configuration(self, body: bytearray) -> None:  # noqa: C901 - independent bounded configuration fields
        if not isinstance(self.vpc_id, str) or not _VPC.fullmatch(self.vpc_id):
            raise ValueError("Helper configuration requires an AWS VPC identity")
        if not isinstance(self.cidrs, tuple) or not 1 <= len(self.cidrs) <= MAX_RANGES:
            raise ValueError("Helper configuration requires one through eight IPv4 ranges")
        networks = []
        for value in self.cidrs:
            network = IPv4Network(value, strict=True)
            if (
                str(network) != value
                or not MIN_PREFIX <= network.prefixlen <= MAX_PREFIX
                or not any(network.subnet_of(parent) for parent in _PRIVATE)
            ):
                raise ValueError("Helper ranges must be canonical RFC1918 IPv4 /16 through /28")
            if any(network.overlaps(other) for other in networks):
                raise ValueError("Helper configuration contains overlapping ranges")
            networks.append(network)
        if not isinstance(self.resolvers, tuple) or len(self.resolvers) > MAX_DOMAINS:
            raise ValueError("Helper configuration exceeds its DNS domain limit")
        domains = set()
        for endpoint in self.resolvers:
            if (
                not isinstance(endpoint, ResolverEndpoint)
                or normalize_dns_name(endpoint.domain) != endpoint.domain
            ):
                raise ValueError("Helper DNS domains must be normalized ASCII names")
            if endpoint.domain in domains:
                raise ValueError("Helper configuration contains duplicate DNS domains")
            if type(endpoint.port) is not int or not _MIN_PORT <= endpoint.port <= _MAX_PORT:
                raise ValueError("Helper DNS endpoint requires an unprivileged loopback port")
            domains.add(endpoint.domain)
        body.extend(_text(self.vpc_id))
        body.append(len(self.cidrs))
        for value in self.cidrs:
            body.extend(_text(value))
        body.append(len(self.resolvers))
        for endpoint in self.resolvers:
            body.extend(_text(endpoint.domain))
            body.extend(struct.pack("!H", endpoint.port))


def _text(value: str) -> bytes:
    encoded = value.encode("ascii")
    return bytes([len(encoded)]) + encoded
