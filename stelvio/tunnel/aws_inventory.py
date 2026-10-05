"""Resolve complete deployed IPv4 ranges before a session changes host routes."""

from __future__ import annotations

from dataclasses import replace
from ipaddress import IPv4Network
from typing import TYPE_CHECKING

from stelvio.tunnel.manifest import NetworkManifest, validate_network_ownership

if TYPE_CHECKING:
    from collections.abc import Callable

    from botocore.client import BaseClient

    from stelvio.tunnel.manifest import VpcNetwork


def resolve_vpc_inventory(
    manifest: NetworkManifest, client_for: Callable[[VpcNetwork], BaseClient]
) -> NetworkManifest:
    """Use captured provider clients; never create a session from handler env vars.

    The component's primary CIDR cannot inventory independently associated IPv4
    ranges. Query used VPCs, retain all associated ranges, then validate overlap
    across the full collection before any host operation.
    """
    resolved = {
        network.identity: resolve_vpc_network(network, client_for(network))
        for network in manifest.enabled_vpcs
    }
    result = replace(
        manifest, vpcs=tuple(resolved.get(network.identity, network) for network in manifest.vpcs)
    )
    validate_network_ownership(result)
    return result


def resolve_vpc_network(network: VpcNetwork, client: BaseClient) -> VpcNetwork:
    if client.meta.region_name != network.region:
        raise ValueError(f"VPC {network.identity!r} client has a different AWS region")
    vpcs = client.describe_vpcs(VpcIds=[network.vpc_id])["Vpcs"]
    if len(vpcs) != 1:
        raise ValueError(f"VPC {network.identity!r} is missing; redeploy before dev")
    actual = vpcs[0]
    if actual["VpcId"] != network.vpc_id or actual["OwnerId"] != network.account:
        raise ValueError(f"VPC {network.identity!r} has a different AWS owner")
    if actual["State"] != "available":
        raise ValueError(f"VPC {network.identity!r} is not available")
    associations = actual.get("CidrBlockAssociationSet", [])
    if any(item["CidrBlockState"]["State"] != "associated" for item in associations):
        raise ValueError(f"VPC {network.identity!r} has changing CIDR associations; retry later")
    cidrs = tuple(
        dict.fromkeys(
            IPv4Network(value, strict=True)
            for value in [actual["CidrBlock"], *(item["CidrBlock"] for item in associations)]
        )
    )
    if actual["CidrBlock"] != str(network.cidrs[0]):
        raise ValueError(f"VPC {network.identity!r} primary CIDR changed; redeploy before dev")
    subnets = client.describe_subnets(
        SubnetIds=[subnet.subnet_id for subnet in network.public_subnets]
    )["Subnets"]
    expected = {subnet.subnet_id: subnet for subnet in network.public_subnets}
    if {subnet["SubnetId"] for subnet in subnets} != set(expected):
        raise ValueError(f"VPC {network.identity!r} public subnet inventory changed")
    for subnet in subnets:
        known = expected[subnet["SubnetId"]]
        if (
            subnet["VpcId"] != network.vpc_id
            or subnet["OwnerId"] != network.account
            or subnet["AvailabilityZone"] != known.availability_zone
            or subnet["CidrBlock"] != str(known.cidr)
            or subnet["State"] != "available"
        ):
            raise ValueError(f"VPC {network.identity!r} public subnet identity changed")
    return replace(network, cidrs=cidrs)
