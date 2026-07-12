import re
from dataclasses import dataclass, field, replace
from typing import Any, Literal

import pulumi
from pytest import mark, param, raises

from stelvio.aws.vpc import NatConfig, NatConfigDict, Vpc
from tests.aws.pulumi_mocks import TP, R, tid


@mark.parametrize(
    ("nat", "error_suffix"),
    [
        param(NatConfig(ip=["1"]), "expected 2 (2 AZs)", id="two-azs-one-ip"),
        param(
            NatConfig(ip=["1", "2"], single=True),
            "expected 1 (single NAT)",
            id="single-nat-two-ips",
        ),
    ],
)
def test_vpc_raises_value_error_when_nat_ip_count_wrong(nat, error_suffix):
    error_start = "`nat.ip` must provide one Elastic IP allocation ID per NAT gateway: "
    with raises(ValueError, match=re.escape(error_start + error_suffix)):
        Vpc("main_vpc", nat=nat)


@mark.parametrize("nat", [param(42, id="int"), param(["managed"], id="list")])
def test_vpc_raises_type_error_when_nat_wrong_type(nat):
    error = "'nat' must be 'managed', a NatConfig, a dict, or None."
    with raises(TypeError, match=re.escape(error)):
        Vpc("main_vpc", nat=nat)


@mark.parametrize(
    ("az", "error_message"),
    [
        param(
            True,
            "`az` parameter must be `int` or `list[str]`, got bool",
            id="bool-true",
        ),
        param(
            False,
            "`az` parameter must be `int` or `list[str]`, got bool",
            id="bool-false",
        ),
        param(
            ["us-east-1a", 4],
            "When `az` is a list, each item must be a string, got 4",
            id="list-with-non-string",
        ),
        param(
            "us-east-1a",
            "`az` parameter must be `int` or `list[str]`, got",
            id="bare-string",
        ),
    ],
)
def test_vpc_raises_type_error_when_az_wrong_type(az, error_message):
    with raises(TypeError, match=re.escape(error_message)):
        Vpc("main_vpc", az=az)


@mark.parametrize(
    ("az", "error_message"),
    [
        param(0, "When `az` is a number it must be at least 1, got 0", id="zero"),
        param(-1, "When `az` is a number it must be at least 1, got -1", id="negative"),
        param(
            [],
            "When `az` is a list, you must provide at least one name.",
            id="empty-list",
        ),
        param(
            ["us-east-1a", "us-east-1a"],
            "`az` must not contain duplicate names, got",
            id="duplicates",
        ),
    ],
)
def test_vpc_raises_value_error_when_az_invalid(az, error_message):
    with raises(ValueError, match=re.escape(error_message)):
        Vpc("main_vpc", az=az)


@dataclass
class VpcTestCase:
    """A vpc config and the complete infrastructure expected from it.

    Expectations are literal values, never computed with production math — a case
    is a full spec: `verify_vpc` asserts everything it declares plus sealed counts.
    """

    test_id: str
    # inputs
    az: int | list[str] = 2
    nat: Literal["managed"] | NatConfig | NatConfigDict | None = None
    tags: dict[str, str] | None = None
    # expected: (type, az letter, cidr) per subnet
    subnets: list[tuple[str, str, str]] = field(default_factory=list)
    # expected: az letter per created EIP
    eips: list[str] = field(default_factory=list)
    # expected: (az letter, allocation id) per NAT gateway
    nats: list[tuple[str, str]] = field(default_factory=list)
    # expected: (private route table az letter, nat az letter) per NAT route
    routes: list[tuple[str, str]] = field(default_factory=list)


DEFAULT_TC = VpcTestCase(
    test_id="default",
    # 2 AZs x 3 tiers: public /24 from .0, private /22 from .20, isolated /24 from .60
    subnets=[
        ("public", "a", "10.0.0.0/24"),
        ("public", "b", "10.0.1.0/24"),
        ("private", "a", "10.0.20.0/22"),
        ("private", "b", "10.0.24.0/22"),
        ("isolated", "a", "10.0.60.0/24"),
        ("isolated", "b", "10.0.61.0/24"),
    ],
)


def nat_eip_allocation(az: str) -> str:
    """Expected allocation id of the EIP created for the NAT in `az` (see mocks' EIP outputs)."""
    return f"eipalloc-{tid(TP + f'main_vpc-nat-eip-{az}')}"


NAT_TC = replace(
    DEFAULT_TC,
    test_id="nat-per-az",
    nat="managed",
    eips=["a", "b"],
    nats=[("a", nat_eip_allocation("a")), ("b", nat_eip_allocation("b"))],
    routes=[("a", "a"), ("b", "b")],
)

THREE_AZ_SUBNETS = [
    ("public", "a", "10.0.0.0/24"),
    ("public", "b", "10.0.1.0/24"),
    ("public", "c", "10.0.2.0/24"),
    ("private", "a", "10.0.20.0/22"),
    ("private", "b", "10.0.24.0/22"),
    ("private", "c", "10.0.28.0/22"),
    ("isolated", "a", "10.0.60.0/24"),
    ("isolated", "b", "10.0.61.0/24"),
    ("isolated", "c", "10.0.62.0/24"),
]

# one shared NAT in the first AZ; every private route table routes to it
SINGLE_NAT_TC = replace(
    DEFAULT_TC,
    test_id="single-nat",
    az=3,
    nat={"type": "managed", "single": True},
    subnets=THREE_AZ_SUBNETS,
    eips=["a"],
    nats=[("a", nat_eip_allocation("a"))],
    routes=[("a", "a"), ("b", "a"), ("c", "a")],
)


def verify_vpc(pulumi_mocks, tc: VpcTestCase):
    user_tags = tc.tags or {}
    vpc_name = TP + "main_vpc"
    pulumi_mocks.assert_res(
        "main_vpc",
        R.VPC,
        {
            "cidrBlock": "10.0.0.0/16",
            "enableDnsSupport": True,
            "enableDnsHostnames": True,
            "tags": {"Name": vpc_name} | user_tags,
        },
    )
    pulumi_mocks.assert_res(
        "main_vpc-igw",
        R.INTERNET_GATEWAY,
        {
            "vpcId": tid(vpc_name),
            "tags": {"Name": f"{vpc_name}-igw"} | user_tags,
        },
    )
    for subnet_type, az, cidr in tc.subnets:
        subnet_name = f"main_vpc-{subnet_type}-subnet-{az}"
        pulumi_mocks.assert_res(
            subnet_name,
            R.SUBNET,
            {
                "vpcId": tid(vpc_name),
                "cidrBlock": cidr,
                "availabilityZone": f"us-east-1{az}",
                "tags": {"Name": TP + subnet_name, "stelvio:subnet-type": subnet_type} | user_tags,
            },
        )
        route_table_inputs: dict[str, Any] = {
            "vpcId": tid(vpc_name),
            "tags": {"Name": f"{TP}{subnet_name}-rt"} | user_tags,
        }
        if subnet_type == "public":
            route_table_inputs["routes"] = [
                {"cidrBlock": "0.0.0.0/0", "gatewayId": tid(f"{vpc_name}-igw")}
            ]
        pulumi_mocks.assert_res(f"{subnet_name}-rt", R.ROUTE_TABLE, route_table_inputs)
        pulumi_mocks.assert_res(
            f"{subnet_name}-rta",
            R.ROUTE_TABLE_ASSOCIATION,
            {
                "subnetId": tid(TP + subnet_name),
                "routeTableId": tid(f"{TP}{subnet_name}-rt"),
            },
        )
    for az in tc.eips:
        eip_name = f"main_vpc-nat-eip-{az}"
        pulumi_mocks.assert_res(
            eip_name,
            R.EIP,
            {"domain": "vpc", "tags": {"Name": TP + eip_name} | user_tags},
        )
    for az, allocation_id in tc.nats:
        nat_name = f"main_vpc-nat-{az}"
        pulumi_mocks.assert_res(
            nat_name,
            R.NAT_GATEWAY,
            {
                "subnetId": tid(TP + f"main_vpc-public-subnet-{az}"),
                "allocationId": allocation_id,
                "tags": {"Name": TP + nat_name} | user_tags,
            },
        )
    for rt_az, nat_az in tc.routes:
        pulumi_mocks.assert_res(
            f"main_vpc-nat-route-{rt_az}",
            R.ROUTE,
            {
                "routeTableId": tid(TP + f"main_vpc-private-subnet-{rt_az}-rt"),
                "destinationCidrBlock": "0.0.0.0/0",
                "natGatewayId": tid(TP + f"main_vpc-nat-{nat_az}"),
            },
        )
    # sealed: any resource beyond the declared expectations fails
    counts = {
        R.VPC: 1,
        R.INTERNET_GATEWAY: 1,
        R.SUBNET: len(tc.subnets),
        R.ROUTE_TABLE: len(tc.subnets),
        R.ROUTE_TABLE_ASSOCIATION: len(tc.subnets),
        R.EIP: len(tc.eips),
        R.NAT_GATEWAY: len(tc.nats),
        R.ROUTE: len(tc.routes),
    }
    pulumi_mocks.assert_res_counts({k: v for k, v in counts.items() if v})


@mark.parametrize(
    "tc",
    [
        DEFAULT_TC,
        NAT_TC,
        # same infra from every accepted nat form — normalization is the contract
        replace(NAT_TC, test_id="nat-as-config", nat=NatConfig()),
        replace(NAT_TC, test_id="nat-as-dict", nat={"type": "managed"}),
        SINGLE_NAT_TC,
    ],
    ids=lambda tc: tc.test_id,
)
def test_vpc__(pulumi_mocks, tc):
    # Deploy under the pulumi test runtime; the wrapper returns only after every
    # resource registration settled, so asserts below run as plain synchronous code.
    @pulumi.runtime.test
    def deploy():
        return Vpc("main_vpc", az=tc.az, nat=tc.nat, tags=tc.tags).resources

    deploy()

    verify_vpc(pulumi_mocks, tc)
