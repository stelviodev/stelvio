from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, Literal, TypedDict, final

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
    SubnetArgs,
    VpcArgs,
)
from pulumi_aws.ec2 import Vpc as PulumiVpc

from stelvio import context
from stelvio.component import Component, safe_name


class SubnetType(StrEnum):
    PUBLIC = "public"
    PRIVATE = "private"
    ISOLATED = "isolated"


# cidr prefix, subnet start
SUBNETS_CONFIGS: Final[dict[SubnetType, tuple[int, int]]] = {
    SubnetType.PUBLIC: (24, 0),
    SubnetType.PRIVATE: (22, 20),
    SubnetType.ISOLATED: (24, 60),
}


@final
@dataclass(frozen=True)
class VpcResources:
    vpc: PulumiVpc
    internet_gateway: InternetGateway
    public_subnets: list[Subnet]
    private_subnets: list[Subnet]
    isolated_subnets: list[Subnet]
    public_route_tables: list[RouteTable]
    private_route_tables: list[RouteTable]
    isolated_route_tables: list[RouteTable]
    nat_gateway: NatGateway | None = None


class VpcCustomizationDict(TypedDict, total=False):
    vpc: VpcArgs | dict[str, Any]
    internet_gateway: InternetGatewayArgs | dict[str, Any]
    public_subnet: SubnetArgs | dict[str, Any]
    private_subnet: SubnetArgs | dict[str, Any]
    isolated_subnet: SubnetArgs | dict[str, Any]
    public_route_table: RouteTableArgs | dict[str, Any]
    private_route_table: RouteTableArgs | dict[str, Any]
    isolated_route_table: RouteTableArgs | dict[str, Any]
    nat_gateway: NatGatewayArgs | dict[str, Any]


def calculate_cidr(
    i: int, subnet_type: SubnetType, subnets_configs: dict[SubnetType, tuple[int, int]]
) -> str:
    subnet_prefix, subnet_start = subnets_configs[subnet_type]
    # Calculate a step by which the third octet of each new subnet of a type (public,
    # private, isolated) needs to increase.
    # For a /16 VPC where all subnets are between /17 and /24 we use 24 as that's where
    #  the third octet ends
    subnet_step = 2 ** (24 - subnet_prefix)
    subnet_third_octet = subnet_step * i + subnet_start
    return f"10.0.{subnet_third_octet}.0/{subnet_prefix}"


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

    # TODO: update comment above after implementation is finished
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
        validate_az(az)
        self._az = az
        self._nat = nat

    def _create_resources(self) -> VpcResources:
        vpc = self._create_vpc()
        igw = self._create_internet_gateway(vpc)

        subnets_dict, route_tables_dict = self._create_subnets_with_route_tables(vpc, igw)

        if self._nat == "managed":
            ...
        # TODO: Private route table will have NAT route added later if NAT enabled
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
            public_route_tables=route_tables_dict[SubnetType.PUBLIC],
            private_route_tables=route_tables_dict[SubnetType.PRIVATE],
            isolated_route_tables=route_tables_dict[SubnetType.ISOLATED],
        )

    def _create_subnets_with_route_tables(
        self, vpc: PulumiVpc, igw: InternetGateway
    ) -> tuple[dict[SubnetType, list[Subnet]], dict[SubnetType, list[RouteTable]]]:
        azs = get_az_names(self._az)

        subnets_dict = defaultdict(list)
        route_tables_dict = defaultdict(list)
        for subnet_type in SUBNETS_CONFIGS:
            for i, az in enumerate(azs):
                cidr_block = calculate_cidr(i, subnet_type, SUBNETS_CONFIGS)
                subnet, subnet_name = self._create_subnet(vpc, subnet_type, cidr_block, az)

                route_table = self._create_route_table_for_subnet(
                    vpc, igw, subnet, subnet_type, subnet_name
                )

                subnets_dict[subnet_type].append(subnet)
                route_tables_dict[subnet_type].append(route_table)
        return subnets_dict, route_tables_dict

    def _create_internet_gateway(self, vpc: PulumiVpc) -> InternetGateway:
        igw_name = safe_name(context().prefix(), self.name, 256, "-igw", pulumi_suffix_length=0)
        return InternetGateway(
            igw_name,
            **self._customizer(
                "internet_gateway",
                {"vpc_id": vpc.id, "tags": {"Name": igw_name}},
                inject_tags=True,
            ),
            opts=self._resource_opts(),
        )

    def _create_vpc(self) -> PulumiVpc:
        vpc_name = safe_name(context().prefix(), self.name, max_length=256, pulumi_suffix_length=0)
        default_props = {
            "cidr_block": "10.0.0.0/16",
            "enable_dns_support": True,
            "enable_dns_hostnames": True,
            "tags": {"Name": vpc_name},
        }
        customized_props = self._customizer("vpc", default_props, inject_tags=True)
        return PulumiVpc(vpc_name, **customized_props, opts=self._resource_opts())

    def _create_subnet(
        self, vpc: PulumiVpc, subnet_type: SubnetType, cidr_block: str, az: str
    ) -> tuple[Subnet, str]:
        subnet_name = safe_name(
            context().prefix(),
            self.name,
            256,
            f"-{subnet_type}-subnet-{az[-1]}",
            pulumi_suffix_length=0,
        )
        # TODO: Document that if you customize subnets or route tables
        #       with dict, all subnets/route tables get same config that
        #       you customized and in some cases e.g. cidr it will break
        #       deployment
        default_props = {
            "vpc_id": vpc.id,
            "cidr_block": cidr_block,
            "availability_zone": az,
            "tags": {"Name": subnet_name, "stelvio:subnet-type": subnet_type},
        }
        customized_props = self._customizer(
            f"{subnet_type}_subnet", default_props, inject_tags=True
        )
        subnet = Subnet(subnet_name, **customized_props, opts=self._resource_opts())
        return subnet, subnet_name

    def _create_route_table_for_subnet(
        self,
        vpc: PulumiVpc,
        igw: InternetGateway,
        subnet: Subnet,
        subnet_type: SubnetType,
        subnet_name: str,
    ) -> RouteTable:
        default_props = {"vpc_id": vpc.id, "tags": {"Name": f"{subnet_name}-rt"}}
        # Public route table - has route to internet gateway others don't,
        if subnet_type == SubnetType.PUBLIC:
            default_props |= {"routes": [{"cidr_block": "0.0.0.0/0", "gateway_id": igw.id}]}
        customized_props = self._customizer(
            f"{subnet_type}_route_table", default_props, inject_tags=True
        )
        route_table = RouteTable(
            f"{subnet_name}-rt", **customized_props, opts=self._resource_opts()
        )

        RouteTableAssociation(
            f"{subnet_name}-rta",
            subnet_id=subnet.id,
            route_table_id=route_table.id,
            opts=self._resource_opts(),
        )
        return route_table


def validate_az(az: int | list[str] | None) -> None:
    if isinstance(az, bool):
        raise TypeError(f"`az` parameter must be `int` or `list[str]`, got {type(az).__name__}")
    if isinstance(az, int):
        if az < 1:
            raise ValueError(f"When `az` is a number it must be at least 1, got {az}")
        return
    if isinstance(az, list):
        for az_item in az:
            if not isinstance(az_item, str):
                raise TypeError(f"When `az` is a list, each item must be a string, got {az_item}")
        if len(az) < 1:
            raise ValueError("When `az` is a list, you must provide at least one name.")
        if len(set(az)) != len(az):
            raise ValueError(f"`az` must not contain duplicate names, got {az}")
        return

    raise TypeError(f"`az` parameter must be `int` or `list[str]`, got {type(az).__name__}")


def get_az_names(az: int | list[str]) -> Sequence[str]:
    available_azs_names = get_availability_zones(state="available").names
    region_name = get_region().region
    if isinstance(az, int):
        if az > len(available_azs_names):
            raise ValueError(
                f"Number of requested AZs in `az` parameter ({az}) is higher than "
                f"number of AZs ({len(available_azs_names)}) in the region {region_name!r}."
            )
        return available_azs_names[:az]

    if isinstance(az, list):
        for az_item in az:
            if az_item not in available_azs_names:
                raise ValueError(
                    f"Provided AZ name {az_item!r} does not exist in region {region_name!r}."
                )
    return az
