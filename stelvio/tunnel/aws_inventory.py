"""Read current VPC topology for only the deployment's recorded destinations."""

from __future__ import annotations

from ipaddress import IPv4Network
from typing import TYPE_CHECKING

from stelvio.tunnel.runtime_models import SubnetInventory, VpcInventory, validate_vpc_overlaps

if TYPE_CHECKING:
    from collections.abc import Callable

    from botocore.client import BaseClient

    from stelvio.network import NetworkManifest, VpcNetwork


class InventoryPendingError(ConnectionError):
    """AWS is still changing the recorded network; retry before admitting routes."""


def resolve_vpc_inventory(
    manifest: NetworkManifest, client_for: Callable[[VpcNetwork], BaseClient]
) -> dict[str, VpcInventory]:
    """Resolve every bastion through its captured provider, then check full VPC ranges."""
    resolved = {
        network.identity: resolve_vpc_network(network, client_for(network))
        for network in manifest.enabled_vpcs
    }
    validate_vpc_overlaps(resolved.values())
    return resolved


def resolve_vpc_network(network: VpcNetwork, client: BaseClient) -> VpcInventory:
    if client.meta.region_name != network.region:
        raise ValueError(f"VPC {network.identity!r} client has a different AWS region")
    vpcs = client.describe_vpcs(VpcIds=[network.vpc_id])["Vpcs"]
    if len(vpcs) != 1:
        raise ValueError(f"VPC {network.identity!r} is missing; redeploy before dev")
    actual = vpcs[0]
    if actual.get("VpcId") != network.vpc_id or actual.get("OwnerId") != network.account:
        raise ValueError(f"VPC {network.identity!r} has a different AWS owner")
    if actual.get("State") != "available":
        raise InventoryPendingError(f"VPC {network.identity!r} is not available; retry later")
    _validate_dns(network, client)
    associations = actual.get("CidrBlockAssociationSet", [])
    if any(item.get("CidrBlockState", {}).get("State") != "associated" for item in associations):
        raise InventoryPendingError(
            f"VPC {network.identity!r} has changing CIDR associations; retry later"
        )
    primary = IPv4Network(actual["CidrBlock"], strict=True)
    secondary = {IPv4Network(item["CidrBlock"], strict=True) for item in associations} - {primary}
    cidrs = (primary, *sorted(secondary))
    subnets = _route_subnets(network, client, cidrs)
    return VpcInventory(network, cidrs, subnets)


def _route_subnets(
    network: VpcNetwork, client: BaseClient, cidrs: tuple[IPv4Network, ...]
) -> tuple[SubnetInventory, ...]:
    expected = network.route_subnet_ids
    if len(set(expected)) != len(expected):
        raise ValueError(f"VPC {network.identity!r} has duplicate deployed subnet IDs")
    # An empty EC2 ID filter would enumerate unrelated subnets in the account.
    if not expected:
        return ()
    subnets = client.describe_subnets(SubnetIds=list(expected))["Subnets"]
    if len(subnets) != len(expected) or {subnet.get("SubnetId") for subnet in subnets} != set(
        expected
    ):
        raise ValueError(f"VPC {network.identity!r} subnet inventory changed; redeploy")
    resolved = {}
    for subnet in subnets:
        subnet_id = subnet["SubnetId"]
        az = subnet.get("AvailabilityZone")
        if (
            subnet.get("VpcId") != network.vpc_id
            or subnet.get("OwnerId") != network.account
            or not isinstance(az, str)
            or not az
        ):
            raise ValueError(
                f"VPC {network.identity!r} subnet ownership is invalid: {subnet_id!r}; redeploy"
            )
        if subnet.get("State") != "available":
            raise InventoryPendingError(
                f"VPC {network.identity!r} subnet {subnet_id!r} is not available; retry later"
            )
        cidr = IPv4Network(subnet["CidrBlock"], strict=True)
        if not any(cidr.subnet_of(owner) for owner in cidrs):
            raise ValueError(f"VPC {network.identity!r} subnet {subnet_id!r} is outside its CIDRs")
        if any(cidr.overlaps(previous.cidr) for previous in resolved.values()):
            raise ValueError(f"VPC {network.identity!r} destination subnet {subnet_id!r} overlaps")
        resolved[subnet_id] = SubnetInventory(subnet_id, az, cidr)
    return tuple(resolved[subnet_id] for subnet_id in expected)


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
