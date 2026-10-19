"""Validate the private networking metadata read from deployed state."""

from copy import deepcopy

from pytest import fixture, mark, param, raises

from stelvio.stack_outputs import read_network_manifest
from stelvio.tunnel.policy import BastionPolicy


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
                                "version": 2,
                                "kind": "vpc",
                                "identity": vpc,
                                "account": "123456789012",
                                "region": "us-east-1",
                                "vpc_id": "vpc-0123456789abcdef0",
                                "provider": "urn:pulumi:dev::sample::pulumi:providers:aws::main",
                                "cidrs": ["10.0.0.0/16"],
                                "policy": "persistent",
                                "access": {
                                    "instance_id": "i-12345678",
                                    "security_group_id": "sg-12345678",
                                    "az": "us-east-1a",
                                    "identity_document": "identity",
                                    "owner": vpc,
                                },
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
                                "version": 2,
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
                                "version": 2,
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
    if policy == "disabled":
        metadata["access"] = None
    else:
        metadata["access"] = {
            "instance_id": "i-second123",
            "security_group_id": "sg-second123",
            "az": "us-east-1b",
            "identity_document": "identity-second",
            "owner": second["urn"],
        }
    entries.append(second)
    entries[2]["outputs"]["_stelvio_network"]["vpcs"].append(second["urn"])
    return metadata


@mark.parametrize(
    ("index", "key", "value", "message"),
    [
        param(0, "version", 3, "metadata version", id="version"),
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
    assert manifest.enabled_vpcs[0].policy == BastionPolicy.PERSISTENT


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


@mark.parametrize(
    ("change", "message"),
    [
        ("missing", "Missing VPC networking metadata"),
        ("missing-function", "Missing VPC networking metadata"),
        ("identity", "identity differs"),
        ("kind", "does not match"),
        ("duplicate", "Duplicate network identity"),
        ("physical-vpc", "same AWS VPC"),
        ("duplicate-endpoint", "Duplicate invocation"),
        ("missing-access", "Persistent bastion metadata is missing"),
    ],
)
def test_incomplete_or_ambiguous_state_is_rejected(deployed_state, change, message):
    entries = deployed_state["checkpoint"]["latest"]["resources"]
    if change == "missing":
        entries[0]["outputs"] = {}
    elif change == "missing-function":
        entries[2]["outputs"] = {}
    elif change == "identity":
        entries[0]["outputs"]["_stelvio_network"]["identity"] = "other"
    elif change == "kind":
        entries[0]["outputs"]["_stelvio_network"]["kind"] = "endpoint"
    elif change == "duplicate":
        entries.append(deepcopy(entries[0]))
    elif change == "physical-vpc":
        second = add_second_vpc(deployed_state)
        second["vpc_id"] = entries[0]["outputs"]["_stelvio_network"]["vpc_id"]
    elif change == "missing-access":
        entries[0]["outputs"]["_stelvio_network"]["access"] = None
    else:
        second = deepcopy(entries[2])
        second["urn"] += "-second"
        second["outputs"]["_stelvio_network"]["identity"] = second["urn"]
        entries.append(second)
    with raises(ValueError, match=message):
        read_network_manifest(deployed_state)


def test_non_vpc_app_requires_no_network_metadata():
    state = {
        "checkpoint": {
            "latest": {
                "resources": [
                    {
                        "type": "stelvio:aws:Function",
                        "urn": "urn:plain",
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


@mark.parametrize("version", [0, 1, 3, "2", None])
def test_unsupported_metadata_versions_are_rejected(deployed_state, version):
    metadata = deployed_state["checkpoint"]["latest"]["resources"][0]["outputs"][
        "_stelvio_network"
    ]
    metadata["version"] = version
    with raises(ValueError, match="Unsupported or missing network metadata version"):
        read_network_manifest(deployed_state)


@mark.parametrize("policy", ["temporary", "auto", None])
def test_invalid_bastion_policy_is_rejected(deployed_state, policy):
    metadata = deployed_state["checkpoint"]["latest"]["resources"][0]["outputs"][
        "_stelvio_network"
    ]
    metadata["policy"] = policy
    with raises(ValueError, match="Invalid network metadata bastion policy"):
        read_network_manifest(deployed_state)


def test_disabled_resource_names_do_not_claim_managed_dns(deployed_state):
    entries = deployed_state["checkpoint"]["latest"]["resources"]
    original = deepcopy(entries[1])
    second = add_second_vpc(deployed_state, policy="disabled")
    original["urn"] += "-disabled"
    original["outputs"]["_stelvio_network"].update(
        identity=original["urn"],
        vpc=second["identity"],
        resource_id="disabled-cluster",
        security_groups=["sg-disabled"],
    )
    entries.append(original)
    assert len(read_network_manifest(deployed_state).enabled_vpcs) == 1


def test_access_descriptor_must_be_owned_by_its_vpc(deployed_state):
    metadata = deployed_state["checkpoint"]["latest"]["resources"][0]["outputs"][
        "_stelvio_network"
    ]
    metadata["access"] = {
        "instance_id": "i-12345678",
        "security_group_id": "sg-12345678",
        "az": "us-east-1a",
        "identity_document": "identity",
        "owner": "urn:other-vpc",
    }
    metadata["policy"] = "persistent"
    with raises(ValueError, match="different owner"):
        read_network_manifest(deployed_state)


def test_disabled_vpc_cannot_claim_bastion_access(deployed_state):
    metadata = deployed_state["checkpoint"]["latest"]["resources"][0]["outputs"][
        "_stelvio_network"
    ]
    metadata["policy"] = "disabled"
    metadata["access"] = {
        "instance_id": "i-12345678",
        "security_group_id": "sg-12345678",
        "az": "us-east-1a",
        "identity_document": "identity",
        "owner": metadata["identity"],
    }
    with raises(ValueError, match="must not contain access resources"):
        read_network_manifest(deployed_state)


def test_integral_float_ports_round_trip_from_pulumi_state(deployed_state):
    deployed_state["checkpoint"]["latest"]["resources"][0]["outputs"]["_stelvio_network"][
        "version"
    ] = 2.0
    metadata = deployed_state["checkpoint"]["latest"]["resources"][1]["outputs"][
        "_stelvio_network"
    ]
    metadata["ports"] = [27018.0]
    manifest = read_network_manifest(deployed_state)
    assert manifest.resources[0].ports == (27018,)


def test_bastion_security_group_cannot_be_claimed_by_two_vpcs(deployed_state):
    first_access = deployed_state["checkpoint"]["latest"]["resources"][0]["outputs"][
        "_stelvio_network"
    ]["access"]
    second = add_second_vpc(deployed_state)
    second["access"]["security_group_id"] = first_access["security_group_id"]
    with raises(ValueError, match="claimed by different VPCs"):
        read_network_manifest(deployed_state)


def test_public_subnet_cannot_be_claimed_by_two_vpcs(deployed_state):
    first_subnet = deployed_state["checkpoint"]["latest"]["resources"][0]["outputs"][
        "_stelvio_network"
    ]["public_subnets"][0]["id"]
    second = add_second_vpc(deployed_state)
    second["public_subnets"][0]["id"] = first_subnet
    with raises(ValueError, match="subnet is claimed by different VPCs"):
        read_network_manifest(deployed_state)
