"""Boto3 read-back assertions for the Vpc component's deployed resources."""

from __future__ import annotations

from .assert_helpers import _assert_expected_tags, _boto3_session


def assert_vpc(
    vpc_id: str,
    *,
    cidr: str = "10.0.0.0/16",
    dns_support: bool = True,
    dns_hostnames: bool = True,
) -> None:
    """Assert a VPC's CIDR block and DNS attributes."""
    ec2 = _boto3_session().client("ec2")
    assert ec2.describe_vpcs(VpcIds=[vpc_id])["Vpcs"][0]["CidrBlock"] == cidr
    support = ec2.describe_vpc_attribute(VpcId=vpc_id, Attribute="enableDnsSupport")
    assert support["EnableDnsSupport"]["Value"] == dns_support
    hostnames = ec2.describe_vpc_attribute(VpcId=vpc_id, Attribute="enableDnsHostnames")
    assert hostnames["EnableDnsHostnames"]["Value"] == dns_hostnames


def get_subnets(subnet_ids: list[str]) -> list[dict]:
    """Return AWS subnet descriptions for the given ids."""
    ec2 = _boto3_session().client("ec2")
    return ec2.describe_subnets(SubnetIds=subnet_ids)["Subnets"]


def assert_subnets(subnet_ids: list[str], *, subnet_type: str, cidrs: list[str]) -> None:
    """Assert a subnet tier deployed with the exact CIDR set and stelvio:subnet-type tag."""
    subnets = get_subnets(subnet_ids)
    assert {s["CidrBlock"] for s in subnets} == set(cidrs)
    for s in subnets:
        tags = {t["Key"]: t["Value"] for t in s.get("Tags", [])}
        assert tags.get("stelvio:subnet-type") == subnet_type


def get_default_route(route_table_id: str) -> dict | None:
    """Return the 0.0.0.0/0 route from a route table, or None if it has none."""
    ec2 = _boto3_session().client("ec2")
    rt = ec2.describe_route_tables(RouteTableIds=[route_table_id])["RouteTables"][0]
    for route in rt["Routes"]:
        if route.get("DestinationCidrBlock") == "0.0.0.0/0":
            return route
    return None


def assert_default_route(
    route_table_id: str, *, gateway_id: str | None = None, nat_gateway_id: str | None = None
) -> None:
    """Assert the route table's 0.0.0.0/0 route targets the given internet or NAT gateway."""
    assert gateway_id is not None or nat_gateway_id is not None, "specify a route target"
    route = get_default_route(route_table_id) or {}
    if gateway_id is not None:
        assert route.get("GatewayId") == gateway_id
    if nat_gateway_id is not None:
        assert route.get("NatGatewayId") == nat_gateway_id


def assert_nat_gateway(
    nat_gateway_id: str,
    *,
    subnet_id: str | None = None,
    allocation_id: str | None = None,
) -> None:
    """Assert a NAT gateway is available and, optionally, its subnet + EIP allocation."""
    ec2 = _boto3_session().client("ec2")
    nat = ec2.describe_nat_gateways(NatGatewayIds=[nat_gateway_id])["NatGateways"][0]
    assert nat["State"] == "available"
    if subnet_id is not None:
        assert nat["SubnetId"] == subnet_id
    if allocation_id is not None:
        assert nat["NatGatewayAddresses"][0]["AllocationId"] == allocation_id


def assert_ec2_tags(resource_id: str, expected_tags: dict[str, str]) -> None:
    """Assert an EC2 resource (VPC, subnet, ...) has the expected tag values."""
    ec2 = _boto3_session().client("ec2")
    resp = ec2.describe_tags(Filters=[{"Name": "resource-id", "Values": [resource_id]}])
    tags = {t["Key"]: t["Value"] for t in resp["Tags"]}
    _assert_expected_tags(tags, expected_tags, resource_label=f"EC2 resource {resource_id}")
