"""AWS inventory is resolved and ownership checked before host networking."""

from dataclasses import replace
from ipaddress import IPv4Address, IPv4Network
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
    cidr = IPv4Network(cidr)
    return VpcNetwork(
        identity=name,
        account="123456789012",
        region="us-east-1",
        vpc_id=f"vpc-{name}",
        provider=f"provider-{name}",
        cidrs=(cidr,),
        policy=BastionPolicy.PERSISTENT,
        dns_domains=(),
        public_subnets=(
            SubnetNetwork(
                f"subnet-{name}",
                "us-east-1a",
                IPv4Network((cidr.network_address, 24)),
            ),
        ),
        private_subnets=(
            SubnetNetwork(
                f"subnet-{name}-private",
                "us-east-1a",
                IPv4Network((cidr.network_address + 256, 24)),
            ),
        ),
        isolated_subnets=(
            SubnetNetwork(
                f"subnet-{name}-isolated",
                "us-east-1a",
                IPv4Network((cidr.network_address + 512, 24)),
            ),
        ),
        access=None,
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


def _subnet_record(network: VpcNetwork, subnet: SubnetNetwork) -> dict:
    return {
        "SubnetId": subnet.subnet_id,
        "VpcId": network.vpc_id,
        "OwnerId": network.account,
        "AvailabilityZone": subnet.availability_zone,
        "CidrBlock": str(subnet.cidr),
        "State": "available",
    }


def _add_vpc(
    stub: Stubber,
    network: VpcNetwork,
    *,
    associated: tuple[str, ...] = (),
    association_state: str = "associated",
) -> None:
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
                        {"CidrBlock": value, "CidrBlockState": {"State": association_state}}
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


def _add_network(
    stub: Stubber,
    network: VpcNetwork,
    *,
    associated: tuple[str, ...] = (),
    subnet_response: list[dict] | None = None,
) -> None:
    _add_vpc(stub, network, associated=associated)
    stub.add_response(
        "describe_subnets",
        {
            "Subnets": subnet_response
            if subnet_response is not None
            else [_subnet_record(network, subnet) for subnet in network.subnets]
        },
        {"SubnetIds": [subnet.subnet_id for subnet in network.subnets]},
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
            subnets = [_subnet_record(network, subnet) for subnet in network.subnets]
            if mismatch == "subnet-vpc":
                subnets[0]["VpcId"] = "vpc-foreign"
            else:
                subnets[0]["OwnerId"] = "999999999999"
            stub.add_response(
                "describe_subnets",
                {"Subnets": subnets},
                {"SubnetIds": [subnet.subnet_id for subnet in network.subnets]},
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


@pytest.mark.parametrize("tier", ["public_subnets", "private_subnets", "isolated_subnets"])
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("VpcId", "vpc-foreign"),
        ("OwnerId", "999999999999"),
        ("AvailabilityZone", "us-east-1b"),
        ("CidrBlock", "10.1.9.0/24"),
        ("State", "pending"),
        ("OwnerId", None),
    ],
)
def test_inventory_rechecks_every_deployed_subnet_identity(tier, field, value):
    network = _network("one", "10.1.0.0/16")
    records = [_subnet_record(network, subnet) for subnet in network.subnets]
    identity = getattr(network, tier)[0].subnet_id
    record = next(record for record in records if record["SubnetId"] == identity)
    if value is None:
        del record[field]
    else:
        record[field] = value
    client = _client()
    with Stubber(client) as stub:
        _add_network(stub, network, subnet_response=records)
        with pytest.raises(ValueError, match="subnet identity changed") as caught:
            resolve_vpc_inventory(_manifest(network), lambda _: client)
        assert identity in str(caught.value)
        stub.assert_no_pending_responses()


@pytest.mark.parametrize(
    "fault",
    ["missing-public", "missing-private", "missing-isolated", "duplicate", "extra", "missing-id"],
)
def test_inventory_requires_exact_subnet_identity_multiset(fault):
    network = _network("one", "10.1.0.0/16")
    records = [_subnet_record(network, subnet) for subnet in network.subnets]
    if fault.startswith("missing-") and fault != "missing-id":
        tier = fault.removeprefix("missing-") + "_subnets"
        identity = getattr(network, tier)[0].subnet_id
        records = [record for record in records if record["SubnetId"] != identity]
    elif fault == "duplicate":
        records.append(dict(records[1]))
    elif fault == "extra":
        records.append(dict(records[1], SubnetId="subnet-extra"))
    else:
        del records[1]["SubnetId"]
    client = _client()
    with Stubber(client) as stub:
        _add_network(stub, network, subnet_response=records)
        with pytest.raises(ValueError, match="subnet inventory changed"):
            resolve_vpc_inventory(_manifest(network), lambda _: client)
        stub.assert_no_pending_responses()


def test_inventory_preserves_deployed_tiers_when_aws_changes_response_order():
    network = _network("one", "10.1.0.0/16")
    records = [_subnet_record(network, subnet) for subnet in reversed(network.subnets)]
    # Public IPv4 auto-assignment does not establish a subnet's declared tier.
    for record in records:
        record["MapPublicIpOnLaunch"] = True
    client = _client()
    with Stubber(client) as stub:
        _add_network(stub, network, subnet_response=records)
        resolved = resolve_vpc_inventory(_manifest(network), lambda _: client).vpcs[0]
        stub.assert_no_pending_responses()
    assert resolved.subnets == network.subnets
    assert resolved.route_cidrs == (IPv4Network("10.1.1.0/24"), IPv4Network("10.1.2.0/24"))
    for address in ("10.1.0.2", "10.1.0.9", "10.1.3.1"):
        assert not any(IPv4Address(address) in cidr for cidr in resolved.route_cidrs)
    assert resolved.cidrs == network.cidrs


def test_secondary_vpc_ownership_blocks_do_not_implicitly_add_routes():
    network = _network("one", "10.1.0.0/16")
    private = replace(network.private_subnets[0], cidr=IPv4Network("10.3.1.0/24"))
    deployed = replace(
        network, cidrs=(*network.cidrs, IPv4Network("10.3.0.0/16")), private_subnets=(private,)
    )
    client = _client()
    with Stubber(client) as stub:
        _add_network(stub, deployed, associated=("10.3.0.0/16", "10.4.0.0/16"))
        resolved = resolve_vpc_inventory(_manifest(deployed), lambda _: client).vpcs[0]
        stub.assert_no_pending_responses()
    assert tuple(map(str, resolved.cidrs)) == ("10.1.0.0/16", "10.3.0.0/16", "10.4.0.0/16")
    assert tuple(map(str, resolved.route_cidrs)) == ("10.3.1.0/24", "10.1.2.0/24")
    assert IPv4Address(int(resolved.cidrs[0].network_address) + 2) == IPv4Address("10.1.0.2")
    assert not any(IPv4Address("10.1.0.2") in cidr for cidr in resolved.route_cidrs)
    assert deployed.cidrs == (IPv4Network("10.1.0.0/16"), IPv4Network("10.3.0.0/16"))


def test_inventory_refuses_a_subnet_outside_live_vpc_associations():
    network = _network("one", "10.1.0.0/16")
    private = replace(network.private_subnets[0], cidr=IPv4Network("10.3.1.0/24"))
    network = replace(
        network, cidrs=(*network.cidrs, IPv4Network("10.3.0.0/16")), private_subnets=(private,)
    )
    client = _client()
    with Stubber(client) as stub:
        _add_network(stub, network)  # The former secondary ownership block is absent.
        with pytest.raises(ValueError, match="subnet identity changed"):
            resolve_vpc_inventory(_manifest(network), lambda _: client)
        stub.assert_no_pending_responses()


@pytest.mark.parametrize("state", ["associating", "disassociating", "failing"])
def test_inventory_refuses_changing_vpc_associations_before_subnet_selection(state):
    network = _network("one", "10.1.0.0/16")
    client = _client()
    with Stubber(client) as stub:
        _add_vpc(stub, network, association_state=state)
        with pytest.raises(ValueError, match="changing CIDR associations"):
            resolve_vpc_inventory(_manifest(network), lambda _: client)
        stub.assert_no_pending_responses()


def test_public_only_inventory_does_not_fall_back_to_whole_vpc_routes():
    network = replace(_network("one", "10.1.0.0/16"), private_subnets=(), isolated_subnets=())
    client = _client()
    with Stubber(client) as stub:
        _add_network(stub, network)
        resolved = resolve_vpc_inventory(_manifest(network), lambda _: client).vpcs[0]
        stub.assert_no_pending_responses()
    assert resolved.route_cidrs == ()
    assert resolved.cidrs == network.cidrs


@pytest.mark.parametrize("fault", ["empty", "duplicate"])
def test_invalid_declared_subnets_never_trigger_an_unfiltered_aws_query(fault):
    network = _network("one", "10.1.0.0/16")
    if fault == "empty":
        network = replace(network, public_subnets=(), private_subnets=(), isolated_subnets=())
    else:
        network = replace(network, isolated_subnets=network.private_subnets)
    client = _client()
    with Stubber(client) as stub:
        _add_vpc(stub, network)
        with pytest.raises(ValueError, match="invalid deployed subnet inventory"):
            resolve_vpc_inventory(_manifest(network), lambda _: client)
        stub.assert_no_pending_responses()


def test_unused_and_disabled_vpcs_do_not_request_aws_inventory():
    enabled = _network("one", "10.1.0.0/16")
    disabled = replace(_network("two", "10.2.0.0/16"), policy=BastionPolicy.DISABLED)
    unused = _network("three", "10.3.0.0/16")
    manifest = replace(_manifest(enabled, disabled), vpcs=(enabled, disabled, unused))
    client = _client()
    called = []

    def client_for(network):
        called.append(network.identity)
        assert network is enabled
        return client

    with Stubber(client) as stub:
        _add_network(stub, enabled)
        resolved = resolve_vpc_inventory(manifest, client_for)
        stub.assert_no_pending_responses()
    assert called == ["one"]
    assert resolved.vpcs[1:] == manifest.vpcs[1:]


def test_ten_az_inventory_selects_twenty_routes_from_thirty_deployed_subnets():
    network = _network("one", "10.1.0.0/16")
    tiers = [
        tuple(
            SubnetNetwork(
                f"subnet-{tier}-{index}",
                f"us-east-1{chr(ord('a') + index)}",
                IPv4Network(f"10.1.{tier * 10 + index}.0/24"),
            )
            for index in range(10)
        )
        for tier in range(3)
    ]
    network = replace(
        network, public_subnets=tiers[0], private_subnets=tiers[1], isolated_subnets=tiers[2]
    )
    client = _client()
    with Stubber(client) as stub:
        _add_network(stub, network, associated=("10.3.0.0/16",))
        resolved = resolve_vpc_inventory(_manifest(network), lambda _: client).vpcs[0]
        stub.assert_no_pending_responses()
    assert len(resolved.subnets) == 30
    assert tuple(map(str, resolved.route_cidrs)) == tuple(
        f"10.1.{index}.0/24" for index in range(10, 30)
    )
    assert resolved.cidrs == (IPv4Network("10.1.0.0/16"), IPv4Network("10.3.0.0/16"))
