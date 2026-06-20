from collections import defaultdict
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, Literal, TypedDict, final

from pulumi import Input
from pulumi_aws import get_availability_zones, get_region
from pulumi_aws.ec2 import (
    Eip,
    EipArgs,
    InternetGateway,
    InternetGatewayArgs,
    NatGateway,
    NatGatewayArgs,
    Route,
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


@dataclass(frozen=True)
class NatConfig:
    type: Literal["managed"] = "managed"
    single: bool = False
    ip: list[str] | None = None

    def __post_init__(self) -> None:
        if self.type != "managed":
            raise ValueError(f"Invalid NAT type {self.type!r}. Only 'managed' is supported.")


class NatConfigDict(TypedDict, total=False):
    type: Literal["managed"]
    single: bool
    ip: list[str]


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
    elastic_ips: list[Eip]
    nat_gateways: list[NatGateway]


class VpcCustomizationDict(TypedDict, total=False):
    vpc: VpcArgs | dict[str, Any]
    internet_gateway: InternetGatewayArgs | dict[str, Any]
    public_subnet: SubnetArgs | dict[str, Any]
    private_subnet: SubnetArgs | dict[str, Any]
    isolated_subnet: SubnetArgs | dict[str, Any]
    public_route_table: RouteTableArgs | dict[str, Any]
    private_route_table: RouteTableArgs | dict[str, Any]
    isolated_route_table: RouteTableArgs | dict[str, Any]
    elastic_ip: EipArgs | dict[str, Any]
    nat_gateway: NatGatewayArgs | dict[str, Any]


@final
class Vpc(Component[VpcResources, VpcCustomizationDict]):
    """
    VPC component will create:
    - VPC
    - 3 types of subnets: public, private (with egress if nat enabled), and isolated
    - we try with no global security group. so a security group will have to be
        created each time a resource is added to vpc.
    - for that we'll try to extend the linking mechanism to also handle security groups
    - also each resource will be added to one of the subnets depending on what it is
    - but this also needs to be able to be changed by user so extended vpc param
        where user can also choose subnet and potentially also security group
    - later we'll deal with importing existing stelvio groups so e.g., vpc can be
        shared between personal envs.
    - in later versions we need to be able to also import existing vpc created by
        other
    """

    _az: int | list[str]
    _nat_config: NatConfig | None

    def __init__(
        self,
        name: str,
        /,
        az: int | list[str] = 2,
        nat: Literal["managed"] | NatConfig | NatConfigDict | None = None,
        *,
        tags: dict[str, str] | None = None,
        customize: VpcCustomizationDict | None = None,
    ):
        super().__init__("stelvio:aws:Vpc", name, tags=tags, customize=customize)
        _validate_az(az)
        self._az = az
        self._nat_config = _normalize_nat(nat)
        _validate_nat_config(self._nat_config, self._az)

    def _create_resources(self) -> VpcResources:
        vpc = self._create_vpc()
        igw = self._create_internet_gateway(vpc)
        azs = _get_az_names(self._az)
        subnets_dict, route_tables_dict = self._create_subnets_with_route_tables(vpc, igw, azs)

        elastic_ips = []
        nat_gateways = []
        if self._nat_config and self._nat_config.type == "managed":
            elastic_ips, nat_gateways = self._create_managed_nats(
                igw, azs, subnets_dict, route_tables_dict
            )

        return VpcResources(
            vpc=vpc,
            internet_gateway=igw,
            public_subnets=subnets_dict[SubnetType.PUBLIC],
            private_subnets=subnets_dict[SubnetType.PRIVATE],
            isolated_subnets=subnets_dict[SubnetType.ISOLATED],
            public_route_tables=route_tables_dict[SubnetType.PUBLIC],
            private_route_tables=route_tables_dict[SubnetType.PRIVATE],
            isolated_route_tables=route_tables_dict[SubnetType.ISOLATED],
            elastic_ips=elastic_ips,
            nat_gateways=nat_gateways,
        )

    def _safe_name(self, suffix: str = "") -> str:
        # For resources that have no name in AWS we limit it to 256 so it fits into the tag value.
        return safe_name(context().prefix(), self.name, 256, suffix, pulumi_suffix_length=0)

    def _create_managed_nats(
        self,
        igw: InternetGateway,
        azs: list[str],
        subnets_dict: dict[SubnetType, list[Subnet]],
        route_tables_dict: dict[SubnetType, list[RouteTable]],
    ) -> tuple[list[Eip], list[NatGateway]]:
        if self._nat_config is None:
            return [], []

        elastic_ips = []
        nat_gateways = []

        # if single create list of one az - first one
        nat_azs = azs[:1] if self._nat_config.single else azs
        public_subnets = subnets_dict[SubnetType.PUBLIC]
        for i, az in enumerate(nat_azs):
            # Use user-provided ip if supplied
            if self._nat_config.ip:
                eip_allocation_id = self._nat_config.ip[i]
            else:
                eip = self._create_eip(az)
                elastic_ips.append(eip)
                eip_allocation_id = eip.allocation_id

            # public_subnets is AZ-ordered, so index i aligns with az[i]
            public_subnet = public_subnets[i]

            nat = self._create_nat_gateway(igw, az, eip_allocation_id, public_subnet)
            nat_gateways.append(nat)

        # Now update route tables of all private subnets
        private_route_tables = route_tables_dict[SubnetType.PRIVATE]
        for i, private_rt in enumerate(private_route_tables):
            # Count of private route tables same as nat gateways if not single
            nat = nat_gateways[0] if self._nat_config.single else nat_gateways[i]
            Route(
                # Count of private route tables same as azs
                context().prefix(f"{self.name}-nat-route-{azs[i][-1]}"),
                route_table_id=private_rt.id,
                destination_cidr_block="0.0.0.0/0",
                nat_gateway_id=nat.id,
                opts=self._resource_opts(),
            )

        return elastic_ips, nat_gateways

    def _create_nat_gateway(
        self, igw: InternetGateway, az: str, eip_allocation_id: Input[str], public_subnet: Subnet
    ) -> NatGateway:
        nat_name = self._safe_name(f"-nat-{az[-1]}")
        default_props = {
            "subnet_id": public_subnet.id,
            "allocation_id": eip_allocation_id,
            "tags": {"Name": nat_name},
        }
        customized_props = self._customizer("nat_gateway", default_props, inject_tags=True)
        # NAT only routes once the IGW is attached; we depend on it so first deploy works
        # (also covers the adopted-`ip` case, which has no EIP to carry the dependency).
        return NatGateway(nat_name, **customized_props, opts=self._resource_opts(depends_on=[igw]))

    def _create_eip(self, az: str) -> Eip:
        eip_name = self._safe_name(f"-nat-eip-{az[-1]}")
        default_props = {"domain": "vpc", "tags": {"Name": eip_name}}
        customized_props = self._customizer("elastic_ip", default_props, inject_tags=True)
        return Eip(eip_name, **customized_props, opts=self._resource_opts())

    def _create_subnets_with_route_tables(
        self, vpc: PulumiVpc, igw: InternetGateway, azs: list[str]
    ) -> tuple[dict[SubnetType, list[Subnet]], dict[SubnetType, list[RouteTable]]]:
        subnets_dict = defaultdict(list)
        route_tables_dict = defaultdict(list)
        for subnet_type in SUBNETS_CONFIGS:
            for i, az in enumerate(azs):
                cidr_block = _calculate_cidr(i, subnet_type, SUBNETS_CONFIGS)
                subnet, subnet_name = self._create_subnet(vpc, subnet_type, cidr_block, az)

                route_table = self._create_route_table_for_subnet(
                    vpc, igw, subnet, subnet_type, subnet_name
                )

                subnets_dict[subnet_type].append(subnet)
                route_tables_dict[subnet_type].append(route_table)
        return subnets_dict, route_tables_dict

    def _create_internet_gateway(self, vpc: PulumiVpc) -> InternetGateway:
        igw_name = self._safe_name("-igw")
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
        vpc_name = self._safe_name()
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
        subnet_name = self._safe_name(f"-{subnet_type}-subnet-{az[-1]}")
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


def _validate_az(az: int | list[str] | None) -> None:
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


def _get_az_names(az: int | list[str]) -> list[str]:
    available_azs_names = list(get_availability_zones(state="available").names)
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


def _calculate_cidr(
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


def _normalize_nat(
    nat: Literal["managed"] | NatConfig | NatConfigDict | None = None,
) -> NatConfig | None:
    if nat is None:
        return None
    if isinstance(nat, NatConfig):
        return nat
    if isinstance(nat, str):
        return NatConfig(type=nat)
    if isinstance(nat, dict):
        return NatConfig(**nat)
    raise ValueError(
        f"'nat' must be 'managed', a NatConfig, a dict, or None. Got {type(nat).__name__} "
    )


def _validate_nat_config(nat_config: NatConfig | None, az: int | list[str]) -> None:
    if nat_config is None:
        return
    if nat_config.ip is None:
        return

    az_count = az if isinstance(az, int) else len(az)
    nat_count = 1 if nat_config.single else az_count
    if len(nat_config.ip) != nat_count:
        raise ValueError(
            f"`nat.ip` must provide one Elastic IP allocation ID per NAT gateway: "
            f"expected {nat_count} ({'single NAT' if nat_config.single else f'{az_count} AZs'})"
        )
