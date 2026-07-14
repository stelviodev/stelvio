import pytest
from pytest import param

from stelvio.aws.vpc import NatConfig, Vpc

from .assert_vpc import (
    assert_default_route,
    assert_ec2_tags,
    assert_nat_gateway,
    assert_subnets,
    assert_vpc,
    get_default_route,
    get_subnets,
)
from .export_helpers import export_vpc

pytestmark = pytest.mark.integration


def test_vpc_default(stelvio_env):
    def infra():
        export_vpc(Vpc("net", az=2))

    outputs = stelvio_env.deploy(infra)

    assert_vpc(outputs["vpc_net_id"])

    assert_subnets(
        outputs["vpc_net_public_subnet_ids"],
        subnet_type="public",
        cidrs=["10.0.0.0/24", "10.0.1.0/24"],
    )
    assert_subnets(
        outputs["vpc_net_private_subnet_ids"],
        subnet_type="private",
        cidrs=["10.0.20.0/22", "10.0.24.0/22"],
    )
    assert_subnets(
        outputs["vpc_net_isolated_subnet_ids"],
        subnet_type="isolated",
        cidrs=["10.0.60.0/24", "10.0.61.0/24"],
    )

    # public routes to the internet gateway; private + isolated have no egress (no NAT)
    for rt_id in outputs["vpc_net_public_route_table_ids"]:
        assert_default_route(rt_id, gateway_id=outputs["vpc_net_igw_id"])
    for rt_id in (
        outputs["vpc_net_private_route_table_ids"] + outputs["vpc_net_isolated_route_table_ids"]
    ):
        assert get_default_route(rt_id) is None


@pytest.mark.parametrize(
    ("nat", "nat_count"),
    [
        param("managed", 2, id="per-az"),
        param(NatConfig(type="managed", single=True), 1, id="single"),
    ],
)
def test_vpc_managed_nat(stelvio_env, nat, nat_count):
    def infra():
        export_vpc(Vpc("net", az=2, nat=nat))

    outputs = stelvio_env.deploy(infra)

    nat_ids = outputs["vpc_net_nat_gateway_ids"]
    eip_ids = outputs["vpc_net_eip_allocation_ids"]
    public_subnet_ids = outputs["vpc_net_public_subnet_ids"]

    assert len(nat_ids) == nat_count
    assert len(eip_ids) == nat_count

    # each NAT is available in its AZ's public subnet, bound to its EIP
    for nat_id, subnet_id, eip_id in zip(
        nat_ids, public_subnet_ids[:nat_count], eip_ids, strict=True
    ):
        assert_nat_gateway(nat_id, subnet_id=subnet_id, allocation_id=eip_id)

    # per-az: private RT i egresses via NAT i; single: every private RT via the one NAT
    for i, rt_id in enumerate(outputs["vpc_net_private_route_table_ids"]):
        assert_default_route(rt_id, nat_gateway_id=nat_ids[i % nat_count])


def test_vpc_tags(stelvio_env):
    def infra():
        export_vpc(Vpc("net", az=1, tags={"Team": "platform"}))

    outputs = stelvio_env.deploy(infra)

    prefix = f"stlv-{stelvio_env.run_id}-test-"
    common = {
        "stelvio:app": f"stlv-{stelvio_env.run_id}",
        "stelvio:env": "test",
        "Team": "platform",
    }

    assert_ec2_tags(outputs["vpc_net_id"], {**common, "Name": f"{prefix}net"})

    subnet_id = outputs["vpc_net_public_subnet_ids"][0]
    az = get_subnets([subnet_id])[0]["AvailabilityZone"]
    assert_ec2_tags(
        subnet_id,
        {**common, "stelvio:subnet-type": "public", "Name": f"{prefix}net-public-subnet-{az[-1]}"},
    )
