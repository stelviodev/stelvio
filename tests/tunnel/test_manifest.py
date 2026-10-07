"""Validate deployed networking at the CLI state-consumption boundary."""

from copy import deepcopy

import boto3
from botocore.stub import Stubber
from pytest import fixture, mark, param, raises

from stelvio.stack_outputs import read_network_manifest
from stelvio.tunnel.aws_inventory import resolve_vpc_inventory
from stelvio.tunnel.policy import BastionPolicy


@mark.parametrize("disabled", ["enableDnsSupport", "enableDnsHostnames"])
def test_private_dns_prerequisites_refuse_before_subnet_or_host_setup(deployed_state, disabled):
    manifest = read_network_manifest(deployed_state)
    network = manifest.vpcs[0]
    client = boto3.client(
        "ec2",
        region_name="us-east-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",  # noqa: S106 - SDK stub
    )
    with Stubber(client) as stub:
        stub.add_response(
            "describe_vpcs",
            {
                "Vpcs": [
                    {
                        "VpcId": network.vpc_id,
                        "OwnerId": "123456789012",
                        "State": "available",
                        "CidrBlock": "10.0.0.0/16",
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
                {"VpcId": network.vpc_id, field: {"Value": attribute != disabled}},
                {"VpcId": network.vpc_id, "Attribute": attribute},
            )
            if attribute == disabled:
                break
        with raises(ValueError, match=disabled + "=true"):
            resolve_vpc_inventory(manifest, lambda _: client)
        stub.assert_no_pending_responses()


@fixture
def deployed_state():
    vpc = "urn:pulumi:dev::sample::stelvio:aws:Vpc::net"
    database = "urn:pulumi:dev::sample::stelvio:aws:DocumentDb::database"
    function = "urn:pulumi:dev::sample::stelvio:aws:Function::api"
    return {
        "checkpoint": {
            "latest": {
                "resources": [
                    {
                        "urn": vpc,
                        "type": "stelvio:aws:Vpc",
                        "outputs": {
                            "_stelvio_network": {
                                "version": 1,
                                "kind": "vpc",
                                "identity": vpc,
                                "account": "123456789012",
                                "region": "us-east-1",
                                "vpc_id": "vpc-0123456789abcdef0",
                                "provider": "urn:pulumi:dev::sample::pulumi:providers:aws::main",
                                "cidrs": ["10.0.0.0/16"],
                                "policy": "temporary",
                                "dns_domains": [],
                                "public_subnets": [
                                    {
                                        "id": "subnet-0123456789abcdef0",
                                        "az": "us-east-1a",
                                        "cidr": "10.0.0.0/24",
                                    }
                                ],
                            }
                        },
                    },
                    {
                        "urn": database,
                        "type": "stelvio:aws:DocumentDb",
                        "outputs": {
                            "_stelvio_network": {
                                "version": 1,
                                "kind": "resource",
                                "identity": database,
                                "vpc": vpc,
                                "service": "documentdb",
                                "resource_id": "database",
                                "ports": [27018],
                                "hostnames": [
                                    "database.cluster-token.us-east-1.docdb.amazonaws.com"
                                ],
                                "security_groups": ["sg-0123456789abcdef0"],
                            }
                        },
                    },
                    {
                        "urn": function,
                        "type": "stelvio:aws:Function",
                        "outputs": {
                            "_stelvio_network": {
                                "version": 1,
                                "kind": "endpoint",
                                "identity": function,
                                "endpoint_id": "api-generation",
                                "vpcs": [vpc],
                            }
                        },
                    },
                ]
            }
        }
    }


def add_second_vpc(state, *, cidr="10.2.0.0/16", policy="persistent", domains=()):
    entries = state["checkpoint"]["latest"]["resources"]
    second = deepcopy(entries[0])
    second["urn"] = second["urn"].replace("::net", "::second")
    metadata = second["outputs"]["_stelvio_network"]
    metadata.update(
        {
            "identity": second["urn"],
            "vpc_id": "vpc-fedcba98765432100",
            "cidrs": [cidr],
            "policy": policy,
            "dns_domains": list(domains),
            "public_subnets": [
                {
                    "id": "subnet-fedcba98765432100",
                    "az": "us-east-1b",
                    "cidr": cidr.replace("/16", "/24"),
                }
            ],
        }
    )
    entries.append(second)
    entries[2]["outputs"]["_stelvio_network"]["vpcs"].append(second["urn"])
    return metadata


@mark.parametrize(
    ("index", "key", "value", "message"),
    [
        param(0, "version", 2, "metadata version", id="version"),
        param(0, "version", True, "metadata version", id="bool-version"),
        param(0, "account", "bad", "AWS account", id="account"),
        param(0, "cidrs", ["10.0.0.1/16"], "canonical IPv4", id="noncanonical-cidr"),
        param(0, "cidrs", ["::/64"], "canonical IPv4", id="ipv6"),
        param(0, "policy", "external", "bastion policy", id="policy"),
        param(0, "cidrs", ["10.0.0.0/16", "10.0.0.0/16"], "Duplicate", id="duplicate-cidr"),
        param(1, "ports", [True], "service port", id="boolean-port"),
        param(1, "ports", [65536], "service port", id="large-port"),
        param(1, "ports", [27017.5], "service port", id="fractional-port"),
        param(1, "hostnames", ["*.example"], "Invalid DNS", id="wildcard-host"),
        param(1, "vpc", "unknown", "unknown VPC", id="resource-reference"),
        param(2, "vpcs", ["unknown"], "unknown VPC", id="endpoint-reference"),
        param(2, "vpcs", [], None, id="non-vpc-endpoint"),
        param(1, "password", "must-never-appear", "unknown fields", id="secret-field"),
    ],
)
def test_state_reader_refuses_invalid_networking(deployed_state, index, key, value, message):
    record = deployed_state["checkpoint"]["latest"]["resources"][index]["outputs"][
        "_stelvio_network"
    ]
    record[key] = value
    if message is None:
        assert not read_network_manifest(deployed_state).used_vpcs
    else:
        with raises(ValueError, match=message) as failure:
            read_network_manifest(deployed_state)
        assert "must-never-appear" not in str(failure.value)


def test_distinct_vpcs_and_disabled_dependencies_remain_separate(deployed_state):
    second = add_second_vpc(deployed_state, cidr="10.0.0.0/16", policy="disabled")
    manifest = read_network_manifest(deployed_state)
    assert {vpc.identity for vpc in manifest.used_vpcs} == {
        second["identity"],
        deployed_state["checkpoint"]["latest"]["resources"][0]["urn"],
    }
    assert len(manifest.enabled_vpcs) == 1
    assert manifest.enabled_vpcs[0].policy == BastionPolicy.TEMPORARY


def test_enabled_vpc_overlap_names_both_owners_and_full_ranges(deployed_state):
    add_second_vpc(deployed_state, cidr="10.0.0.0/16")
    with raises(ValueError, match="overlap") as failure:
        read_network_manifest(deployed_state)
    assert "::net" in str(failure.value)
    assert "::second" in str(failure.value)
    assert "10.0.0.0/16" in str(failure.value)


@mark.parametrize("domain", ["cluster-token.us-east-1.docdb.amazonaws.com", "docdb.amazonaws.com"])
def test_custom_domain_cannot_capture_other_vpc_resource(deployed_state, domain):
    add_second_vpc(deployed_state, domains=(domain,))
    with raises(ValueError, match="DNS ownership conflict"):
        read_network_manifest(deployed_state)


@mark.parametrize(("secondary", "conflict"), [("10.2.0.0/16", True), ("10.3.0.0/16", False)])
def test_complete_aws_cidrs_are_checked_before_routes(deployed_state, secondary, conflict):
    add_second_vpc(deployed_state)
    manifest = read_network_manifest(deployed_state)
    client = boto3.client(
        "ec2",
        region_name="us-east-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",  # noqa: S106 - Stubber intercepts every AWS call
    )
    with Stubber(client) as stub:
        for index, network in enumerate(manifest.enabled_vpcs):
            cidrs = [str(network.cidrs[0]), *([secondary] if index == 0 else [])]
            stub.add_response(
                "describe_vpcs",
                {
                    "Vpcs": [
                        {
                            "VpcId": network.vpc_id,
                            "OwnerId": "123456789012",
                            "State": "available",
                            "CidrBlock": cidrs[0],
                            "CidrBlockAssociationSet": [
                                {"CidrBlock": cidr, "CidrBlockState": {"State": "associated"}}
                                for cidr in cidrs
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
            stub.add_response(
                "describe_subnets",
                {
                    "Subnets": [
                        {
                            "SubnetId": subnet.subnet_id,
                            "VpcId": network.vpc_id,
                            "OwnerId": "123456789012",
                            "AvailabilityZone": subnet.availability_zone,
                            "CidrBlock": str(subnet.cidr),
                            "State": "available",
                        }
                        for subnet in network.public_subnets
                    ]
                },
                {"SubnetIds": [subnet.subnet_id for subnet in network.public_subnets]},
            )
        if conflict:
            with raises(ValueError, match="overlap"):
                resolve_vpc_inventory(manifest, lambda network: client)
        else:
            resolved = resolve_vpc_inventory(manifest, lambda network: client)
            assert tuple(str(cidr) for cidr in resolved.vpcs[0].cidrs) == (
                "10.0.0.0/16",
                secondary,
            )
        stub.assert_no_pending_responses()
    assert tuple(str(cidr) for cidr in manifest.vpcs[0].cidrs) == ("10.0.0.0/16",)


@mark.parametrize(
    ("change", "message"),
    [
        ("missing", "Missing VPC networking metadata"),
        ("identity", "identity differs"),
        ("kind", "does not match"),
        ("duplicate", "Duplicate network identity"),
        ("physical-vpc", "same AWS VPC"),
        ("duplicate-endpoint", "Duplicate invocation"),
    ],
)
def test_incomplete_or_ambiguous_state_is_rejected(deployed_state, change, message):
    entries = deployed_state["checkpoint"]["latest"]["resources"]
    if change == "missing":
        entries[0]["outputs"] = {}
    elif change == "identity":
        entries[0]["outputs"]["_stelvio_network"]["identity"] = "other"
    elif change == "kind":
        entries[0]["outputs"]["_stelvio_network"]["kind"] = "endpoint"
    elif change == "duplicate":
        entries.append(deepcopy(entries[0]))
    elif change == "physical-vpc":
        second = add_second_vpc(deployed_state)
        second["vpc_id"] = entries[0]["outputs"]["_stelvio_network"]["vpc_id"]
    else:
        second = deepcopy(entries[2])
        second["urn"] += "-second"
        second["outputs"]["_stelvio_network"]["identity"] = second["urn"]
        entries.append(second)
    with raises(ValueError, match=message):
        read_network_manifest(deployed_state)


def test_legacy_non_vpc_app_requires_no_network_metadata():
    state = {
        "checkpoint": {
            "latest": {
                "resources": [
                    {
                        "type": "stelvio:aws:Function",
                        "urn": "urn:legacy",
                        "outputs": {"url": "https://example.com"},
                    },
                ]
            }
        }
    }
    assert not read_network_manifest(state).vpcs
    assert not read_network_manifest(None).vpcs


@mark.parametrize(
    ("domain", "conflict"),
    [("nested.internal.example.com", True), ("otherinternal.example.com", False)],
)
def test_custom_domain_matching_uses_label_boundaries(deployed_state, domain, conflict):
    deployed_state["checkpoint"]["latest"]["resources"][0]["outputs"]["_stelvio_network"][
        "dns_domains"
    ] = ["internal.example.com"]
    add_second_vpc(deployed_state, domains=(domain,))
    if conflict:
        with raises(ValueError, match="DNS ownership conflict"):
            read_network_manifest(deployed_state)
    else:
        assert len(read_network_manifest(deployed_state).enabled_vpcs) == 2


@mark.parametrize("mismatch", ["account", "subnet-vpc", "subnet-owner"])
def test_inventory_refuses_changed_aws_ownership(deployed_state, mismatch):
    manifest = read_network_manifest(deployed_state)
    network = manifest.vpcs[0]
    client = boto3.client(
        "ec2",
        region_name="us-east-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",  # noqa: S106 - all AWS calls intercepted by Stubber
    )
    with Stubber(client) as stub:
        stub.add_response(
            "describe_vpcs",
            {
                "Vpcs": [
                    {
                        "VpcId": network.vpc_id,
                        "OwnerId": "999999999999" if mismatch == "account" else "123456789012",
                        "State": "available",
                        "CidrBlock": "10.0.0.0/16",
                    }
                ]
            },
            {"VpcIds": [network.vpc_id]},
        )
        if mismatch != "account":
            for attribute, field in (
                ("enableDnsSupport", "EnableDnsSupport"),
                ("enableDnsHostnames", "EnableDnsHostnames"),
            ):
                stub.add_response(
                    "describe_vpc_attribute",
                    {"VpcId": network.vpc_id, field: {"Value": True}},
                    {"VpcId": network.vpc_id, "Attribute": attribute},
                )
            stub.add_response(
                "describe_subnets",
                {
                    "Subnets": [
                        {
                            "SubnetId": network.public_subnets[0].subnet_id,
                            "VpcId": "vpc-fedcba98765432100"
                            if mismatch == "subnet-vpc"
                            else network.vpc_id,
                            "OwnerId": "999999999999"
                            if mismatch == "subnet-owner"
                            else "123456789012",
                            "AvailabilityZone": "us-east-1a",
                            "State": "available",
                            "CidrBlock": "10.0.0.0/24",
                        }
                    ]
                },
                {"SubnetIds": [network.public_subnets[0].subnet_id]},
            )
        with raises(ValueError, match=r"different AWS owner|subnet identity changed"):
            resolve_vpc_inventory(manifest, lambda network: client)
        stub.assert_no_pending_responses()
