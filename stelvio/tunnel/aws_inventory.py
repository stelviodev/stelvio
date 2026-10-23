"""Verify VPC ownership and every declared subnet before selecting route destinations."""

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
    across the full collection before any host operation. Verify public, private,
    and isolated subnet identities against AWS. route_cidrs remains the declared
    private/isolated set; associated ownership blocks never add destinations.
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
    if actual["CidrBlock"] != str(network.cidrs[0]):
        raise ValueError(f"VPC {network.identity!r} primary CIDR changed; redeploy before dev")
    _validate_dns(network, client)
    associations = actual.get("CidrBlockAssociationSet", [])
    if any(item["CidrBlockState"]["State"] != "associated" for item in associations):
        raise ValueError(f"VPC {network.identity!r} has changing CIDR associations; retry later")
    cidrs = tuple(
        dict.fromkeys(
            IPv4Network(value, strict=True)
            for value in [actual["CidrBlock"], *(item["CidrBlock"] for item in associations)]
        )
    )
    _validate_subnets(network, client, cidrs)
    return replace(network, cidrs=cidrs)


def _validate_subnets(
    network: VpcNetwork, client: BaseClient, cidrs: tuple[IPv4Network, ...]
) -> None:
    # Classification comes from deployed metadata, not public-IP assignment or
    # response order. Never enumerate arbitrary AWS subnets if the ID list is empty.
    expected = {subnet.subnet_id: subnet for subnet in network.subnets}
    if not expected or len(expected) != len(network.subnets):
        raise ValueError(f"VPC {network.identity!r} has an invalid deployed subnet inventory")
    subnets = client.describe_subnets(SubnetIds=list(expected))["Subnets"]
    if len(subnets) != len(expected) or {subnet.get("SubnetId") for subnet in subnets} != set(
        expected
    ):
        raise ValueError(f"VPC {network.identity!r} subnet inventory changed; redeploy")
    for subnet in subnets:
        known = expected[subnet["SubnetId"]]
        if (
            subnet.get("VpcId") != network.vpc_id
            or subnet.get("OwnerId") != network.account
            or subnet.get("AvailabilityZone") != known.availability_zone
            or subnet.get("CidrBlock") != str(known.cidr)
            or subnet.get("State") != "available"
            or not any(known.cidr.subnet_of(cidr) for cidr in cidrs)
        ):
            raise ValueError(
                f"VPC {network.identity!r} subnet identity changed: {known.subnet_id!r}; redeploy"
            )


def _validate_dns(network: VpcNetwork, client: BaseClient) -> None:
    for attribute, field in (
        ("enableDnsSupport", "EnableDnsSupport"),
        ("enableDnsHostnames", "EnableDnsHostnames"),
    ):
        response = client.describe_vpc_attribute(VpcId=network.vpc_id, Attribute=attribute)
        if (
            response.get("VpcId") != network.vpc_id
            or response.get(field, {}).get("Value") is not True
        ):
            raise ValueError(
                f"VPC {network.identity!r} requires {attribute}=true for private DNS; "
                "correct its DNS configuration before dev"
            )
