from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal, TypedDict, final

from pulumi_aws import get_availability_zones, get_region
from pulumi_aws.ec2 import (
    InternetGateway,
    InternetGatewayArgs,
    NatGateway,
    NatGatewayArgs,
    RouteTable,
    RouteTableArgs,
    RouteTableAssociation,
    Subnet,
    VpcArgs,
)
from pulumi_aws.ec2 import Vpc as PulumiVpc

from stelvio import context
from stelvio.component import Component, safe_name


class SubnetType(StrEnum):
    PUBLIC = "public"
    PRIVATE = "private"
    ISOLATED = "isolated"


@final
@dataclass(frozen=True)
class VpcResources:
    vpc: PulumiVpc
    internet_gateway: InternetGateway
    public_subnets: list[Subnet]
    private_subnets: list[Subnet]
    isolated_subnets: list[Subnet]
    public_route_table: RouteTable
    private_route_table: RouteTable
    isolated_route_table: RouteTable
    nat_gateway: NatGateway | None = None


class VpcCustomizationDict(TypedDict, total=False):
    vpc: VpcArgs | dict[str, Any]
    internet_gateway: InternetGatewayArgs
    public_route_table: RouteTableArgs
    private_route_table: RouteTableArgs
    isolated_route_table: RouteTableArgs
    nat_gateway: NatGatewayArgs


@final
class Vpc(Component[VpcResources, VpcCustomizationDict]):
    """
    VPC component  will create:
    - VPC
    - 3 types of subnets: public, private (with egress if nat enabled), and isolated
    - we try with no global security group. so security group will have to be
        created each time resource is added to vpc.
    - for that we'll try to extend linking mechanism to also handle security groups
    - also each resource will be added to one of the subnet depending on what it is
    - but this also need to be able to be changed by user so extended vpc param
        where user can also choose subnet and potentially also security group
    - later we'll deal with importing existing stelvio groups so e.g. vpc can be
        shared between personal envs.
    - in later versions we need to be able to also import existing vpc created by
        other
    """

    _az: int | list[str]
    _nat: Literal["managed"] | None

    def __init__(
        self,
        name: str,
        /,
        az: int | list[str] = 2,
        nat: Literal["managed"] | None = None,
        *,
        tags: dict[str, str] | None = None,
        customize: VpcCustomizationDict | None = None,
    ):
        super().__init__("stelvio:aws:Vpc", name, tags=tags, customize=customize)
        self._az = az
        self._nat = nat

    def _create_resources(self) -> VpcResources:
        vpc_name = safe_name(context().prefix(), self.name, max_length=256, pulumi_suffix_length=0)
        vpc = PulumiVpc(
            vpc_name,
            **self._customizer(
                "vpc",
                {
                    "cidr_block": "10.0.0.0/16",
                    "enable_dns_support": True,
                    "enable_dns_hostnames": True,
                    "tags": {"Name": vpc_name},
                },
                inject_tags=True,
            ),
            opts=self._resource_opts(),
        )

        igw_name = safe_name(context().prefix(), self.name, 256, "-igw", pulumi_suffix_length=0)
        igw = InternetGateway(
            igw_name,
            **self._customizer(
                "internet_gateway",
                {
                    "vpc_id": vpc.id,
                    "tags": {"Name": igw_name},
                },
                inject_tags=True,
            ),
            opts=self._resource_opts(),
        )

        def create_route_table(subnet_type: SubnetType, routes: list[dict]) -> RouteTable:
            route_table_name = safe_name(
                context().prefix(), self.name, 256, f"-{subnet_type}-rt", pulumi_suffix_length=0
            )
            return RouteTable(
                route_table_name,
                **self._customizer(
                    f"{subnet_type}_route_table",
                    {"vpc_id": vpc.id, "routes": routes, "tags": {"Name": route_table_name}},
                    inject_tags=True,
                ),
                opts=self._resource_opts(),
            )

        # Public route table - has route to internet gateway
        public_rt = create_route_table(
            SubnetType.PUBLIC, [{"cidr_block": "0.0.0.0/0", "gateway_id": igw.id}]
        )
        # Private route table - will have NAT route added later if NAT enabled
        private_rt = create_route_table(SubnetType.PRIVATE, [])
        # Isolated route table - no routes
        isolated_rt = create_route_table(SubnetType.ISOLATED, [])

        available_azs = get_availability_zones(state="available")
        region_name = get_region().region
        azs = get_az_names(self._az, available_azs.names, region_name)
        # ok so we want to create three types of subnets:
        # public, private with egress and isolated
        subnets_configs = (
            # name, cidr prefix, subnet start, route table
            (SubnetType.PUBLIC, 24, 0, public_rt),
            (SubnetType.PRIVATE, 22, 20, private_rt),
            (SubnetType.ISOLATED, 24, 60, isolated_rt),
        )

        subnets_dict = defaultdict(list)
        for subnet_type, subnet_prefix, subnet_start, route_table in subnets_configs:
            for i, az in enumerate(azs):
                # Calculate a step by which the third octet of each new subnet of a type (public,
                # private, isolated) needs to increase.
                # For a /16 VPC where all subnets are between /17 and /24 we use 24 as that's where
                # third octet ends
                subnet_step = 2 ** (24 - subnet_prefix)
                subnet_third_octet = subnet_step * i + subnet_start
                subnet_name = safe_name(
                    context().prefix(),
                    self.name,
                    256,
                    f"-{subnet_type}-subnet-{az[-1]}",
                    pulumi_suffix_length=0,
                )
                # TODO: Consider how to support customization of subnets
                #       Currently not really possible unless we support functions
                #       for customization in addition to static dict
                subnet = Subnet(
                    subnet_name,
                    vpc_id=vpc.id,
                    cidr_block=f"10.0.{subnet_third_octet}.0/{subnet_prefix}",
                    availability_zone=az,
                    tags={"Name": subnet_name, "stelvio:subnet-type": subnet_type},
                    opts=self._resource_opts(),
                )
                subnets_dict[subnet_type].append(subnet)
                RouteTableAssociation(
                    f"{subnet_name}-rta",
                    subnet_id=subnet.id,
                    route_table_id=route_table.id,
                    opts=self._resource_opts(),
                )

        if self._nat == "managed":
            ...
        # TODO: add route to NAT if enabled
        # if nat_enabled:
        #     Route(
        #         f"{name}-private-nat-route",
        #         route_table_id=private_rt.id,
        #         destination_cidr_block="0.0.0.0/0",
        #         nat_gateway_id=nat_gw.id,
        #     )
        return VpcResources(
            vpc=vpc,
            internet_gateway=igw,
            public_subnets=subnets_dict[SubnetType.PUBLIC],
            private_subnets=subnets_dict[SubnetType.PRIVATE],
            isolated_subnets=subnets_dict[SubnetType.ISOLATED],
            public_route_table=public_rt,
            private_route_table=private_rt,
            isolated_route_table=isolated_rt,
        )


def get_az_names(
    az: int | list[str], available_azs_names: Sequence[str], region_name: str
) -> Sequence[str]:
    if isinstance(az, int):
        if az > len(available_azs_names):
            raise ValueError(
                f"Number of requested AZs in `az` parameter ({az}) is higher than "
                f"number of AZs ({len(available_azs_names)}) in the region '{region_name}'."
            )
        return available_azs_names[:az]

    if isinstance(az, list):
        for az_item in az:
            if az_item not in available_azs_names:
                raise ValueError(
                    f"Provided AZ name '{az_item}' does not exists in region '{region_name}'."
                )
        return az

    raise TypeError(f"`az` parameter must be `int` or `list[str]` got {type(az).__name__}")
