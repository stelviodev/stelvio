"""AWS inventory is resolved and ownership checked before host networking."""

from ipaddress import IPv4Network
from types import SimpleNamespace

import boto3
import pytest
from botocore.client import BaseClient
from botocore.stub import Stubber

from stelvio.tunnel.aws_inventory import resolve_vpc_inventory
from stelvio.tunnel.manifest import (
    EndpointNetwork,
    NetworkManifest,
    SubnetNetwork,
    VpcNetwork,
)
from stelvio.tunnel.policy import BastionPolicy


def _network(name: str, cidr: str) -> VpcNetwork:
    return VpcNetwork(
        name,
        "123456789012",
        "us-east-1",
        f"vpc-{name}",
        f"provider-{name}",
        (IPv4Network(cidr),),
        BastionPolicy.PERSISTENT,
        (),
        (
            SubnetNetwork(
                f"subnet-{name}", "us-east-1a", IPv4Network(cidr).subnets(new_prefix=24).__next__()
            ),
        ),
    )


def _manifest(*networks: VpcNetwork) -> NetworkManifest:
    return NetworkManifest(
        tuple(networks),
        (),
        (
            EndpointNetwork(
                "function", "function", tuple(network.identity for network in networks)
            ),
        ),
    )


def _client() -> BaseClient:
    return boto3.client(
        "ec2",
        region_name="us-east-1",
        aws_access_key_id="inventory-test",
        aws_secret_access_key="inventory-test-secret",  # noqa: S106 - Stubber intercepts calls
    )


def _add_network(stub: Stubber, network: VpcNetwork, *, associated: tuple[str, ...] = ()) -> None:
    cidr = str(network.cidrs[0])
    associations = [cidr, *associated]
    stub.add_response(
        "describe_vpcs",
        {
            "Vpcs": [
                {
                    "VpcId": network.vpc_id,
                    "OwnerId": network.account,
                    "State": "available",
                    "CidrBlock": cidr,
                    "CidrBlockAssociationSet": [
                        {"CidrBlock": value, "CidrBlockState": {"State": "associated"}}
                        for value in associations
                    ],
                }
            ]
        },
        {"VpcIds": [network.vpc_id]},
    )
    for attribute, field in (
        ("enableDnsSupport", "EnableDnsSupport"),
        ("enableDnsHostnames", "EnableDnsHostnames"),
    ):
        stub.add_response(
            "describe_vpc_attribute",
            {"VpcId": network.vpc_id, field: {"Value": True}},
            {"VpcId": network.vpc_id, "Attribute": attribute},
        )
    subnet = network.public_subnets[0]
    stub.add_response(
        "describe_subnets",
        {
            "Subnets": [
                {
                    "SubnetId": subnet.subnet_id,
                    "VpcId": network.vpc_id,
                    "OwnerId": network.account,
                    "AvailabilityZone": subnet.availability_zone,
                    "CidrBlock": str(subnet.cidr),
                    "State": "available",
                }
            ]
        },
        {"SubnetIds": [subnet.subnet_id]},
    )


def test_inventory_adds_associated_cidrs_without_mutating_deployed_manifest():
    deployed = _manifest(_network("one", "10.1.0.0/16"))
    client = _client()
    with Stubber(client) as stub:
        _add_network(stub, deployed.vpcs[0], associated=("10.3.0.0/16",))
        resolved = resolve_vpc_inventory(deployed, lambda _: client)
        stub.assert_no_pending_responses()
    assert tuple(map(str, resolved.vpcs[0].cidrs)) == ("10.1.0.0/16", "10.3.0.0/16")
    assert tuple(map(str, deployed.vpcs[0].cidrs)) == ("10.1.0.0/16",)


def test_inventory_refuses_a_client_from_another_region():
    network = _network("one", "10.1.0.0/16")
    deployed = _manifest(network)
    client = SimpleNamespace(meta=SimpleNamespace(region_name="eu-west-1"))
    with pytest.raises(ValueError, match="different AWS region"):
        resolve_vpc_inventory(deployed, lambda _: client)


@pytest.mark.parametrize("attribute", ["enableDnsSupport", "enableDnsHostnames"])
def test_inventory_refuses_when_private_dns_is_disabled(attribute: str):
    network = _network("one", "10.1.0.0/16")
    deployed = _manifest(network)
    client = _client()
    with Stubber(client) as stub:
        stub.add_response(
            "describe_vpcs",
            {
                "Vpcs": [
                    {
                        "VpcId": network.vpc_id,
                        "OwnerId": network.account,
                        "State": "available",
                        "CidrBlock": str(network.cidrs[0]),
                    }
                ]
            },
            {"VpcIds": [network.vpc_id]},
        )
        for name, response_field in (
            ("enableDnsSupport", "EnableDnsSupport"),
            ("enableDnsHostnames", "EnableDnsHostnames"),
        ):
            stub.add_response(
                "describe_vpc_attribute",
                {"VpcId": network.vpc_id, response_field: {"Value": name != attribute}},
                {"VpcId": network.vpc_id, "Attribute": name},
            )
            if name == attribute:
                break
        with pytest.raises(ValueError, match=attribute + "=true"):
            resolve_vpc_inventory(deployed, lambda _: client)
        stub.assert_no_pending_responses()


@pytest.mark.parametrize("mismatch", ["account", "primary-cidr", "subnet-vpc", "subnet-owner"])
def test_inventory_rejects_changed_aws_ownership(mismatch: str):
    network = _network("one", "10.1.0.0/16")
    deployed = _manifest(network)
    client = _client()
    with Stubber(client) as stub:
        vpc = {
            "VpcId": network.vpc_id,
            "OwnerId": "999999999999" if mismatch == "account" else network.account,
            "State": "available",
            "CidrBlock": "10.9.0.0/16" if mismatch == "primary-cidr" else str(network.cidrs[0]),
        }
        stub.add_response("describe_vpcs", {"Vpcs": [vpc]}, {"VpcIds": [network.vpc_id]})
        if mismatch not in {"account", "primary-cidr"}:
            for attribute, field in (
                ("enableDnsSupport", "EnableDnsSupport"),
                ("enableDnsHostnames", "EnableDnsHostnames"),
            ):
                stub.add_response(
                    "describe_vpc_attribute",
                    {"VpcId": network.vpc_id, field: {"Value": True}},
                    {"VpcId": network.vpc_id, "Attribute": attribute},
                )
            subnet = network.public_subnets[0]
            stub.add_response(
                "describe_subnets",
                {
                    "Subnets": [
                        {
                            "SubnetId": subnet.subnet_id,
                            "VpcId": "vpc-foreign" if mismatch == "subnet-vpc" else network.vpc_id,
                            "OwnerId": "999999999999"
                            if mismatch == "subnet-owner"
                            else network.account,
                            "AvailabilityZone": subnet.availability_zone,
                            "CidrBlock": str(subnet.cidr),
                            "State": "available",
                        }
                    ]
                },
                {"SubnetIds": [subnet.subnet_id]},
            )
        with pytest.raises(
            ValueError, match=r"different AWS owner|primary CIDR changed|subnet identity changed"
        ):
            resolve_vpc_inventory(deployed, lambda _: client)
        stub.assert_no_pending_responses()


def test_inventory_rejects_overlap_in_full_associated_vpc_ranges():
    first = _network("one", "10.1.0.0/16")
    second = _network("two", "10.2.0.0/16")
    deployed = _manifest(first, second)
    client = _client()
    with Stubber(client) as stub:
        _add_network(stub, first, associated=("10.2.0.0/16",))
        _add_network(stub, second)
        with pytest.raises(ValueError, match="overlap"):
            resolve_vpc_inventory(deployed, lambda _: client)
        stub.assert_no_pending_responses()
