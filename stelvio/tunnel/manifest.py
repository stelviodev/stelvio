"""Validated, immutable deployment metadata consumed by a dev session.

This is private component metadata, not an application manifest or extension API.
Credentials and handler environments have no field in this contract.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from ipaddress import IPv4Network
from typing import final
from uuid import UUID

from stelvio.tunnel.policy import BastionPolicy, domain_contains, normalize_dns_name

VERSION = 1
OUTPUT_KEY = "_stelvio_network"
ACCOUNT = re.compile(r"[0-9]{12}\Z")
MAX_PORT = 65535


@final
@dataclass(frozen=True)
class SubnetNetwork:
    subnet_id: str
    availability_zone: str
    cidr: IPv4Network


@final
@dataclass(frozen=True)
class AccessDescriptor:
    """Component-owned access group; runtime must verify instance membership."""

    instance_id: str
    security_group_id: str
    availability_zone: str
    identity_document: str
    owner: str


@final
@dataclass(frozen=True)
class VpcNetwork:
    identity: str
    account: str
    region: str
    vpc_id: str
    provider: str
    cidrs: tuple[IPv4Network, ...]
    policy: BastionPolicy
    dns_domains: tuple[str, ...]
    public_subnets: tuple[SubnetNetwork, ...]
    access: AccessDescriptor | None = None


@final
@dataclass(frozen=True)
class ResourceNetwork:
    identity: str
    vpc: str
    service: str
    resource_id: str
    ports: tuple[int, ...]
    hostnames: tuple[str, ...]
    security_groups: tuple[str, ...]


@final
@dataclass(frozen=True)
class EndpointNetwork:
    identity: str
    endpoint_id: str
    vpcs: tuple[str, ...]


@final
@dataclass(frozen=True)
class NetworkManifest:
    vpcs: tuple[VpcNetwork, ...]
    resources: tuple[ResourceNetwork, ...]
    endpoints: tuple[EndpointNetwork, ...]

    @property
    def used_vpcs(self) -> tuple[VpcNetwork, ...]:
        used = {identity for endpoint in self.endpoints for identity in endpoint.vpcs}
        return tuple(vpc for vpc in self.vpcs if vpc.identity in used)

    @property
    def enabled_vpcs(self) -> tuple[VpcNetwork, ...]:
        return tuple(vpc for vpc in self.used_vpcs if vpc.policy != BastionPolicy.DISABLED)


@final
@dataclass(frozen=True)
class SessionDescription:
    session_id: str
    app: str
    environment: str
    owner_uid: int
    manifest: NetworkManifest
    version: int = VERSION

    def __post_init__(self) -> None:
        if self.version != VERSION or type(self.version) is not int:
            raise ValueError("Unsupported tunnel session version")
        if str(UUID(self.session_id)) != self.session_id:
            raise ValueError("Tunnel session ID must be a canonical UUID")
        if type(self.owner_uid) is not int or self.owner_uid <= 0:
            raise ValueError("Tunnel session owner must be a nonroot UID")
        _text(self.app, "app")
        _text(self.environment, "environment")


def _text(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or any(c.isspace() or not c.isprintable() for c in value)
    ):
        raise ValueError(f"Invalid network metadata field {field!r}: require a nonempty string")
    return value


def _items(value: object, field: str, *, required: bool = True) -> list:
    if not isinstance(value, list) or (required and not value):
        raise ValueError(f"Invalid network metadata field {field!r}: require a list")
    return value


def _texts(value: object, field: str, *, required: bool = True) -> tuple[str, ...]:
    result = tuple(_text(item, field) for item in _items(value, field, required=required))
    if len(set(result)) != len(result):
        raise ValueError(f"Duplicate network metadata field {field!r}")
    return result


def _fields(value: object, required: set[str], optional: set[str] = frozenset()) -> dict:
    if (
        not isinstance(value, dict)
        or required - value.keys()
        or value.keys() - required - optional
    ):
        raise ValueError("Malformed network metadata: missing or unknown fields")
    return value


def _network(value: object) -> IPv4Network:
    try:
        return IPv4Network(_text(value, "cidr"), strict=True)
    except ValueError as error:
        raise ValueError(
            "Invalid network metadata CIDR: require a canonical IPv4 network"
        ) from error


def _subnet(value: object) -> SubnetNetwork:
    data = _fields(value, {"id", "az", "cidr"})
    return SubnetNetwork(
        _text(data["id"], "subnet id"), _text(data["az"], "az"), _network(data["cidr"])
    )


def _access(value: object) -> AccessDescriptor | None:
    if value is None:
        return None
    data = _fields(value, {"instance_id", "security_group_id", "az", "identity_document", "owner"})
    return AccessDescriptor(
        *(
            _text(data[key], key)
            for key in ("instance_id", "security_group_id", "az", "identity_document", "owner")
        )
    )


def _vpc(data: dict) -> VpcNetwork:
    _fields(
        data,
        {
            "version",
            "kind",
            "identity",
            "account",
            "region",
            "vpc_id",
            "provider",
            "cidrs",
            "policy",
            "dns_domains",
            "public_subnets",
        },
        {"access"},
    )
    account = _text(data["account"], "account")
    if not ACCOUNT.fullmatch(account):
        raise ValueError("Invalid network metadata AWS account")
    cidrs = tuple(_network(item) for item in _items(data["cidrs"], "cidrs"))
    if len(set(cidrs)) != len(cidrs):
        raise ValueError("Duplicate network metadata CIDRs")
    domains = tuple(
        normalize_dns_name(item)
        for item in _texts(data["dns_domains"], "dns_domains", required=False)
    )
    subnets = tuple(_subnet(item) for item in _items(data["public_subnets"], "public_subnets"))
    if len({subnet.subnet_id for subnet in subnets}) != len(subnets):
        raise ValueError("Duplicate network metadata subnets")
    if any(not any(subnet.cidr.subnet_of(cidr) for cidr in cidrs) for subnet in subnets):
        raise ValueError("Public subnet lies outside its VPC CIDRs")
    try:
        policy = BastionPolicy(data["policy"])
    except (TypeError, ValueError) as error:
        raise ValueError("Invalid network metadata bastion policy") from error
    return VpcNetwork(
        _text(data["identity"], "identity"),
        account,
        _text(data["region"], "region"),
        _text(data["vpc_id"], "vpc_id"),
        _text(data["provider"], "provider"),
        cidrs,
        policy,
        domains,
        subnets,
        _access(data.get("access")),
    )


def _resource(data: dict) -> ResourceNetwork:
    _fields(
        data,
        {
            "version",
            "kind",
            "identity",
            "vpc",
            "service",
            "resource_id",
            "ports",
            "hostnames",
            "security_groups",
        },
    )
    if data["service"] != "documentdb":
        raise ValueError("Unsupported network metadata resource service")
    # Pulumi gRPC Struct encodes numbers as floats; accept integral values
    # while rejecting booleans, fractions and values outside service-port bounds.
    ports = _items(data["ports"], "ports")
    if any(
        type(port) not in (int, float) or not 1 <= port <= MAX_PORT or port != int(port)
        for port in ports
    ):
        raise ValueError("Invalid network metadata service port")
    if len(set(ports)) != len(ports):
        raise ValueError("Duplicate network metadata service ports")
    return ResourceNetwork(
        _text(data["identity"], "identity"),
        _text(data["vpc"], "vpc"),
        data["service"],
        _text(data["resource_id"], "resource_id"),
        tuple(int(port) for port in ports),
        tuple(normalize_dns_name(name) for name in _texts(data["hostnames"], "hostnames")),
        _texts(data["security_groups"], "security_groups"),
    )


def _endpoint(data: dict) -> EndpointNetwork:
    _fields(data, {"version", "kind", "identity", "endpoint_id", "vpcs"})
    return EndpointNetwork(
        _text(data["identity"], "identity"),
        _text(data["endpoint_id"], "endpoint_id"),
        _texts(data["vpcs"], "endpoint vpcs", required=False),
    )


def manifest_from_records(records: list[dict]) -> NetworkManifest:
    vpcs, resources, endpoints = [], [], []
    identities = set()
    parsers = {
        "vpc": (_vpc, vpcs),
        "resource": (_resource, resources),
        "endpoint": (_endpoint, endpoints),
    }
    for data in records:
        if (
            not isinstance(data, dict)
            or type(data.get("version")) not in (int, float)
            or data["version"] != VERSION
        ):
            raise ValueError(
                "Unsupported or missing network metadata version; "
                "redeploy with this Stelvio version"
            )
        kind = data.get("kind")
        if not isinstance(kind, str) or kind not in parsers:
            raise ValueError("Unsupported network metadata kind")
        parser, destination = parsers[kind]
        record = parser(data)
        if record.identity in identities:
            raise ValueError(f"Duplicate network identity {record.identity!r}")
        identities.add(record.identity)
        destination.append(record)
    manifest = NetworkManifest(tuple(vpcs), tuple(resources), tuple(endpoints))
    _validate_references(manifest)
    validate_network_ownership(manifest)
    return manifest


def _validate_references(manifest: NetworkManifest) -> None:
    by_id = {vpc.identity: vpc for vpc in manifest.vpcs}
    physical = {(vpc.account, vpc.region, vpc.vpc_id) for vpc in manifest.vpcs}
    if len(physical) != len(by_id):
        raise ValueError("Multiple components claim the same AWS VPC")
    resource_owners = {}
    for resource in manifest.resources:
        if resource.vpc not in by_id:
            raise ValueError(f"Resource {resource.identity!r} references an unknown VPC")
        network = by_id[resource.vpc]
        targets = [
            (network.account, network.region, resource.service, resource.resource_id),
            *(
                (network.account, network.region, "security-group", group)
                for group in resource.security_groups
            ),
        ]
        for target in targets:
            previous = resource_owners.setdefault(target, resource.vpc)
            if previous != resource.vpc:
                raise ValueError("A deployed resource is claimed by different VPCs")

    ids = [endpoint.endpoint_id for endpoint in manifest.endpoints]
    if len(set(ids)) != len(ids):
        raise ValueError("Duplicate invocation endpoint identity")
    for endpoint in manifest.endpoints:
        if any(vpc not in by_id for vpc in endpoint.vpcs):
            raise ValueError(f"Endpoint {endpoint.identity!r} references an unknown VPC")


def validate_network_ownership(manifest: NetworkManifest) -> None:
    enabled = manifest.enabled_vpcs
    for index, left in enumerate(enabled):
        for right in enabled[index + 1 :]:
            for first in left.cidrs:
                for second in right.cidrs:
                    if first.overlaps(second):
                        raise ValueError(
                            f"VPCs {left.identity!r} ({first}) and "
                            f"{right.identity!r} ({second}) overlap"
                        )
    claims = [(domain, vpc.identity) for vpc in enabled for domain in vpc.dns_domains]
    used = {vpc.identity for vpc in manifest.used_vpcs}
    claims.extend(
        (name, resource.vpc)
        for resource in manifest.resources
        if resource.vpc in used
        for name in resource.hostnames
    )
    for index, (domain, owner) in enumerate(claims):
        for other, other_owner in claims[index + 1 :]:
            if owner != other_owner and (
                domain_contains(domain, other) or domain_contains(other, domain)
            ):
                raise ValueError(
                    f"DNS ownership conflict: {domain!r} in VPC {owner!r} "
                    f"and {other!r} in VPC {other_owner!r}"
                )
