from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from functools import cached_property
from typing import TYPE_CHECKING, Final, Literal, NamedTuple, TypedDict, final

from pulumi_aws import get_availability_zones
from pulumi_aws.ec2 import (
    DefaultSecurityGroup,
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
    SecurityGroup,
    Subnet,
    SubnetArgs,
    VpcArgs,
)
from pulumi_aws.ec2 import Vpc as PulumiVpc
from pulumi_aws.vpc import SecurityGroupEgressRule

from stelvio import context
from stelvio.component import Component, safe_name
from stelvio.provider import ProviderStore, aws_region_of

if TYPE_CHECKING:
    from pulumi import Input

    from stelvio.customize import Customization


class SubnetType(StrEnum):
    """Subnet tier: PUBLIC routes to the internet gateway, PRIVATE gets egress
    via NAT when enabled, ISOLATED has no internet route."""

    PUBLIC = "public"
    PRIVATE = "private"
    ISOLATED = "isolated"


VPC_NETWORK: Final = "10.0"  # /16; subnet tiers carve their 10.0.x.0 ranges from it
MAX_SECURITY_GROUPS: Final = 5  # AWS default per network interface, and Lambda's vpc_config cap


class SubnetLayout(NamedTuple):
    cidr_prefix: int  # subnet size: /24 public+isolated, /22 private
    third_octet_start: int  # where in 10.0.x.0 this tier's range begins


SUBNET_LAYOUTS: Final[dict[SubnetType, SubnetLayout]] = {
    SubnetType.PUBLIC: SubnetLayout(24, 0),
    SubnetType.PRIVATE: SubnetLayout(22, 20),
    SubnetType.ISOLATED: SubnetLayout(24, 60),
}


@dataclass(frozen=True, kw_only=True)
class NatConfig:
    """NAT configuration for private subnet internet access.

    Args:
        type: NAT implementation; only "managed" (AWS NAT Gateway) is supported.
        single: Create one shared NAT gateway instead of one per AZ (default: False).
        ip: Existing Elastic IP allocation IDs to adopt instead of creating EIPs —
            one per NAT gateway.
    """

    type: Literal["managed"]
    single: bool = False
    ip: list[str] | None = None

    def __post_init__(self) -> None:
        if self.type != "managed":
            raise ValueError(f"Invalid NAT type {self.type!r}. Only 'managed' is supported.")


class NatConfigDict(TypedDict, total=False):
    """Dict form of `NatConfig` — see it for field semantics."""

    type: Literal["managed"]
    single: bool
    ip: list[str]


@final
@dataclass(frozen=True)
class VpcResources:
    """Pulumi resources created by a Vpc.

    Subnet and route-table lists are AZ-ordered: index i belongs to the i-th AZ.
    `nat_gateways` is empty without `nat` and has one entry with `single=True`.
    `elastic_ips` is empty without `nat` or when adopting user-provided IPs via `nat.ip`.
    Route table associations and NAT routes are internal plumbing, not exposed.
    """

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
    """Customization keys for Vpc resources. Subnet and route-table keys apply to
    every subnet of that type across AZs — use a callable for per-subnet values."""

    vpc: Customization[VpcArgs]
    internet_gateway: Customization[InternetGatewayArgs]
    public_subnet: Customization[SubnetArgs]
    private_subnet: Customization[SubnetArgs]
    isolated_subnet: Customization[SubnetArgs]
    public_route_table: Customization[RouteTableArgs]
    private_route_table: Customization[RouteTableArgs]
    isolated_route_table: Customization[RouteTableArgs]
    elastic_ip: Customization[EipArgs]
    nat_gateway: Customization[NatGatewayArgs]


@final
class Vpc(Component[VpcResources, VpcCustomizationDict]):
    """VPC with public, private, and isolated subnet tiers across multiple AZs.

    Creates a 10.0.0.0/16 VPC with an internet gateway and, per AZ, one subnet
    of each tier with its own route table. Public subnets route to the internet
    gateway, isolated subnets have no internet route, and private subnets get
    egress only when `nat` is set — a managed NAT gateway per AZ, or one shared
    with `single=True`. `nat.ip` adopts existing Elastic IP allocation IDs
    instead of creating EIPs. Functions join with `Function(vpc=...)`; they share
    one app security group (no ingress, all egress) that the Vpc creates when the
    first function attaches.
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
        super().__init__(
            ProviderStore.aws(), "stelvio:aws:Vpc", name, tags=tags, customize=customize
        )
        _validate_az(az)
        self._az = az
        self._nat_config = _normalize_nat(nat)
        _validate_nat_config(self._nat_config, self._az)

    def _create_resources(self) -> VpcResources:
        azs = _get_az_names(self._az, aws_region_of(self))
        vpc = self._create_vpc()
        igw = self._create_internet_gateway(vpc)
        # Adopt the VPC's default security group and keep it empty: nothing in Stelvio
        # attaches to it, and a default group with rules is a CIS 5.4 finding. Adopting
        # strips AWS's allow-all rules on the first deploy.
        default_sg_name = self._safe_name("-default-sg")
        DefaultSecurityGroup(
            default_sg_name,
            vpc_id=vpc.id,
            tags={"Name": default_sg_name} | self.tags,
            opts=self._resource_opts(),
        )
        subnets_dict, route_tables_dict = self._create_subnets_with_route_tables(vpc, igw, azs)

        elastic_ips = []
        nat_gateways = []
        if self._nat_config:  # type == "managed" guaranteed by NatConfig.__post_init__
            elastic_ips, nat_gateways = self._create_managed_nats(
                self._nat_config, igw, azs, subnets_dict, route_tables_dict
            )

        self.register_outputs({})
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

    @cached_property
    def _app_security_group(self) -> SecurityGroup:
        # Shared security group every attached function wears; datastore components source
        # their one ingress rule from it. Made on first read (the first attaching function),
        # never from _create_resources: `self.resources` there would recurse.
        # AWS security group names are capped at 255, unlike the other Vpc children which
        # carry only a Name tag.
        sg_name = safe_name(context().prefix(), self.name, 255, "-app-sg")
        sg = SecurityGroup(
            sg_name,
            vpc_id=self.resources.vpc.id,
            description="Stelvio app tier: shared by functions attached to this VPC",
            tags={"Name": sg_name} | self.tags,
            opts=self._resource_opts(),
        )
        # No inline rules, ever: the provider drops AWS's default allow-all egress on
        # create, and inline rules can't coexist with standalone rule resources on one
        # group (the provider reverts one or the other). Datastores add standalone
        # ingress rules later, so egress is standalone too.
        SecurityGroupEgressRule(
            context().prefix(f"{self.name}-app-sg-egress"),
            security_group_id=sg.id,
            ip_protocol="-1",
            cidr_ipv4="0.0.0.0/0",
            tags=self.tags or None,
            opts=self._resource_opts(),
        )
        return sg

    def _create_vpc(self) -> PulumiVpc:
        vpc_name = self._safe_name()
        computed_props = {
            "cidr_block": f"{VPC_NETWORK}.0.0/16",
            "enable_dns_support": True,
            "enable_dns_hostnames": True,
            "tags": {"Name": vpc_name},
        }
        customized_props = self._customizer("vpc", computed_props, inject_tags=True)
        return PulumiVpc(vpc_name, **customized_props, opts=self._resource_opts())

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

    def _create_subnets_with_route_tables(
        self, vpc: PulumiVpc, igw: InternetGateway, azs: list[str]
    ) -> tuple[dict[SubnetType, list[Subnet]], dict[SubnetType, list[RouteTable]]]:
        subnets_dict = {t: [] for t in SubnetType}
        route_tables_dict = {t: [] for t in SubnetType}
        for subnet_type in SUBNET_LAYOUTS:
            for i, az in enumerate(azs):
                cidr_block = _calculate_cidr(i, SUBNET_LAYOUTS[subnet_type])
                subnet, subnet_name = self._create_subnet(vpc, subnet_type, cidr_block, az)

                route_table = self._create_and_associate_route_table(
                    vpc, igw, subnet, subnet_type, subnet_name
                )

                subnets_dict[subnet_type].append(subnet)
                route_tables_dict[subnet_type].append(route_table)
        return subnets_dict, route_tables_dict

    def _create_subnet(
        self, vpc: PulumiVpc, subnet_type: SubnetType, cidr_block: str, az: str
    ) -> tuple[Subnet, str]:
        subnet_name = self._safe_name(f"-{subnet_type}-subnet-{az[-1]}")
        computed_props = {
            "vpc_id": vpc.id,
            "cidr_block": cidr_block,
            "availability_zone": az,
            "tags": {"Name": subnet_name, "stelvio:subnet-type": subnet_type},
        }
        customized_props = self._customizer(
            f"{subnet_type}_subnet", computed_props, inject_tags=True
        )
        subnet = Subnet(subnet_name, **customized_props, opts=self._resource_opts())
        return subnet, subnet_name

    def _create_and_associate_route_table(
        self,
        vpc: PulumiVpc,
        igw: InternetGateway,
        subnet: Subnet,
        subnet_type: SubnetType,
        subnet_name: str,
    ) -> RouteTable:
        computed_props = {"vpc_id": vpc.id, "tags": {"Name": f"{subnet_name}-rt"}}
        # Public route table - has route to internet gateway others don't,
        if subnet_type == SubnetType.PUBLIC:
            computed_props |= {"routes": [{"cidr_block": "0.0.0.0/0", "gateway_id": igw.id}]}
        customized_props = self._customizer(
            f"{subnet_type}_route_table", computed_props, inject_tags=True
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

    def _create_managed_nats(
        self,
        nat_config: NatConfig,
        igw: InternetGateway,
        azs: list[str],
        subnets_dict: dict[SubnetType, list[Subnet]],
        route_tables_dict: dict[SubnetType, list[RouteTable]],
    ) -> tuple[list[Eip], list[NatGateway]]:
        elastic_ips = []
        nat_gateways = []

        # single NAT lives in the first AZ's public subnet
        nat_azs = azs[:1] if nat_config.single else azs
        # both AZ-ordered: zip pairs each NAT's AZ with its public subnet
        for i, (az, public_subnet) in enumerate(
            zip(nat_azs, subnets_dict[SubnetType.PUBLIC], strict=False)
        ):
            if nat_config.ip:  # adopt user-provided allocation, no EIP created
                eip_allocation_id = nat_config.ip[i]
            else:
                eip = self._create_eip(az)
                elastic_ips.append(eip)
                eip_allocation_id = eip.allocation_id
            nat = self._create_nat_gateway(igw, az, eip_allocation_id, public_subnet)
            nat_gateways.append(nat)

        # one default route per private route table; single NAT → all share it
        for i, private_rt in enumerate(route_tables_dict[SubnetType.PRIVATE]):
            nat = nat_gateways[0] if nat_config.single else nat_gateways[i]
            Route(
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
        computed_props = {
            "subnet_id": public_subnet.id,
            "allocation_id": eip_allocation_id,
            "tags": {"Name": nat_name},
        }
        customized_props = self._customizer("nat_gateway", computed_props, inject_tags=True)
        # NAT only routes once the IGW is attached; we depend on it so first deploy works
        # (also covers the adopted-`ip` case, which has no EIP to carry the dependency).
        return NatGateway(nat_name, **customized_props, opts=self._resource_opts(depends_on=[igw]))

    def _create_eip(self, az: str) -> Eip:
        eip_name = self._safe_name(f"-nat-eip-{az[-1]}")
        computed_props = {"domain": "vpc", "tags": {"Name": eip_name}}
        customized_props = self._customizer("elastic_ip", computed_props, inject_tags=True)
        return Eip(eip_name, **customized_props, opts=self._resource_opts())

    def _safe_name(self, suffix: str = "") -> str:
        # For resources that have no name in AWS we limit it to 256 so it fits into the tag value.
        return safe_name(context().prefix(), self.name, 256, suffix, pulumi_suffix_length=0)


@dataclass(frozen=True, kw_only=True)
class VpcAttachment:
    """How a function joins a `Vpc`. `Function(vpc=my_vpc)` means `VpcAttachment(vpc=my_vpc)`.

    Args:
        vpc: The Vpc to attach to.
        subnets: Subnet tier for the function's network interfaces: "private" (default,
            internet via the Vpc's NAT) or "isolated" (in-VPC only). "public" is rejected:
            Lambda network interfaces never get a public IP, so a public subnet leaves
            the function with no route out.
        security_groups: Existing security group IDs to attach with instead of the Vpc's
            shared app security group. Up to 5 (AWS limit). You wire their rules.
    """

    vpc: Vpc
    subnets: Literal["private", "isolated"] = "private"
    security_groups: list[str] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.vpc, Vpc):
            raise TypeError(f"'vpc' must be a Vpc, got {type(self.vpc).__name__}")
        if self.subnets not in ("private", "isolated"):
            raise ValueError(
                f"Invalid subnets {self.subnets!r}. Must be 'private' or 'isolated'. Lambda "
                "network interfaces never get a public IP, so a public subnet gives no "
                "internet access."
            )
        if self.security_groups is None:
            return
        if not isinstance(self.security_groups, list) or not all(
            isinstance(sg, str) for sg in self.security_groups
        ):
            raise TypeError(
                "'security_groups' must be a list of security group IDs (str), "
                f"got {self.security_groups!r}"
            )
        if not self.security_groups:
            raise ValueError(
                "'security_groups' cannot be empty. Omit it to use the Vpc's app security group."
            )
        if len(self.security_groups) > MAX_SECURITY_GROUPS:
            raise ValueError(
                f"'security_groups' can hold at most {MAX_SECURITY_GROUPS} IDs (AWS limit), "
                f"got {len(self.security_groups)}."
            )


class VpcAttachmentDict(TypedDict, total=False):
    """Dict form of `VpcAttachment` — see it for field semantics."""

    vpc: Vpc
    subnets: Literal["private", "isolated"]
    security_groups: list[str]


def normalize_vpc_attachment(
    vpc: Vpc | VpcAttachment | VpcAttachmentDict | None,
) -> VpcAttachment | None:
    """`Function(vpc=...)` takes a Vpc, a VpcAttachment, or its dict; readers get one shape."""
    if vpc is None:
        return None
    if isinstance(vpc, VpcAttachment):
        return vpc
    if isinstance(vpc, Vpc):
        return VpcAttachment(vpc=vpc)
    if isinstance(vpc, dict):
        try:
            return VpcAttachment(**vpc)
        except (TypeError, ValueError) as e:
            raise ValueError(f"Invalid vpc configuration: {e}") from e
    raise TypeError(
        f"'vpc' must be a Vpc, a VpcAttachment, a dict, or None. Got {type(vpc).__name__}"
    )


def _validate_az(az: int | list[str]) -> None:
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


def _get_az_names(az: int | list[str], region_name: str) -> list[str]:
    available_azs_names = list(get_availability_zones(state="available", region=region_name).names)
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

    raise TypeError(f"`az` parameter must be `int` or `list[str]`, got {type(az).__name__}")


def _calculate_cidr(az_index: int, layout: SubnetLayout) -> str:
    # Third octet step between same-tier subnets: /24 → step 1, /22 → step 4.
    step = 2 ** (24 - layout.cidr_prefix)
    return f"{VPC_NETWORK}.{layout.third_octet_start + az_index * step}.0/{layout.cidr_prefix}"


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
    raise TypeError(
        f"'nat' must be 'managed', a NatConfig, a dict, or None. Got {type(nat).__name__}"
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
