"""Deployment records shared by AWS components and laptop networking code.

This module imports no other Stelvio module. Components write only resource IDs;
the laptop reads current networking details from AWS before opening access.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from ipaddress import ip_address
from typing import final

VERSION = 1
OUTPUT_KEY = "_stelvio_network"
SSH_USER = "stlv-tunnel"
ACCOUNT = re.compile(r"[0-9]{12}\Z")
LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
MAX_DNS_NAME = 253


@final
@dataclass(frozen=True)
class VpcNetwork:
    identity: str
    account: str
    region: str
    vpc_id: str
    provider: str
    route_subnet_ids: tuple[str, ...]
    bastion_instance_id: str | None


@final
@dataclass(frozen=True)
class ResourceNetwork:
    identity: str
    vpc: str
    cluster_id: str


@final
@dataclass(frozen=True)
class NetworkManifest:
    vpcs: tuple[VpcNetwork, ...]
    resources: tuple[ResourceNetwork, ...]

    @property
    def enabled_vpcs(self) -> tuple[VpcNetwork, ...]:
        return tuple(vpc for vpc in self.vpcs if vpc.bastion_instance_id is not None)


def normalize_dns_name(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"DNS names must be strings, got {value!r}")
    name = value.removesuffix(".").lower()
    try:
        name = name.encode("idna").decode("ascii")
    except UnicodeError as error:
        raise ValueError(f"Invalid DNS name {value!r}: require valid DNS labels") from error
    if (
        not name
        or len(name) > MAX_DNS_NAME
        or not all(LABEL.fullmatch(label) for label in name.split("."))
    ):
        raise ValueError(
            f"Invalid DNS name {value!r}: require DNS labels without wildcards or whitespace"
        )
    try:
        ip_address(name)
    except ValueError:
        return name
    raise ValueError(f"Invalid DNS name {value!r}: IP addresses are not DNS names")


def _invalid(identity: str, field: str, reason: str) -> ValueError:
    return ValueError(
        f"Invalid network metadata for {identity!r}, field {field!r}: {reason}; "
        "redeploy the app with this Stelvio version"
    )


def _text(value: object, identity: str, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or any(char.isspace() or not char.isprintable() for char in value)
    ):
        raise _invalid(identity, field, "require a nonempty string without whitespace")
    return value


def _fields(
    data: dict, required: set[str], identity: str, optional: frozenset[str] = frozenset()
) -> None:
    missing = required - data.keys()
    unknown = data.keys() - required - optional
    if missing or unknown:
        reasons = []
        if missing:
            reasons.append(f"missing fields {sorted(missing)!r}")
        if unknown:
            reasons.append(f"unknown fields {sorted(unknown, key=repr)!r}")
        raise _invalid(identity, OUTPUT_KEY, "; ".join(reasons))


def _version(data: dict, identity: str) -> None:
    version = data.get("version")
    # Pulumi's RPC Struct represents integer output values as floats in mocks.
    valid_number = type(version) is int or (type(version) is float and version.is_integer())
    if valid_number and version == VERSION:
        return
    if valid_number and version > VERSION:
        raise ValueError(
            f"Invalid network metadata for {identity!r}, field 'version': "
            f"version {version!r} is newer than supported version {VERSION}; upgrade Stelvio"
        )
    raise _invalid(identity, "version", f"require version {VERSION}, got {version!r}")


def _vpc(data: dict, identity: str) -> VpcNetwork:
    _fields(
        data,
        {
            "version",
            "account",
            "region",
            "vpc_id",
            "provider",
            "route_subnet_ids",
        },
        identity,
        optional=frozenset({"bastion_instance_id"}),
    )
    account = _text(data["account"], identity, "account")
    if not ACCOUNT.fullmatch(account):
        raise _invalid(identity, "account", "require a 12-digit AWS account ID")
    subnet_ids = data["route_subnet_ids"]
    if not isinstance(subnet_ids, list):
        raise _invalid(identity, "route_subnet_ids", "require a list of subnet IDs")
    routes = tuple(_text(subnet, identity, "route_subnet_ids") for subnet in subnet_ids)
    if len(set(routes)) != len(routes):
        raise _invalid(identity, "route_subnet_ids", "duplicate subnet IDs")
    # Pulumi omits None-valued output properties from its serialized record.
    instance = data.get("bastion_instance_id")
    return VpcNetwork(
        identity,
        account,
        _text(data["region"], identity, "region"),
        _text(data["vpc_id"], identity, "vpc_id"),
        _text(data["provider"], identity, "provider"),
        routes,
        _text(instance, identity, "bastion_instance_id") if instance is not None else None,
    )


def _resource(data: dict, identity: str) -> ResourceNetwork:
    _fields(data, {"version", "vpc", "cluster_id"}, identity)
    return ResourceNetwork(
        identity,
        _text(data["vpc"], identity, "vpc"),
        _text(data["cluster_id"], identity, "cluster_id"),
    )


def manifest_from_records(records: list[tuple[str, str, object]]) -> NetworkManifest:
    """Parse (component type, component URN, output value) descriptors from state."""
    vpcs, resources = [], []
    identities = set()
    for component_type, identity, value in records:
        if identity in identities:
            raise _invalid(identity, "identity", "duplicate component URN")
        identities.add(identity)
        if not isinstance(value, dict):
            raise _invalid(identity, OUTPUT_KEY, "require a record object")
        _version(value, identity)
        if component_type == "stelvio:aws:Vpc":
            vpcs.append(_vpc(value, identity))
        elif component_type == "stelvio:aws:DocumentDb":
            resources.append(_resource(value, identity))
        else:
            raise _invalid(identity, OUTPUT_KEY, f"unsupported component type {component_type!r}")
    manifest = NetworkManifest(tuple(vpcs), tuple(resources))
    _validate_references(manifest)
    return manifest


def _claim(
    owners: dict[tuple[str, str, str, str], str],
    target: tuple[str, str, str, str],
    identity: str,
    field: str,
) -> None:
    previous = owners.setdefault(target, identity)
    if previous != identity:
        raise _invalid(identity, field, f"{target[-1]!r} is also claimed by {previous!r}")


def _validate_references(manifest: NetworkManifest) -> None:
    by_id = {vpc.identity: vpc for vpc in manifest.vpcs}
    owners: dict[tuple[str, str, str, str], str] = {}
    for vpc in manifest.vpcs:
        location = (vpc.account, vpc.region)
        _claim(owners, (*location, "vpc", vpc.vpc_id), vpc.identity, "vpc_id")
        for subnet in vpc.route_subnet_ids:
            _claim(owners, (*location, "subnet", subnet), vpc.identity, "route_subnet_ids")
        if vpc.bastion_instance_id is not None:
            _claim(
                owners,
                (*location, "instance", vpc.bastion_instance_id),
                vpc.identity,
                "bastion_instance_id",
            )
    for resource in manifest.resources:
        if resource.vpc not in by_id:
            raise _invalid(resource.identity, "vpc", f"unknown VPC component {resource.vpc!r}")
        owner = by_id[resource.vpc]
        _claim(
            owners,
            (owner.account, owner.region, "cluster", resource.cluster_id),
            resource.identity,
            "cluster_id",
        )
