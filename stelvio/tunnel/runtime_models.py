"""AWS inventory used only by the laptop runtime, never written to deployment state."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, final

if TYPE_CHECKING:
    from collections.abc import Iterable
    from ipaddress import IPv4Network

    from stelvio.network import ResourceNetwork, VpcNetwork


@final
@dataclass(frozen=True)
class SubnetInventory:
    subnet_id: str
    availability_zone: str
    cidr: IPv4Network


@final
@dataclass(frozen=True)
class VpcInventory:
    record: VpcNetwork
    cidrs: tuple[IPv4Network, ...]
    route_subnets: tuple[SubnetInventory, ...]

    @property
    def identity(self) -> str:
        return self.record.identity

    @property
    def route_cidrs(self) -> tuple[IPv4Network, ...]:
        return tuple(subnet.cidr for subnet in self.route_subnets)


@final
@dataclass(frozen=True)
class BastionInventory:
    instance_id: str
    availability_zone: str
    security_group_ids: tuple[str, ...]
    subnet_id: str


@final
@dataclass(frozen=True)
class DiscoveredDocumentDb:
    record: ResourceNetwork
    port: int
    hostnames: tuple[str, ...]

    @property
    def identity(self) -> str:
        return self.record.identity

    @property
    def vpc(self) -> str:
        return self.record.vpc


def validate_vpc_overlaps(inventories: Iterable[VpcInventory]) -> None:
    networks = tuple(inventories)
    for index, left in enumerate(networks):
        for right in networks[index + 1 :]:
            for first in left.cidrs:
                for second in right.cidrs:
                    if first.overlaps(second):
                        raise ValueError(
                            f"VPCs {left.identity!r} ({first}) and "
                            f"{right.identity!r} ({second}) overlap"
                        )


def validate_dns_ownership(resources: Iterable[DiscoveredDocumentDb]) -> None:
    owners = {}
    for resource in resources:
        for hostname in resource.hostnames:
            previous = owners.setdefault(hostname, resource.vpc)
            if previous != resource.vpc:
                raise ValueError(
                    f"DNS ownership conflict: {hostname!r} in VPCs "
                    f"{previous!r} and {resource.vpc!r}"
                )
