"""AWS supplies topology; deployment records select the permitted subnet identities."""

from dataclasses import replace
from ipaddress import IPv4Network
from types import SimpleNamespace

import boto3
import pytest
from botocore.stub import Stubber

from stelvio.network import NetworkManifest, VpcNetwork
from stelvio.tunnel.aws_inventory import InventoryPendingError, resolve_vpc_inventory
from stelvio.tunnel.runtime_models import SubnetInventory, VpcInventory


def _network(name="one"):
    return VpcNetwork(
        name,
        "123456789012",
        "us-east-1",
        f"vpc-{name}",
        f"provider-{name}",
        (f"subnet-{name}-private", f"subnet-{name}-isolated"),
        f"i-{name}",
    )


def _client():
    return boto3.client(
        "ec2",
        region_name="us-east-1",
        aws_access_key_id="inventory-test",
        aws_secret_access_key="inventory-test-secret",  # noqa: S106  # Stubber intercepts calls
    )


def _subnets(network):
    return [
        {
            "SubnetId": subnet_id,
            "VpcId": network.vpc_id,
            "OwnerId": network.account,
            "AvailabilityZone": "us-east-1a",
            "CidrBlock": cidr,
            "State": "available",
        }
        for subnet_id, cidr in zip(
            network.route_subnet_ids, ("10.1.1.0/24", "10.1.2.0/24"), strict=True
        )
    ]


def _add_vpc(stub, network, *, cidr="10.1.0.0/16", associated=(), association_state="associated"):
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
                        {"CidrBlock": value, "CidrBlockState": {"State": association_state}}
                        for value in (cidr, *associated)
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
            {
                "VpcId": network.vpc_id,
                field: {"Value": True},
            },
            {"VpcId": network.vpc_id, "Attribute": attribute},
        )


def _add_network(stub, network, *, cidr="10.1.0.0/16", associated=(), subnets=None):
    _add_vpc(stub, network, cidr=cidr, associated=associated)
    if network.route_subnet_ids:
        stub.add_response(
            "describe_subnets",
            {
                "Subnets": _subnets(network) if subnets is None else subnets,
            },
            {"SubnetIds": list(network.route_subnet_ids)},
        )


def _resolve(network, client):
    return resolve_vpc_inventory(NetworkManifest((network,), ()), lambda _: client)[
        network.identity
    ]


def test_inventory_derives_topology_without_mutating_the_deployed_record():
    deployed = _network()
    client = _client()
    with Stubber(client) as stub:
        _add_network(stub, deployed, associated=("10.3.0.0/16",))
        resolved = _resolve(deployed, client)
        stub.assert_no_pending_responses()
    assert resolved == VpcInventory(
        deployed,
        (IPv4Network("10.1.0.0/16"), IPv4Network("10.3.0.0/16")),
        (
            SubnetInventory("subnet-one-private", "us-east-1a", IPv4Network("10.1.1.0/24")),
            SubnetInventory("subnet-one-isolated", "us-east-1a", IPv4Network("10.1.2.0/24")),
        ),
    )
    assert resolved.record is deployed
    assert deployed == _network()
    assert resolved.route_cidrs == (IPv4Network("10.1.1.0/24"), IPv4Network("10.1.2.0/24"))


def test_inventory_preserves_record_order_when_aws_response_is_shuffled():
    deployed = _network()
    response = list(reversed(_subnets(deployed)))
    response[0]["MapPublicIpOnLaunch"] = True
    client = _client()
    with Stubber(client) as stub:
        _add_network(stub, deployed, subnets=response)
        resolved = _resolve(deployed, client)
        stub.assert_no_pending_responses()
    assert (
        tuple(subnet.subnet_id for subnet in resolved.route_subnets) == deployed.route_subnet_ids
    )
    assert resolved.route_cidrs == (IPv4Network("10.1.1.0/24"), IPv4Network("10.1.2.0/24"))


def test_secondary_ownership_ranges_never_implicitly_add_routes():
    deployed = _network()
    response = _subnets(deployed)
    response[0]["CidrBlock"] = "10.3.1.0/24"
    client = _client()
    with Stubber(client) as stub:
        _add_network(stub, deployed, associated=("10.4.0.0/16", "10.3.0.0/16"), subnets=response)
        resolved = _resolve(deployed, client)
        stub.assert_no_pending_responses()
    assert resolved.cidrs == tuple(map(IPv4Network, ("10.1.0.0/16", "10.3.0.0/16", "10.4.0.0/16")))
    assert resolved.route_cidrs == tuple(map(IPv4Network, ("10.3.1.0/24", "10.1.2.0/24")))


def test_inventory_refuses_a_client_from_another_region():
    client = SimpleNamespace(meta=SimpleNamespace(region_name="eu-west-1"))
    with pytest.raises(ValueError, match="different AWS region"):
        _resolve(_network(), client)


@pytest.mark.parametrize("attribute", ["enableDnsSupport", "enableDnsHostnames"])
def test_inventory_refuses_disabled_private_dns(attribute):
    network, client = _network(), _client()
    with Stubber(client) as stub:
        stub.add_response(
            "describe_vpcs",
            {
                "Vpcs": [
                    {
                        "VpcId": network.vpc_id,
                        "OwnerId": network.account,
                        "State": "available",
                        "CidrBlock": "10.1.0.0/16",
                    }
                ]
            },
            {"VpcIds": [network.vpc_id]},
        )
        for name, field in (
            ("enableDnsSupport", "EnableDnsSupport"),
            ("enableDnsHostnames", "EnableDnsHostnames"),
        ):
            stub.add_response(
                "describe_vpc_attribute",
                {"VpcId": network.vpc_id, field: {"Value": name != attribute}},
                {"VpcId": network.vpc_id, "Attribute": name},
            )
            if name == attribute:
                break
        with pytest.raises(ValueError, match=attribute + "=true"):
            _resolve(network, client)
        stub.assert_no_pending_responses()


@pytest.mark.parametrize(
    ("field", "value"), [("VpcId", "vpc-foreign"), ("OwnerId", "999999999999")]
)
def test_inventory_refuses_foreign_vpc_ownership(field, value):
    network, client = _network(), _client()
    response = {
        "VpcId": network.vpc_id,
        "OwnerId": network.account,
        "State": "available",
        "CidrBlock": "10.1.0.0/16",
    }
    response[field] = value
    with Stubber(client) as stub:
        stub.add_response("describe_vpcs", {"Vpcs": [response]}, {"VpcIds": [network.vpc_id]})
        with pytest.raises(ValueError, match="different AWS owner"):
            _resolve(network, client)
        stub.assert_no_pending_responses()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("VpcId", "vpc-foreign"),
        ("OwnerId", "999999999999"),
        ("AvailabilityZone", ""),
        ("CidrBlock", "10.8.0.0/24"),
        ("CidrBlock", "10.1.1.1/24"),
    ],
)
def test_inventory_refuses_invalid_subnet_ownership_and_ranges(field, value):
    network, client = _network(), _client()
    response = _subnets(network)
    response[0][field] = value
    with Stubber(client) as stub:
        _add_network(stub, network, subnets=response)
        with pytest.raises(ValueError, match=r"subnet ownership|outside its CIDRs|host bits"):
            _resolve(network, client)
        stub.assert_no_pending_responses()


@pytest.mark.parametrize(
    "ids",
    [
        [],
        ["subnet-one-private"],
        ["subnet-one-private", "subnet-one-private"],
        ["subnet-one-private", "subnet-extra"],
    ],
)
def test_inventory_requires_the_exact_subnet_identity_set(ids):
    network, client = _network(), _client()
    response = [dict(_subnets(network)[0], SubnetId=subnet_id) for subnet_id in ids]
    with Stubber(client) as stub:
        _add_network(stub, network, subnets=response)
        with pytest.raises(ValueError, match="subnet inventory changed"):
            _resolve(network, client)
        stub.assert_no_pending_responses()


def test_empty_route_ids_never_trigger_an_unfiltered_subnet_query():
    network, client = replace(_network(), route_subnet_ids=()), _client()
    with Stubber(client) as stub:
        _add_vpc(stub, network)
        assert _resolve(network, client).route_cidrs == ()
        stub.assert_no_pending_responses()


def test_duplicate_recorded_subnet_ids_never_trigger_a_subnet_query():
    network = replace(_network(), route_subnet_ids=("subnet-one", "subnet-one"))
    client = _client()
    with Stubber(client) as stub:
        _add_vpc(stub, network)
        with pytest.raises(ValueError, match="duplicate deployed subnet IDs"):
            _resolve(network, client)
        stub.assert_no_pending_responses()


@pytest.mark.parametrize("state", ["associating", "disassociating", "failing"])
def test_changing_vpc_associations_are_retryable_before_subnet_selection(state):
    network, client = _network(), _client()
    with Stubber(client) as stub:
        _add_vpc(stub, network, association_state=state)
        with pytest.raises(InventoryPendingError, match="changing CIDR associations"):
            _resolve(network, client)
        stub.assert_no_pending_responses()


def test_nonavailable_subnet_is_retryable():
    network, client = _network(), _client()
    response = _subnets(network)
    response[0]["State"] = "pending"
    with Stubber(client) as stub:
        _add_network(stub, network, subnets=response)
        with pytest.raises(InventoryPendingError, match="not available"):
            _resolve(network, client)
        stub.assert_no_pending_responses()


def test_every_bastion_is_resolved_without_functions_and_disabled_vpcs_are_skipped():
    first, second = _network(), _network("two")
    disabled = replace(_network("three"), bastion_instance_id=None)
    manifest = NetworkManifest((first, disabled, second), ())
    client = _client()
    requested = []

    def client_for(network):
        requested.append(network.identity)
        return client

    response = [
        dict(subnet, CidrBlock=cidr)
        for subnet, cidr in zip(_subnets(second), ("10.2.1.0/24", "10.2.2.0/24"), strict=True)
    ]
    with Stubber(client) as stub:
        _add_network(stub, first)
        _add_network(stub, second, cidr="10.2.0.0/16", subnets=response)
        resolved = resolve_vpc_inventory(manifest, client_for)
        stub.assert_no_pending_responses()
    assert requested == ["one", "two"]
    assert tuple(resolved) == ("one", "two")


def test_partial_overlap_in_associated_ranges_is_rejected_without_functions():
    first, second, client = _network(), _network("two"), _client()
    response = [
        dict(subnet, CidrBlock=cidr)
        for subnet, cidr in zip(_subnets(second), ("10.2.1.0/24", "10.2.2.0/24"), strict=True)
    ]
    with Stubber(client) as stub:
        _add_network(stub, first)
        _add_network(
            stub, second, cidr="10.2.0.0/16", associated=("10.1.128.0/17",), subnets=response
        )
        with pytest.raises(ValueError, match=r"10.1.0.0/16.*10.1.128.0/17.*overlap"):
            resolve_vpc_inventory(NetworkManifest((first, second), ()), lambda _: client)
        stub.assert_no_pending_responses()
