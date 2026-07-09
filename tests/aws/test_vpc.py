import re
from typing import Any

import pulumi
from pytest import mark, param, raises

from stelvio.aws.vpc import NatConfig, Vpc
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


# (type, az letter, cidr) for a default 2-AZ vpc: public /24 from .0, private /22 from .20,
# isolated /24 from .60
DEFAULT_SUBNETS = [
    ("public", "a", "10.0.0.0/24"),
    ("public", "b", "10.0.1.0/24"),
    ("private", "a", "10.0.20.0/22"),
    ("private", "b", "10.0.24.0/22"),
    ("isolated", "a", "10.0.60.0/24"),
    ("isolated", "b", "10.0.61.0/24"),
]


def test_vpc_default(pulumi_mocks):
    # Deploy under the pulumi test runtime; the wrapper returns only after every
    # resource registration settled, so asserts below run as plain synchronous code.
    @pulumi.runtime.test
    def deploy():
        return Vpc("main_vpc").resources

    deploy()

    vpc_name = TP + "main_vpc"
    pulumi_mocks.assert_res(
        "main_vpc",
        R.VPC,
        {
            "cidrBlock": "10.0.0.0/16",
            "enableDnsSupport": True,
            "enableDnsHostnames": True,
            "tags": {"Name": vpc_name},
        },
    )
    pulumi_mocks.assert_res(
        "main_vpc-igw",
        R.INTERNET_GATEWAY,
        {
            "vpcId": tid(vpc_name),
            "tags": {"Name": f"{vpc_name}-igw"},
        },
    )
    for subnet_type, az, cidr in DEFAULT_SUBNETS:
        subnet_name = f"main_vpc-{subnet_type}-subnet-{az}"
        pulumi_mocks.assert_res(
            subnet_name,
            R.SUBNET,
            {
                "vpcId": tid(vpc_name),
                "cidrBlock": cidr,
                "availabilityZone": f"us-east-1{az}",
                "tags": {"Name": TP + subnet_name, "stelvio:subnet-type": subnet_type},
            },
        )
        route_table_inputs: dict[str, Any] = {
            "vpcId": tid(vpc_name),
            "tags": {"Name": f"{TP}{subnet_name}-rt"},
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
    # no NAT infra by default
    pulumi_mocks.assert_no_res(R.NAT_GATEWAY, R.EIP)
    # nothing else created
    pulumi_mocks.assert_res_counts(
        {
            R.VPC: 1,
            R.INTERNET_GATEWAY: 1,
            R.SUBNET: 6,
            R.ROUTE_TABLE: 6,
            R.ROUTE_TABLE_ASSOCIATION: 6,
        }
    )
