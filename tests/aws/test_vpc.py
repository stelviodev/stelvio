import re
from dataclasses import dataclass, field, replace
from typing import Any, Literal

import pulumi
from pulumi_aws.ec2 import VpcArgs
from pytest import mark, param, raises

from stelvio.aws.vpc import NatConfig, NatConfigDict, Vpc
from tests.aws.pulumi_mocks import TP, R, tid
from tests.test_utils import assert_config_dict_matches_dataclass


@mark.parametrize(
    ("az", "nat", "error_suffix"),
    [
        param(2, NatConfig(type="managed", ip=["1"]), "expected 2 (2 AZs)", id="two-azs-one-ip"),
        param(
            2,
            NatConfig(type="managed", ip=["1", "2"], single=True),
            "expected 1 (single NAT)",
            id="single-nat-two-ips",
        ),
        param(
            ["us-east-1a"],
            NatConfig(type="managed", ip=["1", "2"]),
            "expected 1 (1 AZs)",
            id="named-az-list-two-ips",
        ),
    ],
)
def test_vpc_raises_value_error_when_nat_ip_count_wrong(az, nat, error_suffix):
    error_start = "`nat.ip` must provide one Elastic IP allocation ID per NAT gateway: "
    with raises(ValueError, match=re.escape(error_start + error_suffix)):
        Vpc("main_vpc", az=az, nat=nat)


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


@mark.parametrize("nat", [param({"type": "ec2"}, id="dict"), param("ec2", id="string")])
def test_vpc_raises_value_error_when_nat_type_not_managed(nat):
    error = "Invalid NAT type 'ec2'. Only 'managed' is supported."
    with raises(ValueError, match=re.escape(error)):
        Vpc("main_vpc", nat=nat)


# az availability is checked against the region during deploy, not in __init__
@mark.parametrize(
    ("az", "error_message"),
    [
        param(
            4,
            "Number of requested AZs in `az` parameter (4) is higher than "
            "number of AZs (3) in the region 'us-east-1'.",
            id="int-too-high",
        ),
        param(
            ["us-east-1z"],
            "Provided AZ name 'us-east-1z' does not exist in region 'us-east-1'.",
            id="unknown-name",
        ),
    ],
)
def test_vpc_deploy_raises_value_error_when_az_unavailable(pulumi_mocks, az, error_message):
    @pulumi.runtime.test
    def deploy():
        return Vpc("main_vpc", az=az).resources

    with raises(ValueError, match=re.escape(error_message)):
        deploy()


def test_vpc_az_lookup_uses_resolved_region(pulumi_mocks, no_region_context):
    """Subnets land in the chain-resolved region's AZs when config has none.

    Scenario (the default new-user setup): no `region=` in @app.config, but the AWS
    chain resolves one (here AWS_REGION=eu-central-1 via the fixture). The original
    code asked the DEFAULT provider for AZs (and get_region() for messages); now the
    AZ lookup is scoped to the Vpc's own region — the mock answers with AZ names
    derived from the requested region, so the names below prove which region was asked.
    """

    @pulumi.runtime.test
    def deploy():
        return Vpc("main_vpc", az=2).resources

    deploy()

    subnets = pulumi_mocks.created(R.SUBNET)
    assert {s.inputs["availabilityZone"] for s in subnets} == {"eu-central-1a", "eu-central-1b"}


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

# adopted allocation ids pass through verbatim; no EIP resources created (eips stays [])
ADOPTED_IPS_TC = replace(
    DEFAULT_TC,
    test_id="nat-adopted-ips",
    nat=NatConfig(type="managed", ip=["eipalloc-user-a", "eipalloc-user-b"]),
    nats=[("a", "eipalloc-user-a"), ("b", "eipalloc-user-b")],
    routes=[("a", "a"), ("b", "b")],
)

SINGLE_NAT_ADOPTED_IP_TC = replace(
    DEFAULT_TC,
    test_id="single-nat-adopted-ip",
    nat=NatConfig(type="managed", single=True, ip=["eipalloc-user-1"]),
    nats=[("a", "eipalloc-user-1")],
    routes=[("a", "a"), ("b", "a")],
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

ONE_AZ_TC = replace(
    DEFAULT_TC,
    test_id="one-az",
    az=1,
    subnets=[
        ("public", "a", "10.0.0.0/24"),
        ("private", "a", "10.0.20.0/22"),
        ("isolated", "a", "10.0.60.0/24"),
    ],
)

THREE_AZ_TC = replace(DEFAULT_TC, test_id="three-az", az=3, subnets=THREE_AZ_SUBNETS)

# exactly the named AZs, in list order: "c" takes each tier's second cidr because it's
# second in the list — cidr assignment is positional, not derived from the AZ name
NAMED_AZS_TC = replace(
    DEFAULT_TC,
    test_id="named-azs",
    az=["us-east-1a", "us-east-1c"],
    subnets=[
        ("public", "a", "10.0.0.0/24"),
        ("public", "c", "10.0.1.0/24"),
        ("private", "a", "10.0.20.0/22"),
        ("private", "c", "10.0.24.0/22"),
        ("isolated", "a", "10.0.60.0/24"),
        ("isolated", "c", "10.0.61.0/24"),
    ],
)

# NAT wiring under named AZs: pairing is positional, so with ["us-east-1a","us-east-1c"]
# the second NAT must land in the "c" public subnet and the "c" private RT routes to it
NAMED_AZS_NAT_TC = replace(
    NAMED_AZS_TC,
    test_id="named-azs-nat",
    nat="managed",
    eips=["a", "c"],
    nats=[("a", nat_eip_allocation("a")), ("c", nat_eip_allocation("c"))],
    routes=[("a", "a"), ("c", "c")],
)

# based on NAT_TC so the merge is asserted on all taggable types incl. EIP + NAT;
# route table associations and routes are not taggable in AWS
TAGS_TC = replace(NAT_TC, test_id="tags", tags={"stage": "test", "team": "core"})

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
        replace(NAT_TC, test_id="nat-as-config", nat=NatConfig(type="managed")),
        replace(NAT_TC, test_id="nat-as-dict", nat={"type": "managed"}),
        SINGLE_NAT_TC,
        ADOPTED_IPS_TC,
        SINGLE_NAT_ADOPTED_IP_TC,
        ONE_AZ_TC,
        THREE_AZ_TC,
        NAMED_AZS_TC,
        NAMED_AZS_NAT_TC,
        TAGS_TC,
    ],
    ids=lambda tc: tc.test_id,
)
def test_vpc(pulumi_mocks, tc):
    # Deploy under the pulumi test runtime; the wrapper returns only after every
    # resource registration settled, so asserts below run as plain synchronous code.
    @pulumi.runtime.test
    def deploy():
        return Vpc("main_vpc", az=tc.az, nat=tc.nat, tags=tc.tags).resources

    deploy()

    verify_vpc(pulumi_mocks, tc)


# key → all instances of that resource kind; dict-form applies the same customization
# to every one of them (the documented same-for-all behavior; callable is per-instance)
CUSTOMIZE_KEY_RESOURCES = [
    ("vpc", R.VPC, ["main_vpc"]),
    ("internet_gateway", R.INTERNET_GATEWAY, ["main_vpc-igw"]),
    ("public_subnet", R.SUBNET, ["main_vpc-public-subnet-a", "main_vpc-public-subnet-b"]),
    ("private_subnet", R.SUBNET, ["main_vpc-private-subnet-a", "main_vpc-private-subnet-b"]),
    ("isolated_subnet", R.SUBNET, ["main_vpc-isolated-subnet-a", "main_vpc-isolated-subnet-b"]),
    (
        "public_route_table",
        R.ROUTE_TABLE,
        ["main_vpc-public-subnet-a-rt", "main_vpc-public-subnet-b-rt"],
    ),
    (
        "private_route_table",
        R.ROUTE_TABLE,
        ["main_vpc-private-subnet-a-rt", "main_vpc-private-subnet-b-rt"],
    ),
    (
        "isolated_route_table",
        R.ROUTE_TABLE,
        ["main_vpc-isolated-subnet-a-rt", "main_vpc-isolated-subnet-b-rt"],
    ),
    ("elastic_ip", R.EIP, ["main_vpc-nat-eip-a", "main_vpc-nat-eip-b"]),
    ("nat_gateway", R.NAT_GATEWAY, ["main_vpc-nat-a", "main_vpc-nat-b"]),
]


# Which resource(s) each customize key lands on is the contract here; merge semantics
# are owned by tests/test_component.py, hence partial asserts.
@mark.parametrize(
    ("key", "customization", "typ", "resource_names"),
    [
        *(
            param(key, {"tags": {"customized": key}}, typ, names, id=key)
            for key, typ, names in CUSTOMIZE_KEY_RESOURCES
        ),
        # Args-form: normalized to a dict of its set fields, then routed identically
        param("vpc", VpcArgs(tags={"customized": "vpc"}), R.VPC, ["main_vpc"], id="vpc-args"),
    ],
)
def test_vpc_customize_targets_resource(pulumi_mocks, key, customization, typ, resource_names):
    @pulumi.runtime.test
    def deploy():
        return Vpc("main_vpc", nat="managed", customize={key: customization}).resources

    deploy()

    # shallow merge replaces the whole tags dict → exact value proves it landed
    for name in resource_names:
        pulumi_mocks.assert_res(name, typ, {"tags": {"customized": key}}, partial=True)
    # ...and nowhere else: routing is exclusive to the targeted kind
    targeted = {TP + name for name in resource_names}
    for r in pulumi_mocks.created_resources:
        if r.name not in targeted:
            assert "customized" not in (r.inputs.get("tags") or {}), r.name


def test_vpc_customize_callable_receives_per_subnet_props(pulumi_mocks):
    seen = []

    def adjust_cidr(props: dict[str, Any]) -> dict[str, Any]:
        seen.append(props)
        # a user identifies the subnet from its props (az here; cidr or Name tag work too);
        # the return replaces the props wholesale, hence `props | {...}`
        if props["availability_zone"] == "us-east-1a":
            return props | {"cidr_block": "10.0.100.0/24"}
        return props

    @pulumi.runtime.test
    def deploy():
        return Vpc("main_vpc", customize={"public_subnet": adjust_cidr}).resources

    deploy()

    # called once per public subnet, each time with that subnet's own computed props
    assert [(p["availability_zone"], p["cidr_block"]) for p in seen] == [
        ("us-east-1a", "10.0.0.0/24"),
        ("us-east-1b", "10.0.1.0/24"),
    ]
    # and each subnet got its own result — the thing dict-form can't express
    pulumi_mocks.assert_res(
        "main_vpc-public-subnet-a", R.SUBNET, {"cidrBlock": "10.0.100.0/24"}, partial=True
    )
    pulumi_mocks.assert_res(
        "main_vpc-public-subnet-b", R.SUBNET, {"cidrBlock": "10.0.1.0/24"}, partial=True
    )


@mark.parametrize(
    ("nat", "eips", "nats"),
    [
        param(None, [], [], id="no-nat"),
        param(
            "managed",
            ["main_vpc-nat-eip-a", "main_vpc-nat-eip-b"],
            ["main_vpc-nat-a", "main_vpc-nat-b"],
            id="managed-nat",
        ),
        # adopted ips: nat_gateways populated, elastic_ips still empty
        param(
            NatConfig(type="managed", ip=["eipalloc-1", "eipalloc-2"]),
            [],
            ["main_vpc-nat-a", "main_vpc-nat-b"],
            id="adopted-ips",
        ),
    ],
)
@pulumi.runtime.test
def test_vpc_resources_exposes_created_resources(pulumi_mocks, nat, eips, nats):
    r = Vpc("main_vpc", nat=nat).resources

    exposed = [
        r.vpc,
        r.internet_gateway,
        *r.public_subnets,
        *r.private_subnets,
        *r.isolated_subnets,
        *r.public_route_tables,
        *r.private_route_tables,
        *r.isolated_route_tables,
        *r.elastic_ips,
        *r.nat_gateways,
    ]
    # in mocks a resource id derives from its logical name (tid), so comparing
    # ids pins identity, tier membership, and AZ order in one list
    expected = [
        "main_vpc",
        "main_vpc-igw",
        "main_vpc-public-subnet-a",
        "main_vpc-public-subnet-b",
        "main_vpc-private-subnet-a",
        "main_vpc-private-subnet-b",
        "main_vpc-isolated-subnet-a",
        "main_vpc-isolated-subnet-b",
        "main_vpc-public-subnet-a-rt",
        "main_vpc-public-subnet-b-rt",
        "main_vpc-private-subnet-a-rt",
        "main_vpc-private-subnet-b-rt",
        "main_vpc-isolated-subnet-a-rt",
        "main_vpc-isolated-subnet-b-rt",
        *eips,
        *nats,
    ]

    def check(ids):
        assert ids == [tid(TP + name) for name in expected]

    return pulumi.Output.all(*[res.id for res in exposed]).apply(check)


@pulumi.runtime.test
def test_vpc_resources_parented_to_vpc_component(pulumi_mocks):
    # `parent=self` lives in ResourceOptions (invisible to input mocks), but it
    # surfaces in each child's URN as the `stelvio:aws:Vpc$` segment.
    r = Vpc("main_vpc", nat="managed").resources
    children = [
        r.vpc,
        r.internet_gateway,
        *r.public_subnets,
        *r.private_subnets,
        *r.isolated_subnets,
        *r.public_route_tables,
        *r.private_route_tables,
        *r.isolated_route_tables,
        *r.elastic_ips,
        *r.nat_gateways,
    ]

    def check(urns):
        assert urns
        for urn in urns:
            assert "::stelvio:aws:Vpc$" in urn

    return pulumi.Output.all(*[res.urn for res in children]).apply(check)


def test_nat_config_dict_matches_dataclass():
    assert_config_dict_matches_dataclass(NatConfig, NatConfigDict)
